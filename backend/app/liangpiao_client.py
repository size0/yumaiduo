from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

from .config import Settings
from .errors import ProviderError


class LiangpiaoClient:
    """Signed, allow-listed HTTP client for the Liangpiao ticket APIs.

    The client deliberately has no arbitrary URL method.  Every request carries
    a trace id and (when supplied) an idempotency key so callers can correlate
    retries without putting credentials or full request URLs in logs.
    """

    ENDPOINTS = {
        "recognize": "recognize/seat-shot",
        "confirm": "recognize/confirm",
        "show_list": "show/list",
        "show_detail": "show/detail",
        "seat_list": "seat/list",
        "order_preflight": "order/preflight",
        "order_create": "order/create",
        "order_detail": "order/detail",
        "order_cancel": "order/cancel",
    }

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        configured = settings
        raw_base = base_url if base_url is not None else getattr(configured, "liangpiao_base_url", "")
        self._base_url = self._normalize_base_url(raw_base)
        self._app_key = (app_key if app_key is not None else getattr(configured, "liangpiao_app_key", "")).strip()
        self._app_secret = (app_secret if app_secret is not None else getattr(configured, "liangpiao_app_secret", "")).strip()
        timeout = float(getattr(configured, "liangpiao_request_timeout_seconds", 20))
        if not self._app_key or not self._app_secret:
            raise ValueError("Liangpiao credentials are required")
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5), follow_redirects=False,
        )
        self._owns_client = http_client is None

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        normalized = str(value or "").strip().rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("LIANGPIAO_BASE_URL must use HTTPS")
        return normalized if normalized.endswith("/api/v1") else f"{normalized}/api/v1"

    @classmethod
    def _endpoint(cls, name: str) -> str:
        try:
            return cls.ENDPOINTS[name]
        except KeyError:
            raise ValueError("liangpiao_endpoint_not_allowed") from None

    async def request(
        self,
        endpoint: str,
        payload: Mapping[str, Any] | None = None,
        *,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        path = self._endpoint(endpoint) if endpoint in self.ENDPOINTS else endpoint.strip("/")
        if path not in self.ENDPOINTS.values():
            raise ValueError("liangpiao_endpoint_not_allowed")
        body_payload = dict(payload or {})
        body = json.dumps(body_payload, ensure_ascii=False, separators=(",", ":"))
        trace = str(trace_id or uuid.uuid4().hex).strip()
        if not trace or len(trace) > 128:
            raise ValueError("liangpiao_trace_id_invalid")
        idem = str(idempotency_key or "").strip()
        if len(idem) > 240:
            raise ValueError("liangpiao_idempotency_key_invalid")
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature = hmac.new(
            self._app_secret.encode("utf-8"),
            f"{self._app_key}{timestamp}{nonce}{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-app-key": self._app_key,
            "x-timestamp": timestamp,
            "x-nonce": nonce,
            "x-sign": signature,
            "x-trace-id": trace,
        }
        if idem:
            headers["idempotency-key"] = idem
            headers["x-idempotency-key"] = idem
        try:
            response = await self._client.post(
                f"{self._base_url}/{path}", content=body.encode("utf-8"), headers=headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise ProviderError("liangpiao_network_error", "良票服务暂时不可用，请稍后重试。") from error
        try:
            parsed = response.json()
        except ValueError as error:
            raise ProviderError("liangpiao_invalid_response", "良票服务返回格式异常。") from error
        if not isinstance(parsed, Mapping):
            raise ProviderError("liangpiao_invalid_response", "良票服务返回格式异常。")
        if not response.is_success:
            raise ProviderError("liangpiao_http_error", "良票服务暂时不可用，请稍后重试。")
        code = parsed.get("code")
        if code not in (None, 0, "0"):
            result = parsed.get("message") or parsed.get("msg") or "良票业务请求未完成。"
            raise ProviderError(f"liangpiao_business_{code}", str(result)[:200])
        data = parsed.get("data")
        result = dict(data) if isinstance(data, Mapping) else {}
        result["raw_response"] = dict(parsed)
        result["trace_id"] = trace
        result["request_id"] = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or parsed.get("requestId")
            or parsed.get("request_id")
        )
        result["http_status"] = response.status_code
        return result

    @staticmethod
    def _payload(payload: Mapping[str, Any] | None, extras: Mapping[str, Any]) -> dict[str, Any]:
        merged = dict(payload or {})
        merged.update(extras)
        aliases = {
            "cinema_id": "cinemaId", "show_id": "showId", "out_order_no": "outOrderNo",
            "provider_order_no": "providerOrderNo", "recognize_id": "recognizeId",
            "ticket_mode": "ticketMode", "price_mode": "priceMode", "trace_id": "traceId",
            "show_date": "showDate", "movie_name": "movieName", "showtime_start": "showtimeStart",
            "hall_name": "hallName", "city_name": "cityName",
        }
        return {aliases.get(str(key), key): value for key, value in merged.items()}

    async def recognize(
        self, image_url: str, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request(
            "recognize", {"imageUrl": image_url, **kwargs},
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    async def confirm(
        self, recognize_id: str, cinema_id: int, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request(
            "confirm", {"recognizeId": recognize_id, "cinemaId": cinema_id, **kwargs},
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    async def show_list(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("show_list", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def show_detail(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("show_detail", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def seat_list(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("seat_list", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def order_preflight(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("order_preflight", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def order_create(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create an order from either a payload mapping or keyword fields."""
        values = dict(payload or {})
        values.update(kwargs)
        key = str(idempotency_key or values.get("outOrderNo") or "").strip() or None
        return await self.request("order_create", values, trace_id=trace_id, idempotency_key=key)

    async def order_detail(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("order_detail", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def order_cancel(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("order_cancel", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# Descriptive alias for callers that prefer the transport-oriented name.
LiangpiaoApiClient = LiangpiaoClient
