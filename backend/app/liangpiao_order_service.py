from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .selected_seat_quote_service import SelectedSeat, SelectedSeatQuoteResult


class OrderServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, manual_hold: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.manual_hold = manual_hold


class LiangpiaoOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    shop_id: str | None = Field(default=None, max_length=200)
    buyer_id: str | None = Field(default=None, max_length=200)
    chat_id: str | None = Field(default=None, max_length=200)
    confirmation_id: str = Field(min_length=1, max_length=200)
    quote_id: str = Field(min_length=1, max_length=160)
    quote_hash: str = Field(min_length=64, max_length=64)
    latest_buyer_message: str = Field(min_length=1, max_length=2000)
    buyer_phone: str = Field(min_length=11, max_length=20)
    generation: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=120)
    buyer_confirmed: bool = False
    allow_seat_change: bool = False


class LiangpiaoOrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    code: str
    out_order_no: str
    provider_order_no: str | None = None
    quote_id: str
    quote_hash: str
    payload_hash: str
    provider_status: str | None = None
    trace_id: str
    raw: dict[str, Any] = Field(default_factory=dict)


class LiangpiaoOrderClient(Protocol):
    async def order_list(self, **kwargs: Any) -> Mapping[str, Any]: ...
    async def order_create(self, **kwargs: Any) -> Mapping[str, Any]: ...
    async def order_detail(self, **kwargs: Any) -> Mapping[str, Any]: ...


class LiangpiaoOrderService:
    """Confirmation and order gate. Prices and seats are always read from a quote snapshot."""

    def __init__(self, client: LiangpiaoOrderClient, *, quote_store: object | None = None,
                 order_store: object | None = None, order_create_enabled: bool = False,
                 external_writes_enabled: bool = False) -> None:
        self._client = client
        self._quote_store = quote_store
        self._order_store = order_store
        self._order_create_enabled = bool(order_create_enabled)
        self._external_writes_enabled = bool(external_writes_enabled)
        self._quotes: dict[str, SelectedSeatQuoteResult | Mapping[str, Any]] = {}
        self._orders: dict[str, LiangpiaoOrderResult] = {}

    def register_quote(self, quote: SelectedSeatQuoteResult | Mapping[str, Any]) -> None:
        quote_id = str(quote.quote_id if isinstance(quote, SelectedSeatQuoteResult) else quote.get("quote_id") or "").strip()
        if quote_id:
            self._quotes[quote_id] = quote

    async def create(self, request: LiangpiaoOrderRequest | Mapping[str, Any],
                    quote: SelectedSeatQuoteResult | Mapping[str, Any] | None = None) -> LiangpiaoOrderResult:
        req = request if isinstance(request, LiangpiaoOrderRequest) else LiangpiaoOrderRequest.model_validate(request)
        if not self._order_create_enabled or not self._external_writes_enabled:
            raise OrderServiceError("LIANGPIAO_ORDER_CREATE_DISABLED", "良票真实下单开关未开启。", manual_hold=False)
        snapshot = quote or self._quotes.get(req.quote_id)
        if snapshot is None:
            getter = getattr(self._quote_store, "get_selected_seat_quote", None)
            snapshot = getter(req.quote_id) if callable(getter) else None
        if snapshot is None:
            raise OrderServiceError("LIANGPIAO_QUOTE_NOT_FOUND", "报价快照不存在。")
        values = snapshot.model_dump(mode="json") if isinstance(snapshot, SelectedSeatQuoteResult) else dict(snapshot)
        owner_tenant = str(values.get("tenant_id") or values.get("snapshot", {}).get("tenant_id") or "")
        owner_conversation = str(values.get("conversation_id") or values.get("snapshot", {}).get("conversation_id") or "")
        if owner_tenant and owner_tenant != req.tenant_id:
            raise OrderServiceError("LIANGPIAO_QUOTE_OWNER_MISMATCH", "报价不属于当前租户。")
        if owner_conversation and owner_conversation != req.conversation_id:
            raise OrderServiceError("LIANGPIAO_QUOTE_OWNER_MISMATCH", "报价不属于当前会话。")
        if str(values.get("quote_id") or req.quote_id) != req.quote_id or str(values.get("quote_hash") or "") != req.quote_hash:
            raise OrderServiceError("LIANGPIAO_QUOTE_HASH_MISMATCH", "报价快照已变更，请重新报价。")
        expires_at = self._parse_time(values.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise OrderServiceError("LIANGPIAO_QUOTE_EXPIRED", "报价已过期，请重新获取实时报价。")
        if int(values.get("generation") or 0) != req.generation:
            raise OrderServiceError("LIANGPIAO_QUOTE_GENERATION_MISMATCH", "报价已失效，请重新确认。")
        if not req.buyer_confirmed or not _is_explicit_confirmation(req.latest_buyer_message):
            raise OrderServiceError("LIANGPIAO_CONFIRMATION_REQUIRED", "需要买家明确确认后才能下单。")
        if not re.fullmatch(r"1[3-9]\d{9}", req.buyer_phone):
            raise OrderServiceError("LIANGPIAO_PHONE_INVALID", "买家手机号格式不合法。")
        if values.get("preflight_verified") is not True:
            raise OrderServiceError("LIANGPIAO_PREFLIGHT_REQUIRED", "报价尚未通过预检。")

        out_order_no = "lp-" + hashlib.sha256(f"{req.tenant_id}\0{req.quote_id}\0{req.quote_hash}".encode()).hexdigest()[:32]
        previous = self._orders.get(out_order_no)
        if previous is not None:
            return previous
        persisted_getter = getattr(self._order_store, "find_liangpiao_order", None)
        persisted = persisted_getter(out_order_no=out_order_no) if callable(persisted_getter) else None
        if isinstance(persisted, Mapping) and _provider_order_id(persisted):
            previous = LiangpiaoOrderResult(
                status="ok", code="ORDER_CREATED", out_order_no=out_order_no,
                provider_order_no=_provider_order_id(persisted), quote_id=req.quote_id,
                quote_hash=req.quote_hash, payload_hash=str(persisted.get("payload_hash") or ""),
                provider_status=str(persisted.get("provider_status") or "created"),
                trace_id=req.trace_id, raw=dict(persisted.get("raw") or {}),
            )
            self._orders[out_order_no] = previous
            return previous
        seats = values.get("seats") or []
        payload = {
            "showId": values.get("show_id"),
            "seats": [_seat_payload(item) for item in seats],
            "ticketMode": values.get("ticket_mode", "STANDARD"),
            "priceMode": values.get("price_mode", "FIXED"),
            "outOrderNo": out_order_no,
            "tel": req.buyer_phone,
            "maxPrice": int(values.get("buyer_amount_fen") or 0),
            "allowSeatChange": bool(req.allow_seat_change),
            "attach": req.confirmation_id,
            "traceId": req.trace_id,
        }
        try:
            response = await self._client.order_create(**payload)
        except Exception as error:
            # A transport timeout is not safe to retry blindly. Reconcile first.
            try:
                detail = await self._client.order_detail(out_order_no=out_order_no, show_id=values.get("show_id"))
            except Exception:
                detail = {}
            if _provider_order_id(detail):
                response = detail
            else:
                raise OrderServiceError("LIANGPIAO_PROVIDER_UNKNOWN", "良票下单结果未知，已转人工核对。") from error
        provider_order_no = _provider_order_id(response)
        if not provider_order_no:
            raise OrderServiceError("LIANGPIAO_ORDER_CREATE_INVALID", "良票下单未返回订单号。")
        provider_status = str(response.get("status") or response.get("orderStatus") or "created")
        result = LiangpiaoOrderResult(
            status="ok", code="ORDER_CREATED", out_order_no=out_order_no,
            provider_order_no=provider_order_no, quote_id=req.quote_id, quote_hash=req.quote_hash,
            payload_hash=hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest(),
            provider_status=provider_status, trace_id=req.trace_id, raw=dict(response),
        )
        self._orders[out_order_no] = result
        save = getattr(self._order_store, "save_liangpiao_order", None)
        if callable(save):
            save({**result.model_dump(), "tenant_id": req.tenant_id, "conversation_id": req.conversation_id,
                  "shop_id": req.shop_id, "buyer_id": req.buyer_id, "chat_id": req.chat_id, "payload": payload})
        return result

    async def create_order(self, request: LiangpiaoOrderRequest | Mapping[str, Any],
                           quote: SelectedSeatQuoteResult | Mapping[str, Any] | None = None) -> LiangpiaoOrderResult:
        return await self.create(request, quote)

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_explicit_confirmation(text: str) -> bool:
    return bool(re.search(r"(?:确认|确定|可以下单|要了|下单吧|同意).*(?:下单|出票|购买)?|下单|出票", text.strip(), re.I))


def _seat_payload(value: object) -> dict[str, Any]:
    if isinstance(value, SelectedSeat):
        return {"rowNo": value.row_no, "colNo": value.col_no, "seatNo": value.seat_no, "areaId": value.area_id}
    if isinstance(value, Mapping):
        return {"rowNo": value.get("row_no", value.get("rowNo")), "colNo": value.get("col_no", value.get("colNo")),
                "seatNo": value.get("seat_no", value.get("seatNo")), "areaId": value.get("area_id", value.get("areaId"))}
    return {"rowNo": getattr(value, "row_no", None), "colNo": getattr(value, "col_no", None),
            "seatNo": getattr(value, "seat_no", None), "areaId": getattr(value, "area_id", None)}


def _provider_order_id(value: Mapping[str, Any] | None) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("providerOrderNo", "provider_order_no", "orderNo", "orderId", "order_id"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return None
