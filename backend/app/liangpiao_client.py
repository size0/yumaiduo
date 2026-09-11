from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
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
        "ping": "ping",
        "city_list": "city/list",
        "city_locate": "city/locate",
        "region_list": "region/list",
        "recognize": "recognize/seat-shot",
        "recognize_async": "recognize/seat-shot/async",
        "task_detail": "recognize/task/detail",
        "confirm": "recognize/confirm",
        "brand_list": "brand/list",
        "cinema_list": "cinema/list",
        "cinema_detail": "cinema/detail",
        "movie_list": "movie/list",
        "movie_detail": "movie/detail",
        "show_dates": "show/dates",
        "show_list": "show/list",
        "show_detail": "show/detail",
        "seat_list": "seat/list",
        "order_preflight": "order/preflight",
        "order_list": "order/list",
        "order_create": "order/create",
        "order_detail": "order/detail",
        "order_cancel": "order/cancel",
        "order_urge": "order/urge",
        "order_refund": "order/refund",
        "order_refund_detail": "order/refund/detail",
        "account_balance": "account/balance",
        "account_transaction_list": "account/transaction/list",
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
        timeout_seconds: float | None = None,
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
            request_options: dict[str, Any] = {
                "content": body.encode("utf-8"),
                "headers": headers,
            }
            if timeout_seconds is not None:
                request_options["timeout"] = timeout_seconds
            response = await self._client.post(
                f"{self._base_url}/{path}", **request_options,
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
            "hall_name": "hallName", "city_name": "cityName", "city_code": "cityCode",
            "district_code": "districtCode", "parent_code": "parentCode",
            "movie_id": "movieId", "page_size": "pageSize", "order_no": "orderNo",
            "movie_type": "type",
            "area_quote_strategy": "areaQuoteStrategy",
        }
        return {aliases.get(str(key), key): value for key, value in merged.items()}

    @staticmethod
    def _optional_text(value: Any, name: str, *, max_length: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"liangpiao_{name}_invalid")
        return text

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"liangpiao_{name}_invalid")
        return text

    @staticmethod
    def _region_code(value: Any, name: str) -> str:
        code = str(value or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"liangpiao_{name}_invalid")
        return code

    @staticmethod
    def _coordinate(value: Any, name: str, *, lower: float, upper: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"liangpiao_{name}_invalid") from None
        if isinstance(value, bool) or not math.isfinite(parsed) or not lower <= parsed <= upper:
            raise ValueError(f"liangpiao_{name}_invalid")
        return parsed

    @staticmethod
    def _bounded_int(value: Any, name: str, *, lower: int, upper: int) -> int:
        if isinstance(value, bool) or not (
            isinstance(value, int)
            or (isinstance(value, str) and re.fullmatch(r"\+?\d+", value.strip()))
        ):
            raise ValueError(f"liangpiao_{name}_invalid")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"liangpiao_{name}_invalid") from None
        if parsed < lower or parsed > upper:
            raise ValueError(f"liangpiao_{name}_invalid")
        return parsed

    async def city_list(
        self, *, keyword: str | None = None, trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        normalized = self._optional_text(keyword, "keyword", max_length=50)
        if normalized is not None:
            payload["keyword"] = normalized
        return await self.request("city_list", payload, trace_id=trace_id)

    async def ping(self, *, trace_id: str | None = None) -> dict[str, Any]:
        return await self.request("ping", {}, trace_id=trace_id)

    async def city_locate(
        self, *, longitude: float, latitude: float, trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "longitude": self._coordinate(longitude, "longitude", lower=-180, upper=180),
            "latitude": self._coordinate(latitude, "latitude", lower=-90, upper=90),
        }
        return await self.request("city_locate", payload, trace_id=trace_id)

    async def region_list(
        self, *, parent_code: str | None = None, trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if parent_code is not None:
            payload["parentCode"] = self._region_code(parent_code, "parent_code")
        return await self.request("region_list", payload, trace_id=trace_id)

    async def recognize(
        self, image_url: str, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request(
            "recognize", {"imageUrl": image_url, **kwargs},
            trace_id=trace_id, idempotency_key=idempotency_key,
            timeout_seconds=5.0,
        )

    async def recognize_async(
        self, image_url: str, *, out_trade_no: str | None = None,
        notify_url: str | None = None, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"imageUrl": image_url, **kwargs}
        if out_trade_no:
            payload["outTradeNo"] = out_trade_no
        if notify_url:
            payload["notifyUrl"] = notify_url
        return await self.request(
            "recognize_async", payload,
            trace_id=trace_id, idempotency_key=idempotency_key or out_trade_no,
        )

    async def task_detail(
        self, task_id: str | int, *, trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "task_detail", {"taskId": str(task_id)},
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    async def confirm(
        self, recognize_id: str, cinema_id: int | None = None, *,
        movie_id: int | None = None, show_id: str | None = None,
        city_name: str | None = None, trace_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        recognition = str(recognize_id or "").strip()
        if not recognition or len(recognition) > 100:
            raise ValueError("liangpiao_recognize_id_invalid")
        payload: dict[str, Any] = {"recognizeId": recognition}
        if cinema_id is not None:
            payload["cinemaId"] = self._positive_id(cinema_id, "cinema")
        if movie_id is not None:
            payload["movieId"] = self._positive_id(movie_id, "movie")
        if show_id is not None:
            show = str(show_id).strip()
            if not show or len(show) > 100 or not show.isdecimal():
                raise ValueError("liangpiao_show_id_invalid")
            payload["showId"] = show
        if not any(key in payload for key in ("cinemaId", "movieId", "showId")):
            raise ValueError("liangpiao_candidate_id_required")
        if city_name is not None:
            city = str(city_name).strip()
            if city:
                if len(city) > 50:
                    raise ValueError("liangpiao_city_name_invalid")
                payload["cityName"] = city
        return await self.request(
            "confirm", payload,
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    @staticmethod
    def _positive_id(value: Any, name: str) -> int:
        if isinstance(value, bool) or not (
            isinstance(value, int)
            or (isinstance(value, str) and re.fullmatch(r"\+?\d+", value.strip()))
        ):
            raise ValueError(f"liangpiao_{name}_id_invalid")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"liangpiao_{name}_id_invalid") from None
        if parsed <= 0:
            raise ValueError(f"liangpiao_{name}_id_invalid")
        return parsed

    async def brand_list(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("brand_list", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def cinema_list(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("cinema_list", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def cinema_detail(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("cinema_detail", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def movie_list(
        self, *, movie_type: str | None = None, city_code: str | None = None,
        keyword: str | None = None, page: int | None = None,
        page_size: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if movie_type is not None:
            normalized_type = str(movie_type).strip().upper()
            if normalized_type not in {"HOT", "COMING"}:
                raise ValueError("liangpiao_movie_type_invalid")
            payload["type"] = normalized_type
        if city_code is not None:
            payload["cityCode"] = self._region_code(city_code, "city_code")
        normalized_keyword = self._optional_text(keyword, "keyword", max_length=50)
        if normalized_keyword is not None:
            payload["keyword"] = normalized_keyword
        if page is not None:
            payload["page"] = self._bounded_int(
                page, "page", lower=1, upper=2_147_483_647,
            )
        if page_size is not None:
            payload["pageSize"] = self._bounded_int(
                page_size, "page_size", lower=1, upper=200,
            )
        return await self.request("movie_list", payload, trace_id=trace_id)

    async def movie_detail(
        self, movie_id: int, *, trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "movie_detail", {"movieId": self._positive_id(movie_id, "movie")},
            trace_id=trace_id,
        )

    async def show_dates(
        self, cinema_id: int, *, movie_id: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cinemaId": self._positive_id(cinema_id, "cinema"),
        }
        if movie_id is not None:
            payload["movieId"] = self._positive_id(movie_id, "movie")
        return await self.request("show_dates", payload, trace_id=trace_id)

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

    async def order_list(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.request("order_list", self._payload(payload, kwargs), trace_id=trace_id, idempotency_key=idempotency_key)

    async def order_create(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create an order from either a payload mapping or keyword fields."""
        values = dict(payload or {})
        values.update(kwargs)
        body_trace_id = values.pop("traceId", None)
        values.pop("trace_id", None)
        transport_trace_id = trace_id or self._optional_text(
            body_trace_id, "trace_id", max_length=128,
        )
        key = str(idempotency_key or values.get("outOrderNo") or "").strip() or None
        return await self.request(
            "order_create", values,
            trace_id=transport_trace_id, idempotency_key=key,
        )

    async def order_detail(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        values = self._payload(payload, kwargs)
        if set(values) != {"orderNo"}:
            raise ValueError("liangpiao_order_detail_payload_invalid")
        values["orderNo"] = self._required_text(values["orderNo"], "order_no")
        return await self.request(
            "order_detail", values,
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    async def order_cancel(
        self, payload: Mapping[str, Any] | None = None, *, trace_id: str | None = None,
        idempotency_key: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        values = self._payload(payload, kwargs)
        if "orderNo" not in values or set(values) - {"orderNo", "reason"}:
            raise ValueError("liangpiao_order_cancel_payload_invalid")
        values["orderNo"] = self._required_text(values["orderNo"], "order_no")
        if "reason" in values:
            reason = self._optional_text(
                values["reason"], "cancel_reason", max_length=255,
            )
            if reason is None:
                values.pop("reason")
            else:
                values["reason"] = reason
        return await self.request(
            "order_cancel", values,
            trace_id=trace_id, idempotency_key=idempotency_key,
        )

    async def order_urge(
        self, order_no: str, *, trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit one urge request; the provider does not document it as idempotent."""
        return await self.request(
            "order_urge", {"orderNo": self._required_text(order_no, "order_no")},
            trace_id=trace_id,
        )

    async def order_refund(
        self, order_no: str, *, reason: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply for an asynchronous refund; callers must reconcile before retrying."""
        payload: dict[str, Any] = {
            "orderNo": self._required_text(order_no, "order_no"),
        }
        normalized_reason = self._optional_text(reason, "refund_reason", max_length=255)
        if normalized_reason is not None:
            payload["reason"] = normalized_reason
        return await self.request("order_refund", payload, trace_id=trace_id)

    async def order_refund_detail(
        self, order_no: str, *, trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "order_refund_detail",
            {"orderNo": self._required_text(order_no, "order_no")},
            trace_id=trace_id,
        )

    async def account_balance(
        self, *, trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.request("account_balance", {}, trace_id=trace_id)

    async def account_transaction_list(
        self, *, page: int | None = None, page_size: int | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if page is not None:
            payload["page"] = self._bounded_int(
                page, "page", lower=1, upper=2_147_483_647,
            )
        if page_size is not None:
            payload["pageSize"] = self._bounded_int(
                page_size, "page_size", lower=1, upper=100,
            )
        return await self.request(
            "account_transaction_list", payload, trace_id=trace_id,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# Descriptive alias for callers that prefer the transport-oriented name.
LiangpiaoApiClient = LiangpiaoClient
