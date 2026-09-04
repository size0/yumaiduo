"""Authoritative, read-only payment validation for FishMore order.paid events.

This module deliberately does not own a quote repository or a transaction
repository.  Binding, QuoteRecordStore, and the RulesFirst transaction state
store remain the three authorities used by the payment reducer.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .order_quote_binding_v2.service import OrderQuoteBindingV2Service
from .transaction_state_store import StateRevisionConflict


AUTO_SOURCES = frozenset({"AUTO_PRICING", "wanda_pricing_v2"})
MANUAL_SOURCE = "MANUAL_OPERATOR"
PAYMENT_VALIDATION_EVENT = "order.paid"
PAID_STATUSES = frozenset({
    "2", "paid", "payment_success", "已付款", "支付成功", "shipped", "completed",
    "finished", "已发货", "已完成", "交易成功",
})
NO_REFUND_STATUSES = frozenset({"", "0", "none", "no_refund", "not_refunded", "false"})


class PaymentValidationError(ValueError):
    pass


def parse_fishmore_fen(value: object, *, field: str = "payment") -> int | None:
    """Parse FishMore's integer fen field without decimal/boolean coercion.

    FishMore serializes money as a string containing fen (for example
    ``"2000"``).  A decimal string is not a yuan value to convert here and is
    therefore rejected rather than silently multiplied or truncated.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 9_007_199_254_740_991 else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit() and len(normalized) <= 16:
            parsed = int(normalized)
            return parsed if parsed <= 9_007_199_254_740_991 else None
    return None


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _field(source: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try:
            numeric = float(text)
        except ValueError:
            return None
        if numeric > 10_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None


def _identity(body: Mapping[str, Any]) -> tuple[dict[str, str], str] | None:
    envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    order = body.get("order") if isinstance(body.get("order"), Mapping) else {}
    values = {
        "tenant_id": _text(envelope.get("tenantId") or envelope.get("tenant_id")),
        # These are intentionally session-first.  The session is the
        # authoritative conversation returned by getSessionByOrder.
        "shop_id": _text(session.get("accountUnb") or session.get("account_unb")),
        "buyer_id": _text(session.get("peerUnb") or session.get("peer_unb")),
        "chat_id": _text(session.get("chatId") or session.get("chat_id")),
    }
    order_id = _text(
        order.get("order_id") or order.get("platform_order_id")
        or payload.get("orderId") or payload.get("order_id")
        or payload.get("platformOrderId") or payload.get("platform_order_id")
    )
    return ({key: value for key, value in values.items()}, order_id) if order_id and all(values.values()) else None  # type: ignore[arg-type]


def _order_is_paid(order: Mapping[str, Any]) -> bool:
    status = (_text(_field(order, "order_status", "orderStatus", "status")) or "").lower()
    return status in PAID_STATUSES or bool(_field(order, "paid_at", "payTime", "pay_time", "paidAt"))


def _same_or_missing(actual: object, expected: str) -> bool:
    value = _text(actual)
    return value is None or value == expected


class AuthoritativePaymentValidationService:
    """Reduce one authoritative ``order.paid`` reread into durable state.

    ``expired_auto_refresh`` is an optional *read-only* facts/pricing adapter.
    It may return a validation-only amount, but it must not save a Quote or
    perform a provider/platform write.  If it is absent, expiration fails
    closed into MANUAL_HOLD.
    """

    authority_name = "rules_first_sqlite_payment_validation"

    def __init__(
        self,
        state_store: object,
        binding_service: OrderQuoteBindingV2Service,
        quote_store: object,
        *,
        wplus_service: object | None = None,
        expired_auto_refresh: Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any] | None] | None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._states = state_store
        self._binding = binding_service
        self._quotes = quote_store
        self._wplus = wplus_service
        self._expired_auto_refresh = expired_auto_refresh
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    async def process_event(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        if _text(envelope.get("event")) != "order.paid":
            return None
        event_id = _text(envelope.get("id") or envelope.get("eventId") or envelope.get("event_id"))
        identity_result = _identity(body)
        if not event_id or identity_result is None:
            return self._unverified(event_id, "PAYMENT_EVENT_IDENTITY_UNVERIFIED")
        identity, order_id = identity_result
        order = body.get("order") if isinstance(body.get("order"), Mapping) else None
        if order is None:
            return self._unverified(event_id, "AUTHORITATIVE_ORDER_MISSING")
        if _text(order.get("tenant_id")) not in {None, identity["tenant_id"]}:
            return await self._hold(identity, event_id, order_id, "PAYMENT_TENANT_MISMATCH")
        if not _same_or_missing(order.get("order_id"), order_id):
            return await self._hold(identity, event_id, order_id, "PAYMENT_ORDER_ID_MISMATCH")
        if not all(_same_or_missing(order.get(field), identity[key]) for field, key in (
            ("shop_id", "shop_id"), ("buyer_id", "buyer_id"), ("chat_id", "chat_id"),
        )):
            return await self._hold(identity, event_id, order_id, "PAYMENT_ORDER_SESSION_IDENTITY_MISMATCH")

        current = self._states.get(**identity)
        if current is not None and event_id in current.processed_event_ids:
            return self._duplicate(current, event_id)
        if not _order_is_paid(order):
            return await self._hold(identity, event_id, order_id, "PAYMENT_NOT_AUTHORITATIVE")
        refund_status = (_text(_field(order, "refund_status", "refundStatus", "refund_state", "refundState")) or "").lower()
        if refund_status not in NO_REFUND_STATUSES:
            return await self._hold(identity, event_id, order_id, "PAYMENT_REFUND_STATE_UNSAFE")
        actual = parse_fishmore_fen(_field(order, "amount_cents", "payment", "payment_cents", "price_fee", "priceFee"))
        if actual is None:
            return await self._hold(identity, event_id, order_id, "PAYMENT_AMOUNT_UNSAFE")

        try:
            bound = self._binding.get_bound_quote(order_id, identity)
        except (ValueError, KeyError):
            bound = None
        if not isinstance(bound, Mapping):
            return await self._hold(identity, event_id, order_id, "PAYMENT_BINDING_UNAVAILABLE", actual=actual)
        if not self._binding_identity_is_valid(bound, identity, order_id):
            return await self._hold(identity, event_id, order_id, "PAYMENT_BINDING_IDENTITY_MISMATCH", actual=actual)
        # Read the quote through the binding's record id only.  Never search
        # for a newer/nearby quote after an order has been bound.
        quote = bound
        record_id = _text(bound.get("record_id"))
        getter = getattr(self._quotes, "get_record", None)
        if callable(getter) and record_id:
            quote_record = getter(tenant_id=identity["tenant_id"], record_id=record_id)
            if not isinstance(quote_record, Mapping) or _text(quote_record.get("quote_id")) != _text(bound.get("quote_id")):
                return await self._hold(identity, event_id, order_id, "PAYMENT_QUOTE_RECORD_UNAVAILABLE", actual=actual, bound=bound)
            quote = quote_record

        expected = parse_fishmore_fen(quote.get("total_sell_price_fen"), field="expected_amount_fen")
        if expected is None or expected <= 0 or expected > 200_000:
            return await self._hold(identity, event_id, order_id, "PAYMENT_EXPECTED_AMOUNT_UNSAFE", actual=actual, bound=bound)
        evidence: dict[str, Any] = {
            "schema_version": "phase_10.payment_validation.v1",
            "event_id": event_id,
            "platform_order_id": order_id,
            "tenant_id": identity["tenant_id"],
            "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"],
            "chat_id": identity["chat_id"],
            "quote_record_id": _text(quote.get("record_id") or bound.get("record_id")),
            "quote_id": _text(quote.get("quote_id") or bound.get("quote_id")),
            "quote_source": _text(quote.get("source") or bound.get("source")),
            "quote_generation": quote.get("generation", bound.get("generation")),
            "binding_revision": quote.get("binding_revision", bound.get("binding_revision")),
            "expected_amount_cents": expected,
            "actual_amount_cents": actual,
            # postFee is recorded as a separate provider fact; it is never
            # added to payment and never used as the expected quote amount.
            "post_fee_cents": parse_fishmore_fen(_field(order, "post_fee_cents", "postFee", "post_fee"), field="post_fee"),
            "paid_at": _text(_field(order, "paid_at", "payTime", "pay_time", "paidAt")),
            "validated_at": self._now().isoformat(),
        }

        validation_quote = quote
        if not self._quote_active(quote):
            source = _text(bound.get("source"))
            if source == MANUAL_SOURCE:
                evidence.update({"validation_status": "MANUAL_HOLD", "reason_code": "MANUAL_QUOTE_EXPIRED_AT_PAYMENT"})
                return await self._apply_hold(identity, event_id, order_id, "MANUAL_QUOTE_EXPIRED_AT_PAYMENT", evidence, bound)
            if source not in AUTO_SOURCES:
                evidence.update({"validation_status": "MANUAL_HOLD", "reason_code": "QUOTE_EXPIRED_UNSUPPORTED_SOURCE"})
                return await self._apply_hold(identity, event_id, order_id, "QUOTE_EXPIRED_UNSUPPORTED_SOURCE", evidence, bound)
            refreshed = await self._refresh_expired_auto(bound, order, body)
            refreshed_amount = parse_fishmore_fen(
                refreshed.get("total_sell_price_fen", refreshed.get("expected_amount_cents"))
                if isinstance(refreshed, Mapping) else None,
                field="refreshed_expected_amount_fen",
            )
            if refreshed_amount is None or refreshed_amount <= 0 or refreshed_amount > 200_000:
                evidence.update({"validation_status": "MANUAL_HOLD", "reason_code": "AUTO_QUOTE_REFRESH_UNAVAILABLE", "refresh_status": "unavailable"})
                return await self._apply_hold(identity, event_id, order_id, "AUTO_QUOTE_REFRESH_UNAVAILABLE", evidence, bound)
            expected = refreshed_amount
            evidence["expected_amount_cents"] = refreshed_amount
            # Refresh may supply only a recalculated amount.  Preserve the
            # bound quote's immutable route/type/context; callback metadata
            # cannot redirect payment into another transaction.
            validation_quote = {**quote, "total_sell_price_fen": refreshed_amount}
            evidence.update({
                "expired_quote": True,
                "refresh_status": "validation_only",
                "refreshed_expected_amount_cents": refreshed_amount,
                "refreshed_quote_record_id": _text(refreshed.get("record_id")),
            })
        else:
            evidence["expired_quote"] = False

        evidence["difference_cents"] = actual - expected
        if actual < expected:
            evidence.update({"validation_status": "REFUND_REQUIRED", "reason_code": "PAID_AMOUNT_LESS_THAN_EXPECTED"})
            return await self._apply_hold(identity, event_id, order_id, "PAID_AMOUNT_LESS_THAN_EXPECTED", evidence, validation_quote, payment_status="mismatch", result_status="REFUND_REQUIRED")
        evidence.update({
            "validation_status": "VERIFIED_PAID",
            "reason_code": "PAID_AMOUNT_VALID" if actual == expected else "PAID_AMOUNT_EXCESS_ACCEPTED",
            "overpayment_cents": max(0, actual - expected),
        })
        return await self._apply_verified(identity, event_id, order_id, evidence, validation_quote)

    def _now(self) -> datetime:
        value = self._now_provider()
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _quote_active(self, bound: Mapping[str, Any]) -> bool:
        checker = getattr(self._quotes, "is_quote_active", None)
        if not callable(checker):
            return False
        try:
            active = bool(checker(bound, at=self._now()))
        except (TypeError, ValueError):
            return False
        # A provider preflight can expire before the seller quote TTL.  It is
        # an independent read-freshness gate and must trigger AUTO refresh (or
        # fail closed), not authorize payment from stale availability facts.
        provider_expiry = _datetime(bound.get("provider_preflight_expires_at"))
        return active and (provider_expiry is None or self._now() < provider_expiry)

    async def _refresh_expired_auto(self, bound: Mapping[str, Any], order: Mapping[str, Any], body: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if self._expired_auto_refresh is None:
            return None
        try:
            value = self._expired_auto_refresh(bound, order, body)
            if inspect.isawaitable(value):
                value = await value
            return value if isinstance(value, Mapping) else None
        except Exception:
            return None

    @staticmethod
    def _binding_identity_is_valid(bound: Mapping[str, Any], identity: Mapping[str, str], order_id: str) -> bool:
        stored_order = _text(bound.get("platform_order_id") or bound.get("order_id"))
        return (
            stored_order == order_id
            and all(_text(bound.get(field)) == identity[field] for field in ("tenant_id", "shop_id", "buyer_id", "chat_id"))
            and bool(_text(bound.get("quote_id")))
            and isinstance(bound.get("binding_revision"), int)
            and not isinstance(bound.get("binding_revision"), bool)
            and bound.get("binding_revision", 0) >= 1
        )

    async def _apply_verified(self, identity: Mapping[str, str], event_id: str, order_id: str, evidence: dict[str, Any], quote: Mapping[str, Any]) -> dict[str, Any]:
        request_type = _text(quote.get("request_type"))
        route = _text(quote.get("provider_route"))
        if request_type == "WPLUS_AREA" and route in {None, "WANDA_SELF"} and self._wplus is not None:
            purchase_context = _text(quote.get("purchase_context_id") or quote.get("item_id"))
            if not purchase_context:
                evidence["validation_status"] = "MANUAL_HOLD"
                evidence["reason_code"] = "PAYMENT_WPLUS_CONTEXT_UNAVAILABLE"
                return await self._apply_hold(identity, event_id, order_id, "PAYMENT_WPLUS_CONTEXT_UNAVAILABLE", evidence, quote)
            applied = await self._wplus.apply_payment_validated(
                **identity, purchase_context_id=purchase_context,
                event_id=event_id, platform_order_id=order_id, payment_evidence=evidence,
                request_type=request_type, provider_route=route,
                quote_record_id=_text(quote.get("record_id")),
                transition_code="payment_validated_authoritative",
            )
            return {
                "handled": True, "status": applied.get("status"), "validation_status": "VERIFIED_PAID",
                "reason_code": evidence["reason_code"], "payment_validation": evidence,
                "decision": applied.get("decision", {"mode": "auto", "actions": [], "reason": "payment_validated_wplus"}),
                "rule_decision": self._rule_from_applied(applied, evidence), "wplus": True,
            }
        return await self._transition_result(
            identity, event_id, "PAID_WAITING_FULFILLMENT", "official_payment_verified", {
                "order_id": order_id, "order_status": "paid", "payment_status": "verified_paid",
                "fulfillment_status": "pending", "payment_validation_evidence": evidence,
            }, evidence, "VERIFIED_PAID",
        )

    async def _apply_hold(self, identity: Mapping[str, str], event_id: str, order_id: str, reason: str, evidence: dict[str, Any], bound: Mapping[str, Any] | None, *, payment_status: str = "verification_required", result_status: str = "MANUAL_HOLD") -> dict[str, Any]:
        return await self._transition_result(
            identity, event_id, "MANUAL_HOLD", reason, {
                "order_id": order_id, "order_status": "paid", "payment_status": payment_status,
                "fulfillment_status": "none", "payment_validation_evidence": evidence,
                **({"purchase_context_id": _text(bound.get("purchase_context_id") or bound.get("item_id"))} if bound else {}),
            }, evidence, result_status,
        )

    async def _hold(self, identity: Mapping[str, str], event_id: str, order_id: str | None, reason: str, *, actual: int | None = None, bound: Mapping[str, Any] | None = None) -> dict[str, Any]:
        evidence = {
            "schema_version": "phase_10.payment_validation.v1", "event_id": event_id,
            "platform_order_id": order_id, "validation_status": "MANUAL_HOLD", "reason_code": reason,
            "actual_amount_cents": actual, "validated_at": self._now().isoformat(),
        }
        if not identity.get("tenant_id"):
            return self._unverified(event_id, reason)
        return await self._apply_hold(identity, event_id, order_id or "unverified-order", reason, evidence, bound)

    async def _transition_result(self, identity: Mapping[str, str], event_id: str, flow_state: str, code: str, updates: dict[str, Any], evidence: dict[str, Any], status: str) -> dict[str, Any]:
        current = self._states.get(**identity) or self._states.get_or_create(**identity)
        if event_id in current.processed_event_ids:
            return self._duplicate(current, event_id)
        try:
            updated = self._states.transition(
                **identity, expected_revision=current.revision, event_id=event_id,
                transition_code=code, flow_state=flow_state, updates=updates,
                allow_compatible_bootstrap=True,
            )
        except StateRevisionConflict:
            latest = self._states.get(**identity)
            if latest is None:
                raise
            return self._duplicate(latest, event_id)
        return {
            "handled": True, "status": status, "validation_status": evidence.get("validation_status"),
            "reason_code": evidence.get("reason_code", code), "payment_validation": evidence,
            "rule_decision": {"state_before": current.flow_state, "state_after": updated.flow_state, "transition_code": code, "state_revision": updated.revision, "actions": [], "handoff_reason": code if flow_state == "MANUAL_HOLD" else None},
        }

    async def _unverified(self, event_id: str | None, reason: str) -> dict[str, Any]:
        return {"handled": True, "status": "MANUAL_HOLD", "validation_status": "MANUAL_HOLD", "reason_code": reason, "payment_validation": {"schema_version": "phase_10.payment_validation.v1", "event_id": event_id, "validation_status": "MANUAL_HOLD", "reason_code": reason}}

    @staticmethod
    def _duplicate(current: object, event_id: str) -> dict[str, Any]:
        return {"handled": True, "duplicate": True, "status": getattr(current, "flow_state", "MANUAL_HOLD"), "validation_status": "DUPLICATE", "reason_code": "PAYMENT_EVENT_DUPLICATE", "rule_decision": {"state_before": getattr(current, "flow_state", "MANUAL_HOLD"), "state_after": getattr(current, "flow_state", "MANUAL_HOLD"), "transition_code": "payment_event_duplicate_ignored", "state_revision": getattr(current, "revision", 0), "actions": []}}

    @staticmethod
    def _rule_from_applied(applied: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
        rule = applied.get("rule_decision")
        if isinstance(rule, Mapping):
            return dict(rule)
        state = str(applied.get("status") or "MANUAL_HOLD")
        return {"state_before": applied.get("state_before"), "state_after": state, "transition_code": "payment_validated_wplus", "state_revision": 0, "actions": [], "handoff_reason": None}


# Short compatibility names for callers that treat this as the payment
# validator rather than the phase-specific authority.
PaymentValidationService = AuthoritativePaymentValidationService
safe_parse_fishmore_fen = parse_fishmore_fen