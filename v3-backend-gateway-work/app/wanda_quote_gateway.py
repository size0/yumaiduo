from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from fastapi import HTTPException, status

from .schemas import Recognition
from .wanda_quote_domain import _text


def _showtime_start(value: str | None) -> str | None:
    if not value:
        return None
    matched = re.match(r"^(\d{2}:\d{2})", value.strip())
    return matched.group(1) if matched else value


def _gateway_showtime_hint(recognition: Recognition) -> str | None:
    start = _showtime_start(recognition.showtime)
    if recognition.date and start:
        return f"{recognition.date.isoformat()} {start}"
    return start


def _gateway_match_text(hints: Mapping[str, Any]) -> str:
    labels = (("城市", "city"), ("影院", "cinema"), ("电影", "movie"), ("场次", "showtime"), ("影厅", "hall"))
    return "\n".join(f"{label}：{value}" for label, key in labels if (value := _text(hints.get(key))))


def _gateway_auth_headers() -> dict[str, str]:
    """Authenticate V3-to-ticket-gateway calls without exposing an operator session."""
    key = os.getenv("WANDA_QUOTE_GATEWAY_KEY", "").strip()
    return {"X-Plugin-Bridge-Key": key} if key else {}


class TicketGateway(Protocol):
    async def for_quote(self) -> "TicketGateway": ...

    def account_mobile(self) -> str: ...

    async def match(self, recognition: Recognition) -> Mapping[str, Any]: ...

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]: ...

    async def lock(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def available_offers(self, *, cinema_id: str, showtime_id: str, partition: str, order_id: str) -> Mapping[str, Any]: ...

    async def cancel(self, order_id: str) -> bool: ...


class LocalTicketGateway:
    """Adapter for the locally deployed ticket gateway; it owns Wanda signing/token use."""

    def __init__(self, base_url: str | None = None, account_phone: str | None = None) -> None:
        self._base_url = (base_url or os.getenv("WANDA_QUOTE_GATEWAY_URL", "http://127.0.0.1:8000")).rstrip("/")
        self._account_phone = (account_phone or os.getenv("WANDA_ACCOUNT_PHONE", "")).strip()

    def _require_account_phone(self) -> str:
        if not self._account_phone:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="万达核价账号尚未配置")
        return self._account_phone

    def account_mobile(self) -> str:
        return self._require_account_phone()

    async def for_quote(self) -> TicketGateway:
        if self._account_phone:
            return self
        phone = await self._select_online_wplus_account_phone()
        return LocalTicketGateway(self._base_url, phone)

    async def _select_online_wplus_account_phone(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=4)) as client:
                response = await client.get(
                    f"{self._base_url}/api/auth/internal/wplus-accounts",
                    headers=_gateway_auth_headers(),
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=502, detail=f"万达账号池网关返回 HTTP {error.response.status_code}") from error
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="万达账号池网关连接失败") from error
        accounts = payload.get("accounts") if isinstance(payload, Mapping) else None
        if not isinstance(accounts, list):
            raise HTTPException(status_code=502, detail="万达账号池返回格式无效")
        candidates: list[tuple[int, str]] = []
        for account in accounts:
            if not isinstance(account, Mapping) or account.get("available") is not True:
                continue
            phone = _text(account.get("phone"))
            remaining = account.get("remaining")
            remaining_count = remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else 0
            if phone:
                candidates.append((remaining_count, phone))
        if candidates:
            return sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]
        raise HTTPException(status_code=422, detail="线上账号池没有可用的 W+ 会员账号")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            headers = _gateway_auth_headers()
            extra_headers = kwargs.pop("headers", None)
            if isinstance(extra_headers, Mapping):
                headers.update({str(key): str(value) for key, value in extra_headers.items()})
            async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=4)) as client:
                response = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=502, detail=f"万达核价网关返回 HTTP {error.response.status_code}") from error
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="万达核价网关连接失败") from error
        if not isinstance(body, Mapping):
            raise HTTPException(status_code=502, detail="万达核价网关返回格式无效")
        return body

    async def match(self, recognition: Recognition) -> Mapping[str, Any]:
        hints = {
            "city": recognition.city,
            "cinema": recognition.cinema,
            "movie": recognition.movie,
            "showtime": _gateway_showtime_hint(recognition),
            "hall": recognition.hall,
            "seats": recognition.official_selection.selected_seat_numbers,
        }
        text = _gateway_match_text(hints)
        return await self._request(
            "POST",
            "/api/order/match",
            json={"text": text, "mode": "screenshot", "phone": self._require_account_phone(), "auto_select_seats": False, "hints": hints},
        )

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]:
        return await self._request("GET", "/api/showtime/seats", params={"showtimeId": showtime_id, "phone": self._require_account_phone()})

    async def lock(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._request("POST", "/api/order/create-ticket-flow", json=payload)

    async def available_offers(self, *, cinema_id: str, showtime_id: str, partition: str, order_id: str) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            "/api/order/available-offers",
            params={
                "cinemaId": cinema_id,
                "showtimeId": showtime_id,
                "partition": partition,
                "orderId": order_id,
                "phone": self._require_account_phone(),
                "includeYqk": "false",
            },
        )

    async def cancel(self, order_id: str) -> bool:
        try:
            result = await self._request(
                "POST",
                "/api/order/cancel",
                json={"order_id": order_id, "phone": self._require_account_phone()},
            )
        except HTTPException:
            return False
        return result.get("code") in (None, 0, "0") or result.get("success") is True


gateway_auth_headers = _gateway_auth_headers
