from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .errors import ProviderError
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
    # QuoteRecord lineage hashes are terms fingerprints (not necessarily
    # provider SHA-256 quote hashes); the provider snapshot still carries its
    # own provider_quote_hash separately.
    quote_hash: str = Field(min_length=1, max_length=160)
    latest_buyer_message: str = Field(min_length=1, max_length=2000)
    buyer_phone: str = Field(min_length=11, max_length=20)
    generation: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=120)
    buyer_confirmed: bool = False
    allow_seat_change: bool = False
    platform_order_id: str | None = Field(default=None, max_length=240)
    binding_revision: int | None = Field(default=None, ge=1)
    fulfillment_attempt: int = Field(default=0, ge=0, le=1)


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
                 external_writes_enabled: bool = False, quote_record_store: object | None = None,
                 binding_service: object | None = None, state_store: object | None = None,
                 preflight_service: object | None = None,
                 now_provider: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._quote_store = quote_store
        self._order_store = order_store
        self._order_create_enabled = bool(order_create_enabled)
        self._external_writes_enabled = bool(external_writes_enabled)
        self._quote_records = quote_record_store
        self._binding_service = binding_service
        self._state_store = state_store
        self._preflight_service = preflight_service
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
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
        # The rule engine supplies the authoritative confirmation flag. Do not
        # re-guess buyer intent from free-form text at the order boundary.
        if not req.buyer_confirmed:
            raise OrderServiceError("LIANGPIAO_CONFIRMATION_REQUIRED", "需要买家明确确认后才能下单。")
        if not re.fullmatch(r"1[3-9]\d{9}", req.buyer_phone):
            raise OrderServiceError("LIANGPIAO_PHONE_INVALID", "买家手机号格式不合法。")
        if values.get("preflight_verified") is not True:
            raise OrderServiceError("LIANGPIAO_PREFLIGHT_REQUIRED", "报价尚未通过预检。")

        out_order_no = _out_order_no(req)
        previous = self._orders.get(out_order_no)
        if previous is not None:
            return previous
        persisted_getter = getattr(self._order_store, "find_liangpiao_order", None)
        if callable(persisted_getter):
            try:
                persisted = persisted_getter(out_order_no=out_order_no, tenant_id=req.tenant_id)
            except TypeError:
                persisted = persisted_getter(out_order_no=out_order_no)
        else:
            persisted = None
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
        payload = _create_payload(values, req, out_order_no)
        area_strategy = values.get("area_quote_strategy")
        if area_strategy in {"AVERAGE", "HIGHEST", "LOWEST"}:
            payload["areaQuoteStrategy"] = area_strategy
        save = getattr(self._order_store, "save_liangpiao_order", None)
        if callable(save):
            save({
                "flow_version": "LEGACY_LIANGPIAO_ORDER" if req.platform_order_id is None else "V4_LIANGPIAO_FULFILLMENT_V1",
                "out_order_no": out_order_no, "tenant_id": req.tenant_id,
                "shop_id": req.shop_id, "buyer_id": req.buyer_id, "chat_id": req.chat_id,
                "platform_order_id": req.platform_order_id, "quote_id": req.quote_id,
                "quote_hash": req.quote_hash, "quote_generation": req.generation,
                "binding_revision": req.binding_revision, "attempt_no": req.fulfillment_attempt,
                "price_mode": values.get("price_mode"), "ticket_mode": values.get("ticket_mode"),
                "payload": payload, "payload_hash": _payload_hash(payload),
                "provider_status": "SUBMITTING", "created_at": self._now_provider().isoformat(),
            })
        try:
            response = await self._client.order_create(**payload)
        except Exception as error:
            if not _is_unknown_create_result(error):
                raise _order_create_rejected(error) from error
            # Liangpiao documents order/create as idempotent on outOrderNo.
            # order/detail accepts only the provider orderNo, which is not
            # available when the first create response is lost.  Replaying the
            # exact same create payload is therefore the only documented way
            # to reconcile an unknown create result without creating a second
            # order.
            try:
                response = await self._client.order_create(**payload)
            except Exception as retry_error:
                if not _is_unknown_create_result(retry_error):
                    raise _order_create_rejected(retry_error) from retry_error
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
        order_snapshot = {**result.model_dump(), "tenant_id": req.tenant_id, "conversation_id": req.conversation_id,
                          "shop_id": req.shop_id, "buyer_id": req.buyer_id, "chat_id": req.chat_id, "payload": payload,
                          "platform_order_id": req.platform_order_id, "quote_generation": req.generation,
                          "binding_revision": req.binding_revision, "attempt_no": req.fulfillment_attempt,
                          "price_mode": values.get("price_mode"), "ticket_mode": values.get("ticket_mode"),
                          "provider_amount_fen": values.get("provider_amount_fen")}
        update = getattr(self._order_store, "update_liangpiao_order", None)
        if callable(update):
            update(out_order_no, provider_order_no=provider_order_no,
                   provider_status=provider_status, snapshot_updates=order_snapshot)
        else:
            save = getattr(self._order_store, "save_liangpiao_order", None)
            if callable(save):
                save(order_snapshot)
        return result

    async def create_order(self, request: LiangpiaoOrderRequest | Mapping[str, Any],
                           quote: SelectedSeatQuoteResult | Mapping[str, Any] | None = None) -> LiangpiaoOrderResult:
        return await self.create(request, quote)

    async def fulfill_payment_validated(
        self, body: Mapping[str, Any], payment_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Fulfill one already validated Liangpiao payment.

        Payment validation remains the only entry authority.  This method is
        intentionally an extension of the existing order service: it reuses
        the bound QuoteRecord, RulesFirst state/order tables and current
        selected-seat preflight service, while never reading buyer text for
        seat or quote decisions.
        """
        if payment_result.get("validation_status") != "VERIFIED_PAID" or payment_result.get("duplicate"):
            return {"status": "SKIPPED", "reason": "PAYMENT_NOT_VALIDATED"}
        identity, order_id = _payment_identity(body)
        if identity is None or order_id is None:
            return await self._fulfillment_hold(None, "LIANGPIAO_FULFILLMENT_IDENTITY_UNVERIFIED")
        if not self._order_create_enabled or not self._external_writes_enabled:
            return await self._fulfillment_hold(identity, "LIANGPIAO_ORDER_CREATE_DISABLED")
        if self._binding_service is None or self._quote_records is None or self._state_store is None:
            return await self._fulfillment_hold(identity, "LIANGPIAO_FULFILLMENT_COMPOSITION_INCOMPLETE")
        current = self._state_store.get(**identity)
        if current is None or current.payment_status != "verified_paid":
            return await self._fulfillment_hold(identity, "LIANGPIAO_PAYMENT_STATE_NOT_VALIDATED")
        if current.flow_state in {"TICKET_SENT", "COMPLETED", "REFUND_PENDING", "REFUNDED", "CANCELLED", "MANUAL_HOLD"}:
            return {"status": "SKIPPED", "reason": "LIANGPIAO_TERMINAL_OR_HELD_STATE", "state": current.flow_state}
        if current.flow_state not in {"PAID_WAITING_FULFILLMENT", "FULFILLMENT_IN_PROGRESS"}:
            return await self._fulfillment_hold(identity, "LIANGPIAO_PAYMENT_STATE_NOT_READY")
        try:
            bound = self._binding_service.get_bound_quote(order_id, identity)
        except (TypeError, ValueError, KeyError):
            bound = None
        if not isinstance(bound, Mapping):
            return await self._fulfillment_hold(identity, "NO_BINDING")
        reason = _validate_fulfillment_binding(bound, identity, order_id, current)
        if reason is not None:
            return await self._fulfillment_hold(identity, reason)
        quote = self._quote_records.get_record(
            tenant_id=identity["tenant_id"], record_id=str(bound.get("record_id") or ""),
        )
        if not isinstance(quote, Mapping) or quote.get("quote_id") != bound.get("quote_id"):
            return await self._fulfillment_hold(identity, "QUOTE_RECORD_UNAVAILABLE")
        if str(quote.get("quote_hash") or quote.get("terms_fingerprint") or "") != str(bound.get("quote_hash") or bound.get("terms_fingerprint") or ""):
            return await self._fulfillment_hold(identity, "QUOTE_HASH_MISMATCH")
        quote_expires_at = self._parse_time(quote.get("expires_at"))
        if quote_expires_at is None or quote_expires_at <= self._now_provider():
            return await self._fulfillment_hold(identity, "QUOTE_EXPIRED")
        lineage, reason = _liangpiao_lineage(quote)
        if lineage is None:
            return await self._fulfillment_hold(identity, reason or "LIANGPIAO_LINEAGE_INCOMPLETE")

        existing = self._existing_attempts(quote, identity, order_id)
        if any(_terminal_provider_state(item.get("provider_status")) for item in existing):
            return {"status": "SKIPPED", "reason": "LIANGPIAO_ORDER_ALREADY_TERMINAL"}
        active = next(
            (item for item in existing
             if _provider_order_id(item) and not _explicit_provider_failure(item.get("provider_status"))
             and str(item.get("provider_status") or "").upper() != "UNKNOWN"),
            None,
        )
        if active is not None:
            reconciled = await self.reconcile_order_detail(active, identity=identity, current=current)
            if reconciled is not None and str(reconciled.get("provider_status") or "").upper() != "UNKNOWN":
                return reconciled
            held = await self._fulfillment_hold(identity, "LIANGPIAO_EXISTING_PROVIDER_ORDER_UNRECONCILED")
            return {**held, "out_order_no": active.get("out_order_no"),
                    "provider_order_no": active.get("provider_order_no")}
        unknown = next((item for item in existing if str(item.get("provider_status") or "").upper() == "UNKNOWN"), None)
        if unknown is not None:
            reconciled = await self.reconcile_order_detail(unknown, identity=identity, current=current)
            if reconciled is not None:
                return reconciled
            held = await self._fulfillment_hold(identity, "LIANGPIAO_PROVIDER_UNKNOWN")
            return {**held, "out_order_no": unknown.get("out_order_no")}
        limit_failed = next((item for item in existing if int(item.get("attempt_no") or 0) == 0 and _explicit_provider_failure(item.get("provider_status"))), None)
        attempt = 1 if limit_failed is not None and str(quote.get("price_mode") or "").upper() == "LIMIT" else 0
        first_mode = "FIXED" if attempt == 1 else str(quote.get("price_mode") or "").upper()
        first = await self._run_fulfillment_attempt(
            identity, order_id, quote, lineage, current, body, mode=first_mode, attempt=attempt,
        )
        if first.get("status") in {"ORDER_CREATED", "TICKETED"}:
            return first
        if first.get("status") == "MANUAL_HOLD":
            held = await self._fulfillment_hold(identity, str(first.get("reason") or "LIANGPIAO_FULFILLMENT_HOLD"))
            return {**held, **first}
        if first_mode != "LIMIT" or first.get("failure_kind") != "EXPLICIT_FAILURE":
            if first.get("status") == "FAILED":
                held = await self._fulfillment_hold(identity, str(first.get("reason") or "LIANGPIAO_ORDER_CREATE_FAILED"))
                return {**held, **first}
            return first

        fixed = await self._run_fulfillment_attempt(
            identity, order_id, quote, lineage, current, body, mode="FIXED", attempt=1,
            preflight_only=True,
        )
        fixed_cost = fixed.get("provider_cost_fen")
        seller_total = quote.get("total_sell_price_fen")
        if not isinstance(seller_total, int) or isinstance(seller_total, bool) or not isinstance(fixed_cost, int) or isinstance(fixed_cost, bool):
            return await self._fulfillment_hold(identity, "LIANGPIAO_FIXED_PREFLIGHT_INCOMPLETE")
        if seller_total < fixed_cost:
            return await self._fulfillment_refund_required(
                identity, "LIANGPIAO_FIXED_COST_EXCEEDS_SELLER_COMMITMENT", {
                    "seller_quote_total_fen": seller_total, "current_fixed_provider_cost_fen": fixed_cost,
                    "difference_fen": fixed_cost - seller_total, "limit_result": first,
                    "fixed_preflight": fixed, "out_order_no": first.get("out_order_no"),
                }, current,
            )
        replay = await self._run_fulfillment_attempt(
            identity, order_id, quote, lineage, current, body, mode="FIXED", attempt=1,
            fresh=fixed.get("fresh"),
        )
        if replay.get("status") in {"ORDER_CREATED", "TICKETED"}:
            return {**replay, "replay_authorized": True, "max_auto_replay": 1,
                    "seller_quote_total_fen": seller_total, "fixed_provider_cost_fen": fixed_cost,
                    "original_limit_order": first.get("out_order_no")}
        if replay.get("failure_kind") == "UNKNOWN":
            held = await self._fulfillment_hold(identity, str(replay.get("reason") or "LIANGPIAO_PROVIDER_UNKNOWN"))
            return {**held, **replay}
        return await self._fulfillment_refund_required(
            identity, "LIANGPIAO_AUTO_REPLAY_EXHAUSTED", {
                "seller_quote_total_fen": seller_total, "current_fixed_provider_cost_fen": fixed_cost,
                "limit_result": first, "fixed_result": replay, "attempt_count": 2,
            }, current,
        )

    async def _run_fulfillment_attempt(
        self, identity: Mapping[str, str], order_id: str, quote: Mapping[str, Any],
        lineage: Mapping[str, Any], current: Any, body: Mapping[str, Any], *,
        mode: str, attempt: int, preflight_only: bool = False,
        fresh: SelectedSeatQuoteResult | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if fresh is None:
            try:
                fresh = await self._fresh_preflight(identity, quote, lineage, mode)
            except Exception as error:
                return {"status": "MANUAL_HOLD", "reason": "LIANGPIAO_FRESH_PREFLIGHT_FAILED", "error_type": type(error).__name__}
        if isinstance(fresh, Mapping) and fresh.get("status") == "MANUAL_HOLD":
            return dict(fresh)
        fresh_values = fresh.model_dump(mode="json") if isinstance(fresh, SelectedSeatQuoteResult) else dict(fresh)
        parity_reason = _validate_fresh_preflight(fresh_values, lineage, quote, mode)
        if parity_reason is not None:
            return {"status": "MANUAL_HOLD", "reason": parity_reason}
        provider_cost = _provider_cost(fresh_values)
        if provider_cost is None:
            return {"status": "MANUAL_HOLD", "reason": "LIANGPIAO_FRESH_PROVIDER_COST_MISSING"}
        if preflight_only:
            return {"status": "PREFLIGHT_READY", "provider_cost_fen": provider_cost, "fresh": fresh_values}
        quote_hash = str(quote.get("quote_hash") or quote.get("terms_fingerprint") or "").strip()
        generation = int(quote.get("generation") or 0)
        binding_revision = int(quote.get("binding_revision") or 0)
        request = LiangpiaoOrderRequest(
            tenant_id=identity["tenant_id"], conversation_id=identity["chat_id"],
            shop_id=identity["shop_id"], buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
            confirmation_id=str(quote.get("confirmation_id") or order_id), quote_id=str(quote["quote_id"]),
            quote_hash=quote_hash, latest_buyer_message="payment_validated",
            buyer_phone=_order_phone(body), generation=generation,
            trace_id=f"{order_id}:liangpiao:attempt:{attempt}", buyer_confirmed=True,
            platform_order_id=order_id, binding_revision=binding_revision,
            fulfillment_attempt=attempt,
        )
        snapshot = {
            **fresh_values, "quote_id": str(quote["quote_id"]), "quote_hash": quote_hash,
            "generation": generation, "expires_at": quote.get("expires_at"),
            "show_id": lineage["show_id"], "seats": list(lineage["seats"]),
            "ticket_mode": lineage["ticket_mode"], "price_mode": mode,
            "area_quote_strategy": lineage.get("area_quote_strategy"),
            "max_price_fen": fresh_values.get("max_price_fen") or provider_cost,
            "buyer_amount_fen": fresh_values.get("buyer_amount_fen") or provider_cost,
        }
        out_order_no = _out_order_no(request)
        payload = _create_payload(snapshot, request, out_order_no)
        audit = {
            "flow_version": "V4_LIANGPIAO_FULFILLMENT_V1", "out_order_no": out_order_no,
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"], "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
            "platform_order_id": order_id, "quote_id": quote["quote_id"], "quote_hash": quote_hash,
            "quote_generation": generation, "binding_revision": binding_revision,
            "attempt_no": attempt, "price_mode": mode, "ticket_mode": lineage["ticket_mode"],
            "show": {key: quote.get(key) for key in ("city", "cinema", "movie", "quote_date", "showtime_start", "hall")},
            "seats": list(lineage["seats"]), "provider_amount_fen": provider_cost,
            "seller_quote_total_fen": quote.get("total_sell_price_fen"), "payload": payload,
            "payload_hash": _payload_hash(payload), "provider_status": "SUBMITTING",
            "created_at": self._now_provider().isoformat(), "updated_at": self._now_provider().isoformat(),
        }
        save = getattr(self._order_store, "save_liangpiao_order", None)
        if callable(save):
            save(audit)
        try:
            created = await self.create(request, snapshot)
        except OrderServiceError as error:
            status = "UNKNOWN" if error.code == "LIANGPIAO_PROVIDER_UNKNOWN" else "FAILED"
            update = getattr(self._order_store, "update_liangpiao_order", None)
            if callable(update):
                update(out_order_no, provider_status=status, snapshot_updates={
                    "failure_reason": error.code, "failure_message": error.message,
                    "updated_at": self._now_provider().isoformat(),
                })
            return {"status": "MANUAL_HOLD" if status == "UNKNOWN" else "FAILED",
                    "failure_kind": "UNKNOWN" if status == "UNKNOWN" else "EXPLICIT_FAILURE",
                    "reason": error.code, "out_order_no": out_order_no,
                    "error": error.message, "provider_cost_fen": provider_cost}
        provider_status = str(created.provider_status or "CREATED").upper()
        update = getattr(self._order_store, "update_liangpiao_order", None)
        if callable(update):
            update(out_order_no, provider_order_no=created.provider_order_no,
                   provider_status=provider_status, snapshot_updates={
                       "provider_response": created.raw, "updated_at": self._now_provider().isoformat(),
                   })
        self._transition_fulfillment(identity, current, order_id, out_order_no, created.provider_order_no, provider_status)
        if _explicit_provider_failure(provider_status):
            return {"status": "FAILED", "failure_kind": "EXPLICIT_FAILURE", "reason": "LIANGPIAO_ORDER_CREATE_FAILED", "out_order_no": out_order_no, "provider_cost_fen": provider_cost}
        return {"status": "ORDER_CREATED", "out_order_no": out_order_no,
                "provider_order_no": created.provider_order_no, "provider_status": provider_status,
                "provider_cost_fen": provider_cost, "attempt_no": attempt, "fresh": fresh_values}

    async def _fresh_preflight(
        self, identity: Mapping[str, str], quote: Mapping[str, Any],
        lineage: Mapping[str, Any], mode: str,
    ) -> SelectedSeatQuoteResult | Mapping[str, Any]:
        if self._preflight_service is None:
            raise OrderServiceError("LIANGPIAO_FRESH_PREFLIGHT_UNAVAILABLE", "fresh preflight unavailable")
        request = {
            "tenant_id": identity["tenant_id"], "conversation_id": identity["chat_id"],
            "cinema_id": int(lineage["cinema_id"]), "show_id": lineage["show_id"],
            "cinema_name": quote.get("cinema"), "movie_name": quote.get("movie"),
            "show_date": quote.get("quote_date"), "showtime_start": quote.get("showtime_start"),
            "hall_name": quote.get("hall"), "seats": list(lineage["seats"]),
            "ticket_mode": lineage["ticket_mode"], "price_mode": mode,
            "area_quote_strategy": lineage.get("area_quote_strategy"),
            "trace_id": f'{quote.get("quote_id")}:fulfillment:{mode.lower()}',
        }
        return await self._preflight_service.quote(request)

    def _existing_attempts(self, quote: Mapping[str, Any], identity: Mapping[str, str], order_id: str) -> list[dict[str, Any]]:
        lister = getattr(self._order_store, "list_liangpiao_orders", None)
        if not callable(lister):
            return []
        return [item for item in lister(identity["tenant_id"], limit=500)
                if item.get("platform_order_id") == order_id and item.get("quote_id") == quote.get("quote_id")]

    async def reconcile_order_detail(
        self, order: Mapping[str, Any], *, identity: Mapping[str, str], current: Any | None = None,
    ) -> dict[str, Any] | None:
        provider_order_no = str(order.get("provider_order_no") or "").strip()
        if not provider_order_no:
            return None
        try:
            detail = await self._client.order_detail(orderNo=provider_order_no)
        except Exception:
            return None
        status = str(detail.get("status") or detail.get("orderStatus") or "UNKNOWN").upper()
        codes = _ticket_codes(detail)
        update = getattr(self._order_store, "update_liangpiao_order", None)
        if callable(update):
            update(str(order.get("out_order_no") or ""), provider_status=status,
                   snapshot_updates={"detail_evidence": detail, "ticket_codes": codes, "updated_at": self._now_provider().isoformat()})
        if current is not None and codes and status in {"TICKETED", "TICKET_SENT", "SETTLED", "COMPLETED"}:
            self._transition_fulfillment(
                identity, current, str(current.order_id or ""), str(order.get("out_order_no") or ""),
                provider_order_no, status, ticket_codes=codes,
            )
            return {"status": "TICKETED", "provider_status": status, "ticket_codes": codes,
                    "out_order_no": order.get("out_order_no"), "provider_order_no": provider_order_no}
        return {"status": "RECONCILED", "provider_status": status, "ticket_codes": codes,
                "out_order_no": order.get("out_order_no"), "provider_order_no": provider_order_no}

    def _transition_fulfillment(
        self, identity: Mapping[str, str], current: Any, order_id: str,
        out_order_no: str, provider_order_no: str | None, provider_status: str,
        *, ticket_codes: list[str] | None = None,
    ) -> Any | None:
        transition = getattr(self._state_store, "transition", None)
        if not callable(transition):
            return None
        if getattr(current, "flow_state", None) in {"REFUND_PENDING", "REFUNDED", "MANUAL_HOLD", "CANCELLED", "COMPLETED", "TICKET_SENT"}:
            return current
        try:
            return transition(
                **identity, expected_revision=int(getattr(current, "revision", 0)),
                event_id=f"{out_order_no}:provider:{provider_status.lower()}",
                transition_code="liangpiao_fulfillment_started",
                flow_state="FULFILLMENT_IN_PROGRESS",
                updates={"order_id": order_id, "order_status": "paid", "payment_status": "verified_paid",
                         "fulfillment_status": "ticket_issued" if ticket_codes else "claimed",
                         "out_order_no": out_order_no, "provider_order_no": provider_order_no,
                         "provider_status": provider_status.lower(), **({"ticket_codes": ticket_codes} if ticket_codes else {})},
                allow_compatible_bootstrap=True,
            )
        except Exception:
            return None

    async def _fulfillment_hold(self, identity: Mapping[str, str] | None, reason: str) -> dict[str, Any]:
        if identity is not None and self._state_store is not None:
            current = self._state_store.get(**identity)
            if current is not None and current.flow_state not in {"REFUND_PENDING", "REFUNDED", "TICKET_SENT", "COMPLETED", "CANCELLED"}:
                self._safe_transition(identity, current, "MANUAL_HOLD", "liangpiao_fulfillment_hold", {"fulfillment_status": "failed"}, reason)
        return {"status": "MANUAL_HOLD", "reason": reason}

    async def _fulfillment_refund_required(self, identity: Mapping[str, str], reason: str, evidence: Mapping[str, Any], current: Any) -> dict[str, Any]:
        self._safe_transition(identity, current, "REFUND_PENDING", "liangpiao_refund_required", {
            "order_status": "refund_pending", "payment_status": "refund_pending", "fulfillment_status": "failed",
        }, reason)
        return {"status": "REFUND_REQUIRED", "reason": reason, "refund_api_allowed": False, **dict(evidence)}

    def _safe_transition(self, identity: Mapping[str, str], current: Any, state: str, code: str, updates: dict[str, Any], reason: str) -> None:
        transition = getattr(self._state_store, "transition", None)
        if not callable(transition) or getattr(current, "flow_state", None) in {"REFUND_PENDING", "REFUNDED", "TICKET_SENT", "COMPLETED"}:
            return
        try:
            transition(**identity, expected_revision=int(getattr(current, "revision", 0)),
                        event_id=f"liangpiao:{code}:{reason}", transition_code=code,
                        flow_state=state, updates=updates, allow_compatible_bootstrap=True)
        except Exception:
            return

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _out_order_no(request: LiangpiaoOrderRequest) -> str:
    if request.platform_order_id and request.binding_revision:
        material = "\\0".join(str(value) for value in (
            request.tenant_id, request.shop_id, request.buyer_id, request.chat_id,
            request.platform_order_id, request.quote_id, request.quote_hash,
            request.generation, request.binding_revision, request.fulfillment_attempt,
        ))
    else:
        material = f"{request.tenant_id}\\0{request.quote_id}\\0{request.quote_hash}"
    return "lp-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _create_payload(values: Mapping[str, Any], request: LiangpiaoOrderRequest, out_order_no: str) -> dict[str, Any]:
    seats = values.get("seats") or []
    payload = {
        "showId": values.get("show_id"),
        "seats": [_seat_payload(item) for item in seats],
        "ticketMode": values.get("ticket_mode", "STANDARD"),
        "priceMode": values.get("price_mode", "FIXED"),
        "outOrderNo": out_order_no,
        "tel": request.buyer_phone,
        "maxPrice": int(values.get("max_price_fen") or values.get("buyer_amount_fen") or 0),
        "allowSeatChange": bool(request.allow_seat_change),
        "attach": request.confirmation_id,
        "traceId": request.trace_id,
    }
    area_strategy = values.get("area_quote_strategy")
    if area_strategy in {"AVERAGE", "HIGHEST", "LOWEST"}:
        payload["areaQuoteStrategy"] = area_strategy
    return payload


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _payment_identity(body: Mapping[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    order = body.get("order") if isinstance(body.get("order"), Mapping) else {}
    identity = {
        "tenant_id": str(envelope.get("tenantId") or envelope.get("tenant_id") or "").strip(),
        "shop_id": str(session.get("accountUnb") or session.get("account_unb") or "").strip(),
        "buyer_id": str(session.get("peerUnb") or session.get("peer_unb") or "").strip(),
        "chat_id": str(session.get("chatId") or session.get("chat_id") or "").strip(),
    }
    order_id = str(order.get("order_id") or order.get("platform_order_id") or "").strip()
    return (identity, order_id) if order_id and all(identity.values()) else (None, None)


def _validate_fulfillment_binding(bound: Mapping[str, Any], identity: Mapping[str, str], order_id: str, state: Any) -> str | None:
    if any(str(bound.get(field) or "").strip() != identity[field] for field in ("tenant_id", "shop_id", "buyer_id", "chat_id")):
        return "IDENTITY_MISMATCH"
    if str(bound.get("platform_order_id") or bound.get("order_id") or "").strip() != order_id:
        return "PLATFORM_ORDER_ID_MISMATCH"
    if str(bound.get("provider_route") or "").upper() != "LIANGPIAO":
        return "NON_LIANGPIAO_ROUTE"
    if not str(bound.get("quote_id") or "").strip() or not str(bound.get("quote_hash") or bound.get("terms_fingerprint") or "").strip():
        return "QUOTE_LINEAGE_INCOMPLETE"
    if not isinstance(bound.get("generation"), int) or isinstance(bound.get("generation"), bool) or bound["generation"] < 1:
        return "QUOTE_GENERATION_MISSING"
    if not isinstance(bound.get("binding_revision"), int) or isinstance(bound.get("binding_revision"), bool) or bound["binding_revision"] < 1:
        return "BINDING_REVISION_MISSING"
    evidence = getattr(state, "payment_validation_evidence", None)
    if isinstance(evidence, Mapping):
        if evidence.get("quote_id") not in {None, bound.get("quote_id")}:
            return "QUOTE_IDENTITY_MISMATCH"
        if evidence.get("quote_generation") not in {None, bound.get("generation")}:
            return "QUOTE_GENERATION_MISMATCH"
        if evidence.get("binding_revision") not in {None, bound.get("binding_revision")}:
            return "BINDING_REVISION_MISMATCH"
    return None


def _liangpiao_lineage(quote: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    fields = {"cinema_id": quote.get("liangpiao_cinema_id"), "movie_id": quote.get("liangpiao_movie_id"), "show_id": quote.get("liangpiao_show_id")}
    if any(not str(value or "").strip() for value in fields.values()):
        return None, "LIANGPIAO_LINEAGE_INCOMPLETE"
    mode = str(quote.get("ticket_mode") or "").strip().upper()
    price_mode = str(quote.get("price_mode") or "").strip().upper()
    if mode not in {"STANDARD", "FAST", "FLASH"} or price_mode not in {"LIMIT", "FIXED"}:
        return None, "LIANGPIAO_MODE_LINEAGE_INCOMPLETE"
    raw_seats = quote.get("selected_seats")
    if not isinstance(raw_seats, list) or not raw_seats:
        return None, "LIANGPIAO_SEAT_LINEAGE_INCOMPLETE"
    seats: list[dict[str, Any]] = []
    for raw in raw_seats:
        if not isinstance(raw, Mapping):
            return None, "LIANGPIAO_SEAT_LINEAGE_INCOMPLETE"
        values = {
            "row_no": raw.get("row_no", raw.get("rowNo")), "col_no": raw.get("col_no", raw.get("colNo")),
            "seat_no": raw.get("seat_no", raw.get("seatNo")), "area_id": raw.get("area_id", raw.get("areaId")),
        }
        if any(value is None or str(value).strip() == "" for value in values.values()):
            return None, "LIANGPIAO_SEAT_LINEAGE_INCOMPLETE"
        try:
            values["row_no"] = int(values["row_no"])
            values["col_no"] = int(values["col_no"])
        except (TypeError, ValueError):
            return None, "LIANGPIAO_SEAT_LINEAGE_INCOMPLETE"
        seats.append(values)
    strategy = quote.get("area_quote_strategy")
    if strategy is not None:
        strategy = str(strategy).upper()
        if strategy not in {"AVERAGE", "HIGHEST", "LOWEST"}:
            return None, "LIANGPIAO_AREA_STRATEGY_INVALID"
    return {
        "cinema_id": str(fields["cinema_id"]), "movie_id": str(fields["movie_id"]),
        "show_id": str(fields["show_id"]), "ticket_mode": mode, "price_mode": price_mode,
        "area_quote_strategy": strategy, "seats": seats,
    }, None


def _validate_fresh_preflight(fresh: Mapping[str, Any], lineage: Mapping[str, Any], quote: Mapping[str, Any], mode: str) -> str | None:
    if fresh.get("preflight_verified") is not True:
        return "LIANGPIAO_FRESH_PREFLIGHT_NOT_VERIFIED"
    if str(fresh.get("show_id") or "") != str(lineage["show_id"]):
        return "LIANGPIAO_SHOW_MISMATCH"
    if str(fresh.get("price_mode") or "").upper() != mode:
        return "LIANGPIAO_PRICE_MODE_MISMATCH"
    snapshot = fresh.get("snapshot") if isinstance(fresh.get("snapshot"), Mapping) else {}
    if snapshot.get("ticket_mode") not in {None, lineage["ticket_mode"]}:
        return "LIANGPIAO_TICKET_MODE_MISMATCH"
    fresh_seats = fresh.get("seats")
    if not isinstance(fresh_seats, list) or _seat_signatures(fresh_seats) != _seat_signatures(lineage["seats"]):
        return "LIANGPIAO_SEAT_MISMATCH"
    return None


def _seat_signatures(values: list[Any]) -> list[tuple[str, str, str, str]]:
    result = []
    for item in values:
        if isinstance(item, Mapping):
            result.append((str(item.get("row_no", item.get("rowNo"))), str(item.get("col_no", item.get("colNo"))), str(item.get("seat_no", item.get("seatNo"))), str(item.get("area_id", item.get("areaId")))))
        else:
            result.append((str(getattr(item, "row_no", "")), str(getattr(item, "col_no", "")), str(getattr(item, "seat_no", "")), str(getattr(item, "area_id", ""))))
    return sorted(result)


def _provider_cost(values: Mapping[str, Any]) -> int | None:
    for key in ("provider_amount_fen", "provider_cost_fen", "buyer_amount_fen"):
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _explicit_provider_failure(value: object) -> bool:
    return str(value or "").strip().upper() in {"FAILED", "REJECTED", "CANCELLED", "FAIL"}


def _terminal_provider_state(value: object) -> bool:
    return str(value or "").strip().upper() in {"TICKETED", "TICKET_SENT", "SETTLED", "COMPLETED", "REFUNDED"}


def _order_phone(body: Mapping[str, Any]) -> str:
    order = body.get("order") if isinstance(body.get("order"), Mapping) else {}
    phone = str(order.get("buyer_phone") or order.get("buyerPhone") or order.get("tel") or "").strip()
    if not phone:
        raise OrderServiceError("LIANGPIAO_PHONE_MISSING", "买家手机号缺失。")
    return phone


def _ticket_codes(value: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for item in value.get("tickets", []) if isinstance(value.get("tickets"), list) else []:
        if isinstance(item, Mapping):
            code = str(item.get("ticketCode") or item.get("ticket_code") or item.get("ticketNo") or item.get("ticket_no") or "").strip()
            if code:
                result.append(code)
    for key in ("ticketCode", "ticket_code", "ticketNo", "ticket_no"):
        code = str(value.get(key) or "").strip()
        if code:
            result.append(code)
    return list(dict.fromkeys(result))


def _is_unknown_create_result(error: Exception) -> bool:
    return (
        isinstance(error, (TimeoutError, ConnectionError))
        or isinstance(error, ProviderError)
        and error.code == "liangpiao_network_error"
    )


def _order_create_rejected(error: Exception) -> OrderServiceError:
    message = str(getattr(error, "message", "") or "").strip()
    return OrderServiceError(
        "LIANGPIAO_ORDER_CREATE_REJECTED",
        message[:200] or "良票下单未受理，请重新核对场次、座位和渠道。",
        manual_hold=False,
    )


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
