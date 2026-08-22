"""Minimal verified Wanda Android API adapter for temporary quote probes.

This module intentionally exposes only seat reads, temporary order creation,
locked activity lookup, cancellation and status verification. It has no payment,
refund, issuance or ticket-system HTTP capability.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .wanda_direct_gateway import DirectGatewayError


FRONT_ORIGIN: Final = "https://front-gateway-c.wandafilm.com"
MARKETING_ORIGIN: Final = "https://mkt-activity-api-prd-mx.wandafilm.com"
ALLOWED_HOSTS: Final = frozenset({"front-gateway-c.wandafilm.com", "mkt-activity-api-prd-mx.wandafilm.com"})
DEFAULT_ACCOUNT_POOL_PATH: Final = Path("/var/lib/ticket-system/backend-data/accounts.json")
SALE_SUBJECT_CODE: Final = "Wanda"
APP_CHANNEL: Final = "1_2"
APP_VERSION: Final = "9.3.6"
APP_MODEL: Final = "meizu 17"
APP_SYSTEM_VERSION: Final = "11"
# Public application protocol material extracted from the official client; it
# is not an account credential. Account tokens always come from the pool file.
DEFAULT_APP_CLIENT_KEY: Final = "B6C1D9E2F8G7H5J3K4L0MNPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZ5678A"
APP_AES_KEY: Final = b"6f34faeefba8fd39"


def _lowercase_quote(value: str) -> str:
    encoded = quote(str(value), safe="!*'()")
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda item: "%" + item.group(1).lower(), encoded)


def _eligible_account(account: Mapping[str, Any]) -> bool:
    risk = str(account.get("risk_status") or "").strip().lower()
    account_type = str(account.get("account_type") or "").strip().lower()
    return bool(
        str(account.get("status") or "").strip().lower() == "online"
        and risk in {"", "normal", "ok", "safe", "正常"}
        and (account.get("is_wplus") is True or account_type == "wplus")
        and isinstance(account.get("token"), str)
        and str(account.get("token")).strip()
        and str(account.get("phone") or account.get("mobile") or "").strip()
    )


class JsonWandaAccountSource:
    """Read the existing account pool without writing it or exposing it."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_ACCOUNT_POOL_PATH) -> None:
        self.path = Path(path)

    async def list_accounts(self) -> list[dict[str, Any]]:
        try:
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            payload = json.loads(raw.lstrip("\ufeff"))
        except (OSError, UnicodeError, ValueError):
            raise DirectGatewayError("account_pool_unavailable") from None
        records = payload.get("accounts") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            raise DirectGatewayError("account_pool_invalid")
        return [deepcopy(dict(item)) for item in records if isinstance(item, Mapping) and _eligible_account(item)]


def _validated_origin(value: str, expected_host: str) -> str:
    candidate = str(value).rstrip("/")
    parsed = urlsplit(candidate)
    if not (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and parsed.hostname in ALLOWED_HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    ):
        raise DirectGatewayError("official_origin_forbidden")
    return candidate


def _available_seat_ids(payload: Mapping[str, Any]) -> list[str]:
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            seat_id = value.get("seatId") or value.get("seat_id")
            status = value.get("status")
            if seat_id and status in (1, "1", "可选"):
                found.append(str(seat_id))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                visit(nested)

    visit(payload)
    return list(dict.fromkeys(found))


def _decrypt_official_data(value: str, client_key: str) -> Any:
    try:
        encrypted = bytes.fromhex(value)
    except ValueError:
        return None
    for key in (client_key[:16].encode(), APP_AES_KEY):
        try:
            decoded = AES.new(key, AES.MODE_ECB).decrypt(encrypted)
            try:
                decoded = unpad(decoded, AES.block_size)
            except ValueError:
                decoded = decoded.rstrip(b"\x00")
            return json.loads(decoded.decode("utf-8"))
        except Exception:
            continue
    return None


def _normalize_activities(payload: Any) -> list[dict[str, Any]]:
    groups = payload if isinstance(payload, list) else payload.get("res", payload) if isinstance(payload, Mapping) else []
    if not isinstance(groups, list):
        return []
    activities: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        items = group.get("groupItems")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            allot: dict[str, Any] = {}
            raw_allot = item.get("allotSeat")
            if isinstance(raw_allot, str) and raw_allot:
                try:
                    parsed = json.loads(raw_allot)
                    if isinstance(parsed, dict):
                        allot = parsed
                except ValueError:
                    pass
            elif isinstance(raw_allot, Mapping):
                allot = dict(raw_allot)
            activities.append({
                "group": str(group.get("groupName") or ""),
                "group_type": group.get("groupType", 0),
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or ""),
                "able": item.get("able") is True,
                "recommend": item.get("recommend") is True,
                "price": item.get("price", 0),
                "allot_seat": allot,
            })
    return activities


class WandaOfficialApiClient:
    """Signed Android-channel client restricted to five official operations."""

    def __init__(
        self,
        account: Mapping[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timestamp_factory: Any = None,
        front_origin: str = FRONT_ORIGIN,
        marketing_origin: str = MARKETING_ORIGIN,
        app_client_key: str | None = None,
        timeout_seconds: float = 18.0,
    ) -> None:
        self._token = str(account.get("token") or "").strip()
        self._mobile = str(account.get("phone") or account.get("mobile") or "").strip()
        user_info = account.get("user_info") if isinstance(account.get("user_info"), Mapping) else {}
        self._user = str(user_info.get("userIdentifier") or account.get("user_identifier") or "").strip()
        self._shumei = str(account.get("shumei_box_id") or "").strip()
        if not self._token or not self._mobile:
            raise DirectGatewayError("official_credentials_unavailable")
        self._front = _validated_origin(front_origin, "front-gateway-c.wandafilm.com")
        self._marketing = _validated_origin(marketing_origin, "mkt-activity-api-prd-mx.wandafilm.com")
        self._client_key = app_client_key or os.getenv("WANDA_DIRECT_APP_CLIENT_KEY", "").strip() or DEFAULT_APP_CLIENT_KEY
        self._transport = transport
        self._timestamp_factory = timestamp_factory or (lambda: int(time.time() * 1000))
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 4.0))

    def _sign(self, path_and_query: str, body: str = "", *, method: str) -> tuple[str, int]:
        timestamp = int(self._timestamp_factory())
        raw = f"{SALE_SUBJECT_CODE}{APP_CHANNEL}{self._client_key}{timestamp}{path_and_query}"
        if method == "POST":
            raw += body
        return hashlib.md5(raw.encode()).hexdigest(), timestamp

    def _headers(self, check: str, timestamp: int) -> dict[str, str]:
        mx = {
            "ver": APP_VERSION, "sCode": SALE_SUBJECT_CODE, "_mi_": self._token,
            "width": 1080, "json": True, "cCode": APP_CHANNEL, "check": check,
            "ts": timestamp, "height": 2244, "appId": 2, "model": APP_MODEL,
            "systemVersion": APP_SYSTEM_VERSION,
        }
        if self._shumei:
            mx["ShumeiBoxId"] = self._shumei
        headers = {
            "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "okhttp/4.12.0",
            "MX-API": json.dumps(mx, separators=(",", ":")), "X-RY-CHECK": check,
            "X-RY-CHANNEL": APP_CHANNEL, "X-RY-TIMESTAMP": str(timestamp),
            "X-RY-TOKEN": self._token, "X-RY-VERSION": APP_VERSION,
            "X-RY-MODEL": APP_MODEL, "X-RY-SYSTEM-VER": APP_SYSTEM_VERSION,
            "Accept-Charset": "UTF-8,*", "Accept-Encoding": "gzip", "Connection": "Keep-Alive",
        }
        if self._user:
            headers["X-RY-USER"] = self._user
        if self._shumei:
            headers["ShumeiBoxId"] = self._shumei
        return headers

    async def _send(self, method: str, origin: str, path: str, *, pairs: list[tuple[str, Any]], sign_encoded: bool = False, retryable_before_create: bool = False) -> Mapping[str, Any]:
        if urlsplit(origin).hostname not in ALLOWED_HOSTS or path.startswith("/api/order"):
            raise DirectGatewayError("official_origin_forbidden")
        if method == "GET":
            query = urlencode(pairs)
            target = f"{path}?{query}" if query else path
            check, timestamp = self._sign(target, method="GET")
            content = None
        else:
            raw_body = "&".join(f"{key}={value}" for key, value in pairs)
            encoded_body = "&".join(f"{key}={_lowercase_quote(str(value))}" for key, value in pairs)
            check, timestamp = self._sign(path, encoded_body if sign_encoded else raw_body, method="POST")
            target = path
            content = encoded_body
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                response = await client.request(method, f"{origin}{target}", content=content, headers=self._headers(check, timestamp))
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            if retryable_before_create and error.response.status_code in {401, 403}:
                raise DirectGatewayError("pre_create_account_unavailable", retryable_before_create=True) from None
            raise DirectGatewayError("official_http_failed") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise DirectGatewayError("official_http_failed") from None
        if not isinstance(payload, Mapping):
            raise DirectGatewayError("official_response_invalid")
        if retryable_before_create and str(payload.get("code") or payload.get("status") or "") in {"401", "403"}:
            raise DirectGatewayError("pre_create_account_unavailable", retryable_before_create=True)
        return payload

    async def create_order(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        payloads = request.get("seat_payloads") if isinstance(request.get("seat_payloads"), list) else request.get("seat_ids")
        seat_payload = "|".join(str(value) for value in (payloads or []))
        response = await self._send("POST", self._front, "/order/create_order.api", pairs=[
            ("retailerCode", "MX"), ("mobile", self._mobile), ("seatId", seat_payload),
            ("totalPrice", int(request.get("total_price_cents") or 0)), ("dId", str(request.get("showtime_id") or "")),
        ], sign_encoded=True, retryable_before_create=True)
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        order_id = data.get("orderId") or response.get("orderId")
        if not order_id:
            raise DirectGatewayError("temporary_lock_state_unknown")
        create_verified = response.get("code") in (0, "0") and data.get("bizCode") in (0, "0")
        return {"order_id": str(order_id), "create_verified": create_verified}

    async def order_status(self, order_id: str) -> Mapping[str, Any]:
        return await self._send("POST", self._front, "/order/order_status.api", pairs=[("json", "true"), ("orderId", order_id)])

    async def activity_offers(self, *, order_id: str, cinema_id: str, showtime_id: str, partition: str) -> Mapping[str, Any]:
        locked = False
        for attempt in range(3):
            status = await self.order_status(order_id)
            data = status.get("data") if isinstance(status.get("data"), Mapping) else {}
            try:
                lock_seat_time = int(data.get("lockSeatTime"))
            except (TypeError, ValueError):
                lock_seat_time = -1
            locked = str(data.get("orderStatus") or "") == "40" and lock_seat_time >= 0
            if locked:
                break
            if attempt < 2:
                await asyncio.sleep(1.0)
        if not locked:
            raise DirectGatewayError("temporary_lock_state_unknown")
        response = await self._send("GET", self._marketing, "/mkt/activity/secret/list.api", pairs=[
            ("partition", partition), ("orderId", order_id), ("did", showtime_id),
        ])
        raw_data = response.get("data")
        decoded = _decrypt_official_data(raw_data, self._client_key) if isinstance(raw_data, str) else raw_data
        if decoded is None:
            raise DirectGatewayError("official_response_invalid")
        return {"activities": _normalize_activities(decoded)}

    async def cancel_order(self, order_id: str) -> bool:
        response = await self._send("POST", self._front, "/order/cancel.api", pairs=[("orderId", order_id)])
        if response.get("code") not in (None, 0, "0") and response.get("success") is not True:
            return False
        status = await self.order_status(order_id)
        data = status.get("data") if isinstance(status.get("data"), Mapping) else {}
        value = str(data.get("orderStatus") or data.get("status") or "").strip().lower()
        try:
            lock_seat_time = int(data.get("lockSeatTime"))
        except (TypeError, ValueError):
            return False
        return value == "60" and lock_seat_time == -1

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]:
        response = await self._send("GET", self._front, "/order/real_time_seat.api", pairs=[("dId", showtime_id)])
        return {"available_seat_ids": _available_seat_ids(response)}
