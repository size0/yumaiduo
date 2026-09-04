from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from .reply_template_store import ReplyTemplates
from .transaction_state_store import StateRevisionConflict, TransactionState, WplusMarkReference


WPLUS_MARK_REPLY_TEMPLATE_AUTHORITY = "wplus_mark_required_template"
_MARK_ACCEPTING_STATES = frozenset({"WAITING_PAYMENT", "WAITING_WPLUS_MARK", "READY_FOR_MANUAL_TICKETING"})


class MarkDetector(Protocol):
    async def detect(self, image_url: str) -> bool | None: ...


class WplusFulfillmentMarkService:
    """Durably records W+ fulfillment marks without touching quote authority.

    This service intentionally has no pricing, provider, order-writing, or
    natural-language responsibilities. Transaction state is the authority for
    the current mark and its bounded audit history.
    """

    def __init__(
        self,
        state_store: object,
        *,
        quote_store: object | None = None,
        mark_detector: MarkDetector | Callable[[str], bool | None | Awaitable[bool | None]] | None = None,
        template_provider: Callable[[], ReplyTemplates] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._states = state_store
        self._quotes = quote_store
        self._detector = mark_detector
        self._template_provider = template_provider
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def _templates(self) -> ReplyTemplates:
        if self._template_provider is not None:
            current = self._template_provider()
            if isinstance(current, ReplyTemplates):
                return current
        return ReplyTemplates()

    def record_quote_context(
        self, body: Mapping[str, Any], result: Mapping[str, Any],
    ) -> TransactionState | None:
        """Attach canonical quote facts to the existing transaction state only.

        The quote remains immutable in QuoteRecordStore; this merely gives the
        fulfillment reducer the context it needs for a later payment fixture or
        mark submission.
        """
        identity = self._event_identity(body)
        quote = result.get("quote") if isinstance(result.get("quote"), Mapping) else {}
        request_type = _text(quote.get("request_type"))
        if identity is None or result.get("status") != "QUOTED" or request_type not in {
            "WPLUS_AREA", "EXACT_SEATS",
        }:
            return None
        current = self._states.get(**_state_identity(identity))
        if current is None:
            current = self._states.get_or_create(**_state_identity(identity))
        if current.flow_state not in {"NEW", "COLLECTING", "FACTS_READY", "QUOTED"}:
            return current
        if identity["event_id"] + ":quote-context" in current.processed_event_ids:
            return current
        context = _text(quote.get("purchase_context_id") or quote.get("item_id"))
        if not context:
            return current
        provider_route = _text(quote.get("provider_route"))
        if provider_route not in {None, "WANDA_SELF", "LIANGPIAO"}:
            provider_route = None
        updates = {
            "purchase_context_id": context,
            "quote_request_type": request_type,
            "quote_provider_route": provider_route,
            "active_quote_record_id": _text(quote.get("record_id")),
            "quote_status": "ready",
            "target_amount_cents": quote.get("total_sell_price_fen"),
            "confirmed_ticket_count": quote.get("ticket_count"),
        }
        quoted_mark = _image_reference(quote.get("mark_image_reference"))
        if (
            request_type == "WPLUS_AREA" and quote.get("has_manual_mark") is True
            and quoted_mark and current.current_wplus_mark is None
        ):
            quoted_reference = WplusMarkReference(
                revision=1, **_state_identity(identity), purchase_context_id=context,
                image_reference=quoted_mark,
                message_id=_text(quote.get("mark_message_id") or identity.get("message_id")),
                event_id=identity["event_id"], submitted_at=_utc(self._now_provider()).isoformat(),
            )
            updates.update({
                "wplus_mark_status": "submitted", "wplus_mark_revision": 1,
                "current_wplus_mark": quoted_reference.model_dump(),
                "wplus_mark_history": [quoted_reference.model_dump()],
            })
        try:
            return self._states.transition(
                **_state_identity(identity), expected_revision=current.revision,
                event_id=identity["event_id"] + ":quote-context",
                transition_code="canonical_quote_context_recorded", flow_state="QUOTED",
                updates=updates, allow_compatible_bootstrap=True,
            )
        except StateRevisionConflict:
            return self._states.get(**_state_identity(identity))

    def should_handle_event(self, body: Mapping[str, Any]) -> bool:
        identity = self._event_identity(body)
        if identity is None or not self._has_image(body):
            return False
        current = self._states.get(**_state_identity(identity))
        return (
            current is not None
            and current.flow_state in _MARK_ACCEPTING_STATES
            and self._is_wplus(current)
        )

    async def process_event(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        """Handle only an image submitted while the transaction explicitly waits for a mark."""
        identity = self._event_identity(body)
        if identity is None or not self._has_image(body):
            return None
        current = self._states.get(**_state_identity(identity))
        if current is None:
            return None
        if identity["event_id"] in current.processed_event_ids and current.flow_state in {
            "WAITING_WPLUS_MARK", "READY_FOR_MANUAL_TICKETING",
        }:
            return self._result(
                current, status=current.flow_state,
                transition_code="wplus_mark_duplicate_ignored",
            )
        if current.flow_state not in _MARK_ACCEPTING_STATES:
            return None
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        image_urls = payload.get("imageUrls", payload.get("image_urls"))
        image_url = image_urls[0] if isinstance(image_urls, list) and image_urls else None
        context = _text(payload.get("itemId") or payload.get("item_id")) or current.purchase_context_id
        if not context:
            return self._result(current, status="PURCHASE_CONTEXT_REQUIRED", transition_code="wplus_mark_context_missing")
        return await self.submit_mark(
            **_state_identity(identity), purchase_context_id=context,
            event_id=identity["event_id"], message_id=identity.get("message_id") or None,
            image_url=image_url,
        )

    async def submit_mark(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        purchase_context_id: str,
        event_id: str,
        image_url: str | None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        current = self._states.get(**identity)
        if current is None:
            return {"status": "TRANSACTION_NOT_FOUND", "handled": False}
        if not self._is_wplus(current):
            return self._result(current, status="NOT_WPLUS_AREA", transition_code="wplus_mark_not_applicable")
        if current.purchase_context_id and current.purchase_context_id != purchase_context_id:
            return self._result(current, status="PURCHASE_CONTEXT_MISMATCH", transition_code="wplus_mark_context_mismatch")
        if event_id in current.processed_event_ids:
            return self._result(
                current,
                status=("READY_FOR_MANUAL_TICKETING" if current.flow_state == "READY_FOR_MANUAL_TICKETING" else "MARK_SUBMITTED"),
                transition_code="wplus_mark_duplicate_ignored",
            )
        normalized_url = _image_reference(image_url)
        if normalized_url is None:
            mark_value = None
        else:
            mark_value = await self._detect(normalized_url)
        if mark_value is not True:
            code = "wplus_mark_missing" if mark_value is False else "wplus_mark_unknown"
            reply = self._missing_reply() if current.flow_state == "WAITING_WPLUS_MARK" else None
            before_flow = current.flow_state
            updated = self._transition(
                current, identity=identity, event_id=event_id, flow_state=current.flow_state,
                transition_code=code, updates={},
            )
            return self._result(
                updated, status="WAITING_WPLUS_MARK" if updated.flow_state == "WAITING_WPLUS_MARK" else "MARK_NOT_CONFIRMED",
                transition_code=code, mark_result=mark_value, reply=reply,
                outcome="UNMARKED_IMAGE" if mark_value is False else "UNKNOWN_MARK_RESULT",
                state_before=before_flow,
            )

        revision = current.wplus_mark_revision + 1
        reference = WplusMarkReference(
            revision=revision, **identity, purchase_context_id=purchase_context_id,
            image_reference=normalized_url, message_id=_text(message_id), event_id=event_id,
            submitted_at=_utc(self._now_provider()).isoformat(),
        )
        history = [*current.wplus_mark_history, reference][-20:]
        before_flow = current.flow_state
        target = (
            "READY_FOR_MANUAL_TICKETING"
            if current.flow_state == "WAITING_WPLUS_MARK" else current.flow_state
        )
        updated = self._transition(
            current, identity=identity, event_id=event_id, flow_state=target,
            transition_code="wplus_mark_submitted", updates={
                "purchase_context_id": purchase_context_id,
                "wplus_mark_status": "submitted", "wplus_mark_revision": revision,
                "current_wplus_mark": reference.model_dump(), "wplus_mark_history": [
                    item.model_dump() for item in history
                ],
                "fulfillment_status": "pending" if target == "READY_FOR_MANUAL_TICKETING" else current.fulfillment_status,
            },
        )
        return self._result(
            updated,
            status="READY_FOR_MANUAL_TICKETING" if target == "READY_FOR_MANUAL_TICKETING" else "MARK_SUBMITTED",
            transition_code="wplus_mark_submitted", mark_result=True, reply=None,
            state_before=before_flow,
        )

    async def apply_payment_validated(
        self,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        purchase_context_id: str,
        event_id: str,
        platform_order_id: str,
        payment_evidence: Mapping[str, Any] | None = None,
        request_type: str | None = None,
        provider_route: str | None = None,
        quote_record_id: str | None = None,
        transition_code: str = "payment_validated_fixture",
    ) -> dict[str, Any]:
        """Apply an authoritative payment result without any platform write."""
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        current = self._states.get(**identity)
        if current is None:
            return {"status": "TRANSACTION_NOT_FOUND", "handled": False}
        if current.purchase_context_id and current.purchase_context_id != purchase_context_id:
            return self._result(current, status="PURCHASE_CONTEXT_MISMATCH", transition_code="payment_fixture_context_mismatch")
        is_wplus = self._is_wplus(current) or (
            request_type == "WPLUS_AREA" and provider_route in {None, "WANDA_SELF"}
        )
        if not is_wplus:
            return self._result(current, status="NOT_WPLUS_AREA", transition_code="payment_fixture_not_wplus")
        if event_id in current.processed_event_ids:
            return self._result(current, status=current.flow_state, transition_code="payment_fixture_duplicate_ignored")
        has_mark = (
            current.current_wplus_mark is not None
            and current.current_wplus_mark.purchase_context_id == purchase_context_id
            and current.wplus_mark_status == "submitted"
        )
        target = "READY_FOR_MANUAL_TICKETING" if has_mark else "WAITING_WPLUS_MARK"
        before_flow = current.flow_state
        updated = self._transition(
            current, identity=identity, event_id=event_id, flow_state=target,
            transition_code=transition_code, updates={
                "purchase_context_id": purchase_context_id,
                "quote_request_type": request_type or current.quote_request_type,
                "quote_provider_route": provider_route or current.quote_provider_route,
                "active_quote_record_id": quote_record_id or current.active_quote_record_id,
                "order_id": _text(platform_order_id), "order_status": "paid",
                "payment_status": "verified_paid", "fulfillment_status": "pending",
                **({"payment_validation_evidence": dict(payment_evidence)} if payment_evidence else {}),
            },
        )
        return self._result(
            updated, status=target, transition_code=transition_code,
            mark_result=has_mark, reply=None, state_before=before_flow,
        )

    def _is_wplus(self, state: TransactionState) -> bool:
        if state.quote_request_type is not None:
            return state.quote_request_type == "WPLUS_AREA" and state.quote_provider_route in {None, "WANDA_SELF"}
        record_id = state.active_quote_record_id
        if not record_id or self._quotes is None:
            return False
        getter = getattr(self._quotes, "get_record", None)
        record = getter(tenant_id=state.tenant_id, record_id=record_id) if callable(getter) else None
        return bool(
            isinstance(record, Mapping)
            and record.get("request_type") == "WPLUS_AREA"
            and record.get("tenant_id") == state.tenant_id
            and record.get("shop_id") == state.shop_id
            and record.get("buyer_id") == state.buyer_id
            and record.get("chat_id") == state.chat_id
            and record.get("purchase_context_id") == state.purchase_context_id
        )

    async def _detect(self, image_url: str) -> bool | None:
        if self._detector is None:
            return None
        try:
            detector = getattr(self._detector, "detect", self._detector)
            value = detector(image_url)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    def _transition(
        self,
        current: TransactionState,
        *,
        identity: dict[str, str],
        event_id: str,
        flow_state: str,
        transition_code: str,
        updates: dict[str, Any],
    ) -> TransactionState:
        try:
            return self._states.transition(
                **identity, expected_revision=current.revision, event_id=event_id,
                transition_code=transition_code, flow_state=flow_state, updates=updates,
                allow_compatible_bootstrap=True,
            )
        except StateRevisionConflict:
            latest = self._states.get(**identity)
            if latest is None:
                raise
            return latest

    def _missing_reply(self) -> str:
        return str(getattr(self._templates(), WPLUS_MARK_REPLY_TEMPLATE_AUTHORITY))

    @staticmethod
    def _event_identity(body: Mapping[str, Any]) -> dict[str, str] | None:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
        values = {
            "event_id": _text(envelope.get("id") or envelope.get("event_id")),
            "tenant_id": _text(envelope.get("tenantId") or envelope.get("tenant_id")),
            "shop_id": _text(session.get("accountUnb") or session.get("account_unb") or payload.get("accountUnb") or payload.get("account_unb")),
            "buyer_id": _text(session.get("peerUnb") or session.get("peer_unb") or payload.get("peerUnb") or payload.get("peer_unb")),
            "chat_id": _text(session.get("chatId") or session.get("chat_id") or payload.get("chatId") or payload.get("chat_id")),
            "message_id": _text(payload.get("messageId") or payload.get("message_id") or payload.get("remoteMessageId") or payload.get("remote_message_id")) or "",
        }
        return values if all(values[key] for key in ("event_id", "tenant_id", "shop_id", "buyer_id", "chat_id")) else None

    @staticmethod
    def _has_image(body: Mapping[str, Any]) -> bool:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        images = payload.get("imageUrls", payload.get("image_urls"))
        return isinstance(images, list) and bool(images)

    @staticmethod
    def _result(
        state: TransactionState,
        *,
        status: str,
        transition_code: str,
        mark_result: bool | None = None,
        reply: str | None = None,
        outcome: str | None = None,
        state_before: str | None = None,
    ) -> dict[str, Any]:
        actions = []
        if reply:
            actions.append({
                "id": f"{state.last_transition_code}:wplus-mark-required",
                "type": "send_message", "text": reply, "rule_governed": True,
                "preserve_on_new_buyer_message": True,
                "dedupe_key": f"{state.state_id}:wplus-mark-required:{state.revision}",
            })
        return {
            "handled": True, "status": status, "flow_state": state.flow_state,
            "mark_result": mark_result, "outcome": outcome, "reply": reply,
            "quote_mutated": False, "binding_mutated": False, "repriced": False,
            "decision": {"mode": "auto", "actions": actions, "reason": transition_code},
            "rule_decision": {
                "state_before": state_before or state.flow_state,
                "state_after": state.flow_state, "transition_code": transition_code,
                "state_revision": state.revision,
                "actions": actions, "handoff_reason": None,
            },
        }


def _state_identity(identity: Mapping[str, str]) -> dict[str, str]:
    return {
        key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")
    }


def _identity(tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> dict[str, str]:
    values = {
        "tenant_id": _text(tenant_id), "shop_id": _text(shop_id),
        "buyer_id": _text(buyer_id), "chat_id": _text(chat_id),
    }
    if not all(values.values()):
        raise ValueError("wplus_mark_identity_invalid")
    return values  # type: ignore[return-value]


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _image_reference(value: object) -> str | None:
    text = _text(value)
    if not text or len(text) > 2_048:
        return None
    parsed = urlsplit(text)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in ("alicdn.com", "tbcdn.cn"))
    return text if parsed.scheme == "https" and hostname and allowed and not parsed.username and not parsed.password else None
