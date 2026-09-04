from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Mapping

from app.quote_record_store import QuoteRecordStore


BindingStatus = str
NEW_FLOW_TRANSACTION_STATE_AUTHORITY = "rules_first_sqlite"
NEW_FLOW_VERSION = "V4_NEW_FLOW_V2"
NEW_FLOW_SOURCE = "phase_9a_authorization"


@dataclass(frozen=True)
class CanonicalRepriceCommand:
    """The single Backend → RulesFirst → Plugin price-change identity."""

    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    platform_order_id: str
    quote_id: str
    quote_hash: str
    quote_generation: int
    binding_revision: int
    quote_version: str
    current_amount_fen: int
    target_amount_fen: int
    transaction_revision: int
    idempotency_key: str
    flow_version: str = NEW_FLOW_VERSION
    source: str = NEW_FLOW_SOURCE

    def to_plugin_action(self, *, action_id: str) -> dict[str, Any]:
        """Render the existing Plugin action without enqueueing it."""
        snapshot = {
            "quote_version": self.quote_version,
            "quote_id": self.quote_id,
            "quote_hash": self.quote_hash,
            "terms_fingerprint": self.quote_hash,
            "quote_generation": self.quote_generation,
            "binding_revision": self.binding_revision,
            "order_id": self.platform_order_id,
            "tenant_id": self.tenant_id,
            "shop_id": self.shop_id,
            "buyer_id": self.buyer_id,
            "chat_id": self.chat_id,
            # The existing Plugin wire name is cents; fen and cents are the
            # same integer unit here. There is only one amount authority.
            "target_amount_cents": self.target_amount_fen,
            "observed_order_amount_cents": self.current_amount_fen,
            "transaction_revision": self.transaction_revision,
            "idempotency_key": self.idempotency_key,
            "flow_version": self.flow_version,
            "source": self.source,
        }
        return {
            "id": action_id,
            "type": "change_order_price",
            "flow_version": self.flow_version,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "transaction_revision": self.transaction_revision,
            "tenant_id": self.tenant_id,
            "shop_id": self.shop_id,
            "buyer_id": self.buyer_id,
            "chat_id": self.chat_id,
            "platform_order_id": self.platform_order_id,
            "current_amount_fen": self.current_amount_fen,
            "target_amount_fen": self.target_amount_fen,
            "quote_snapshot": snapshot,
        }


@dataclass(frozen=True)
class OrderQuoteBindingResult:
    status: BindingStatus
    quote_id: str | None = None
    quote_hash: str | None = None
    generation: int | None = None
    binding_revision: int | None = None
    would_reprice_to_fen: int | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None


class OrderQuoteBindingV2Service:
    """Deterministic order-to-quote binding; never performs a price write."""

    def __init__(
        self,
        store: QuoteRecordStore,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def bind_order(
        self,
        platform_order_id: str,
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        order_created_at: datetime | None = None,
        request_id: str | None = None,
    ) -> OrderQuoteBindingResult:
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        order_id = _required(platform_order_id, "platform_order_id")
        reference = _utc(order_created_at or self._now_provider())
        with self._lock:
            duplicate = self._existing_order_binding(order_id, identity)
            if duplicate is not None:
                if duplicate.get("identity_match") is False:
                    return OrderQuoteBindingResult("REJECTED", reason="ORDER_BINDING_CONFLICT")
                return _already_bound(duplicate["record"])
            status, candidates = self._eligible_candidates(identity, reference)
            if len(candidates) == 1:
                return self._persist_binding(
                    candidates[0], identity, order_id,
                    request_id=request_id, bound_at=_utc(self._now_provider()),
                    reason="UNIQUE_ACTIVE_QUOTE",
                )
            if len(candidates) > 1:
                return OrderQuoteBindingResult(
                    "MULTIPLE_ACTIVE_QUOTES",
                    candidates=[_candidate(item) for item in candidates],
                )
            return OrderQuoteBindingResult(status)

    def bind_selected_quote(
        self,
        platform_order_id: str,
        quote_id: str,
        identity: Mapping[str, str],
        *,
        order_created_at: datetime | None = None,
        request_id: str | None = None,
    ) -> OrderQuoteBindingResult:
        normalized_identity = _identity(
            identity.get("tenant_id"), identity.get("shop_id"),
            identity.get("buyer_id"), identity.get("chat_id"),
        )
        order_id = _required(platform_order_id, "platform_order_id")
        wanted_quote = _required(quote_id, "quote_id")
        reference = _utc(order_created_at or self._now_provider())
        with self._lock:
            duplicate = self._existing_order_binding(order_id, normalized_identity)
            if duplicate is not None:
                if duplicate.get("identity_match") is False:
                    return OrderQuoteBindingResult("REJECTED", reason="ORDER_BINDING_CONFLICT")
                return _already_bound(duplicate["record"])
            _, candidates = self._eligible_candidates(normalized_identity, reference)
            selected = next((item for item in candidates if item.get("quote_id") == wanted_quote), None)
            if selected is None:
                return OrderQuoteBindingResult("REJECTED", reason="QUOTE_NOT_ELIGIBLE")
            return self._persist_binding(
                selected, normalized_identity, order_id,
                request_id=request_id, bound_at=_utc(self._now_provider()),
                reason="EXPLICIT_QUOTE_SELECTION",
            )

    def get_bound_quote(
        self,
        platform_order_id: str,
        identity: Mapping[str, str],
    ) -> dict[str, Any] | None:
        """Read the persisted binding only; never search for a replacement quote."""
        normalized_identity = _identity(
            identity.get("tenant_id"), identity.get("shop_id"),
            identity.get("buyer_id"), identity.get("chat_id"),
        )
        wanted_order = _required(platform_order_id, "platform_order_id")
        return self.store.get_bound_order_quote(
            tenant_id=normalized_identity[0], shop_id=normalized_identity[1],
            buyer_id=normalized_identity[2], chat_id=normalized_identity[3],
            platform_order_id=wanted_order,
        )

    def _eligible_candidates(
        self,
        identity: tuple[str, str, str, str],
        reference: datetime,
    ) -> tuple[str, list[dict[str, Any]]]:
        return self.store.list_transaction_candidates(
            tenant_id=identity[0], shop_id=identity[1],
            buyer_id=identity[2], chat_id=identity[3], at=reference,
        )

    def _existing_order_binding(
        self,
        platform_order_id: str,
        identity: tuple[str, str, str, str],
    ) -> dict[str, Any] | None:
        for item in self.store.list(identity[0], limit=500):
            stored_order = str(item.get("platform_order_id") or item.get("order_id") or "").strip()
            if stored_order != platform_order_id:
                continue
            return {
                "record": item,
                "identity_match": _record_identity(item) == identity,
            }
        return None

    def _persist_binding(
        self,
        candidate: dict[str, Any],
        identity: tuple[str, str, str, str],
        platform_order_id: str,
        *,
        request_id: str | None,
        bound_at: datetime,
        reason: str,
    ) -> OrderQuoteBindingResult:
        bound = self.store.bind_order(
            record_id=str(candidate["record_id"]), tenant_id=identity[0], shop_id=identity[1],
            buyer_id=identity[2], chat_id=identity[3], order_id=platform_order_id,
            bound_at=bound_at,
        )
        if bound is None:
            return OrderQuoteBindingResult("REJECTED", reason="BINDING_CONFLICT")
        revision = 1
        history = [{
            "revision": revision,
            "platform_order_id": platform_order_id,
            "quote_id": candidate["quote_id"],
            "binding_reason": reason,
        }]
        saved = self.store.save({
            **bound,
            "platform_order_id": platform_order_id,
            "binding_request_id": request_id,
            "bound_at": bound_at.isoformat(),
            "binding_revision": revision,
            "binding_reason": reason,
            "binding_history": history,
        })
        return OrderQuoteBindingResult(
            status="BOUND", quote_id=str(saved["quote_id"]),
            quote_hash=str(saved.get("quote_hash") or saved.get("terms_fingerprint") or "") or None,
            generation=_generation(saved), binding_revision=revision,
            would_reprice_to_fen=_positive_amount(saved.get("total_sell_price_fen")),
        )


@dataclass(frozen=True)
class OrderRepriceAuthorizationResult:
    status: str
    platform_order_id: str | None = None
    quote_id: str | None = None
    quote_version: str | None = None
    quote_hash: str | None = None
    quote_generation: int | None = None
    binding_revision: int | None = None
    current_amount_fen: int | None = None
    target_amount_fen: int | None = None
    idempotency_key: str | None = None
    transaction_revision: int | None = None
    reason: str | None = None
    command_created: bool = False


class OrderRepriceAuthorizationService:
    """Read-only authorization from an existing persisted order binding."""

    def __init__(
        self,
        store: QuoteRecordStore,
        *,
        now_provider: Callable[[], datetime] | None = None,
        transaction_state_store: object | None = None,
    ) -> None:
        self.store = store
        if (
            transaction_state_store is not None
            and getattr(transaction_state_store, "authority_name", None)
            != NEW_FLOW_TRANSACTION_STATE_AUTHORITY
        ):
            raise ValueError("new_flow_transaction_state_authority_invalid")
        self._binding = OrderQuoteBindingV2Service(store, now_provider=now_provider)
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._transaction_state_store = transaction_state_store
        self._lock = RLock()

    def authorize(
        self,
        platform_order_id: str,
        order: Mapping[str, Any],
        *,
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        order_created_at: datetime | None = None,
        request_id: str | None = None,
        expected_quote_id: str | None = None,
        expected_quote_hash: str | None = None,
        expected_generation: int | None = None,
        expected_binding_revision: int | None = None,
    ) -> OrderRepriceAuthorizationResult:
        identity = {"tenant_id": tenant_id, "shop_id": shop_id, "buyer_id": buyer_id, "chat_id": chat_id}
        normalized_order_id = _required(platform_order_id, "platform_order_id")
        reference = _utc(order_created_at or self._now_provider())
        with self._lock:
            bound = self._binding.get_bound_quote(normalized_order_id, identity)
            if bound is None:
                return OrderRepriceAuthorizationResult("NO_BOUND_QUOTE", platform_order_id=normalized_order_id)
            quote_hash = str(bound.get("quote_hash") or bound.get("terms_fingerprint") or "").strip() or None
            quote_version = str(bound.get("quote_version") or "").strip() or None
            transaction_state = _transaction_state(self._transaction_state_store, identity)
            # A first order.created may not have a transaction row yet. The
            # RulesFirst runtime bootstraps it atomically when the command is
            # drained; the canonical command still needs an explicit baseline.
            transaction_revision = _transaction_revision(transaction_state) or 0
            if bound.get("quote_hash") and bound.get("terms_fingerprint") and bound["quote_hash"] != bound["terms_fingerprint"]:
                return _quote_invalid(normalized_order_id, bound, "QUOTE_HASH_MISMATCH")
            if (
                expected_quote_id is not None and expected_quote_id != bound.get("quote_id")
                or expected_quote_hash is not None and expected_quote_hash != quote_hash
                or expected_generation is not None and expected_generation != bound.get("generation")
                or expected_binding_revision is not None and expected_binding_revision != bound.get("binding_revision")
            ):
                return _quote_invalid(normalized_order_id, bound, "BOUND_SNAPSHOT_MISMATCH")
            if bound.get("quote_state") == "SUPERSEDED" or bound.get("status") == "superseded" or bound.get("invalidated_reason"):
                return _quote_invalid(normalized_order_id, bound, "QUOTE_SUPERSEDED")
            generation = _positive_generation(bound)
            binding_revision = _positive_binding_revision(bound)
            if not bound.get("quote_id") or not quote_hash or generation is None or binding_revision is None:
                return _quote_invalid(normalized_order_id, bound, "QUOTE_BINDING_LINEAGE_INVALID")
            target = _positive_amount(bound.get("total_sell_price_fen"))
            if bound.get("transaction_authorized") is not True or target is None:
                return _quote_invalid(normalized_order_id, bound, "QUOTE_NOT_TRANSACTION_READY")
            if not self.store.is_quote_active(bound, at=reference):
                return OrderRepriceAuthorizationResult(
                    "QUOTE_EXPIRED", platform_order_id=normalized_order_id,
                    quote_id=str(bound["quote_id"]), quote_version=quote_version,
                    quote_hash=quote_hash, quote_generation=generation,
                    binding_revision=binding_revision, transaction_revision=transaction_revision,
                )
            if not isinstance(order, Mapping):
                return OrderRepriceAuthorizationResult("INPUT_INCOMPLETE", platform_order_id=normalized_order_id, reason="ORDER_FACTS_REQUIRED")
            if not _order_identity_matches(order, identity, normalized_order_id):
                return OrderRepriceAuthorizationResult("IDENTITY_MISMATCH", platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]), reason="ORDER_BINDING_IDENTITY_MISMATCH")
            raw_status = str(order.get("order_status") or order.get("status") or "").strip().lower()
            status = "unpaid" if raw_status in {
                "1", "unpaid", "pending", "pending_payment", "待付款", "待支付",
            } else raw_status
            current_amount = _positive_amount(
                order.get("current_amount_fen", order.get("amount_cents", order.get("payment"))),
            )
            if not status or current_amount is None:
                return OrderRepriceAuthorizationResult("INPUT_INCOMPLETE", platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]), reason="ORDER_FACTS_INCOMPLETE")
            if _manual_takeover(transaction_state):
                return OrderRepriceAuthorizationResult("MANUAL_TAKEOVER", platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]), reason="AUTOMATION_CONTROL_HUMAN_HOLD")
            if status != "unpaid" or _positive_amount(order.get("paid_amount_fen")) is not None:
                return OrderRepriceAuthorizationResult("ORDER_NOT_UNPAID", platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]), current_amount_fen=current_amount, target_amount_fen=target)
            if current_amount == target:
                return OrderRepriceAuthorizationResult("ALREADY_PRICED", platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]), quote_hash=quote_hash, quote_generation=generation, binding_revision=binding_revision, current_amount_fen=current_amount, target_amount_fen=target)
            key = _price_change_idempotency_key(
                tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                platform_order_id=normalized_order_id, quote_id=str(bound["quote_id"]),
                quote_hash=quote_hash, quote_generation=generation,
                binding_revision=binding_revision, target_amount_fen=target,
            )
            return OrderRepriceAuthorizationResult(
                "REPRICE_READY", platform_order_id=normalized_order_id,
                quote_id=str(bound["quote_id"]), quote_version=quote_version,
                quote_hash=quote_hash, quote_generation=generation,
                binding_revision=binding_revision, current_amount_fen=current_amount,
                target_amount_fen=target, idempotency_key=key,
                transaction_revision=transaction_revision,
            )

    def mark_new_flow_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Mark an authorized-flow event without creating a command."""
        return _mark_new_flow_event(event)

    def enqueue_authorized_reprice(
        self,
        result: OrderRepriceAuthorizationResult,
        *,
        rules_first_store: object,
        event: Mapping[str, Any],
        tenant_id: str,
        shop_id: str,
        buyer_id: str,
        chat_id: str,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one authorized command through the existing RulesFirst outbox.

        The command is rendered before the event is persisted, so every
        non-ready authorization fails without creating an inbox or outbox row.
        The event is marked as New Flow to keep the legacy decision engine
        inert; command durability and execution remain the existing store and
        Plugin responsibilities.
        """
        identity = _identity(tenant_id, shop_id, buyer_id, chat_id)
        command_event = _mark_new_flow_event(event)
        envelope = command_event.get("envelope")
        if not isinstance(envelope, Mapping):
            raise ValueError("reprice_event_invalid")
        event_tenant = _required(
            envelope.get("tenantId") or envelope.get("tenant_id"), "event_tenant_id",
        )
        if event_tenant != identity[0]:
            raise ValueError("reprice_event_identity_mismatch")
        event_id = _required(
            envelope.get("id") or envelope.get("eventId") or envelope.get("event_id"),
            "event_id",
        )
        command = build_canonical_reprice_command(
            result,
            tenant_id=identity[0], shop_id=identity[1],
            buyer_id=identity[2], chat_id=identity[3],
            action_id=action_id or f"{event_id}:change-order-price",
        )
        enqueue_event = getattr(rules_first_store, "enqueue_event", None)
        append_commands = getattr(rules_first_store, "append_commands", None)
        if not callable(enqueue_event) or not callable(append_commands):
            raise ValueError("rules_first_outbox_unavailable")
        accepted = enqueue_event(command_event)
        effective_event_id = _required(accepted.get("event_id"), "event_id")
        commands = append_commands(
            tenant_id=identity[0], event_id=effective_event_id,
            commands=[command], state_revision=result.transaction_revision,
        )
        if not commands:
            raise RuntimeError("reprice_command_persist_failed")
        return {
            "accepted": True,
            "duplicate": bool(accepted.get("duplicate")),
            "event_id": effective_event_id,
            "command": commands[0],
        }


def build_canonical_reprice_command(
    result: OrderRepriceAuthorizationResult, *,
    tenant_id: str,
    shop_id: str,
    buyer_id: str,
    chat_id: str,
    action_id: str,
) -> dict[str, Any]:
    """Render the canonical command fixture; this never enqueues or writes."""
    values = (
        result.platform_order_id, result.quote_id, result.quote_hash,
        result.quote_generation, result.binding_revision, result.quote_version,
        result.current_amount_fen, result.target_amount_fen,
        result.transaction_revision, result.idempotency_key,
    )
    if result.status != "REPRICE_READY" or any(value is None for value in values):
        raise ValueError("reprice_command_requires_ready_authorization")
    if (
        not isinstance(result.current_amount_fen, int)
        or isinstance(result.current_amount_fen, bool)
        or result.current_amount_fen <= 0
        or not isinstance(result.target_amount_fen, int)
        or isinstance(result.target_amount_fen, bool)
        or result.target_amount_fen <= 0
        or not isinstance(result.transaction_revision, int)
        or isinstance(result.transaction_revision, bool)
        or result.transaction_revision < 0
    ):
        raise ValueError("reprice_command_amount_or_revision_invalid")
    expected_key = _price_change_idempotency_key(
        tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
        platform_order_id=result.platform_order_id, quote_id=result.quote_id,
        quote_hash=result.quote_hash, quote_generation=result.quote_generation,
        binding_revision=result.binding_revision, target_amount_fen=result.target_amount_fen,
    )
    if result.idempotency_key != expected_key:
        raise ValueError("reprice_command_identity_mismatch")
    return CanonicalRepriceCommand(
        tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
        platform_order_id=result.platform_order_id,
        quote_id=result.quote_id, quote_hash=result.quote_hash,
        quote_generation=result.quote_generation, binding_revision=result.binding_revision,
        quote_version=result.quote_version, current_amount_fen=result.current_amount_fen,
        target_amount_fen=result.target_amount_fen,
        transaction_revision=result.transaction_revision,
        idempotency_key=result.idempotency_key,
    ).to_plugin_action(action_id=action_id)


def _mark_new_flow_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("reprice_event_invalid")
    marked = dict(event)
    envelope = event.get("envelope")
    if not isinstance(envelope, Mapping):
        raise ValueError("reprice_event_invalid")
    marked_envelope = dict(envelope)
    marked_envelope.update({"flow_version": NEW_FLOW_VERSION, "source": NEW_FLOW_SOURCE})
    payload = envelope.get("payload")
    if isinstance(payload, Mapping):
        marked_payload = dict(payload)
        marked_payload.update({"flow_version": NEW_FLOW_VERSION, "source": NEW_FLOW_SOURCE})
        marked_envelope["payload"] = marked_payload
    marked["envelope"] = marked_envelope
    return marked


def _already_bound(record: dict[str, Any]) -> OrderQuoteBindingResult:
    return OrderQuoteBindingResult(
        status="ALREADY_BOUND", quote_id=str(record.get("quote_id") or ""),
        quote_hash=str(record.get("quote_hash") or record.get("terms_fingerprint") or "") or None,
        generation=_generation(record), binding_revision=_binding_revision(record),
        would_reprice_to_fen=_positive_amount(record.get("total_sell_price_fen")),
    )


def _candidate(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "quote_id": record.get("quote_id"),
        "movie": record.get("movie"),
        "show_date": record.get("quote_date"),
        "start_time": record.get("showtime_start"),
        "hall": record.get("hall"),
        "selected_seats": record.get("selected_seats", []),
        "ticket_count": record.get("ticket_count"),
        "unit_sell_price_fen": record.get("unit_sell_price_fen"),
        "total_sell_price_fen": record.get("total_sell_price_fen"),
        "expires_at": record.get("expires_at"),
    }


def _identity(*values: str | None) -> tuple[str, str, str, str]:
    fields = ("tenant_id", "shop_id", "buyer_id", "chat_id")
    normalized = tuple(_required(value, field) for value, field in zip(values, fields, strict=True))
    return normalized  # type: ignore[return-value]


def _record_identity(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return tuple(str(record.get(field) or "").strip() for field in (
        "tenant_id", "shop_id", "buyer_id", "chat_id",
    ))  # type: ignore[return-value]


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200:
        raise ValueError(f"{field}_invalid")
    return text


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("datetime_invalid")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _generation(record: dict[str, Any]) -> int | None:
    value = record.get("generation")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_generation(record: dict[str, Any]) -> int | None:
    value = _generation(record)
    return value if value is not None and value > 0 else None


def _binding_revision(record: dict[str, Any]) -> int | None:
    value = record.get("binding_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_binding_revision(record: dict[str, Any]) -> int | None:
    value = _binding_revision(record)
    return value if value is not None and value > 0 else None


def _positive_amount(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _transaction_state(store: object | None, identity: Mapping[str, str]) -> object | None:
    getter = getattr(store, "get", None)
    return getter(**identity) if callable(getter) else None


def _transaction_revision(state: object | None) -> int | None:
    revision = getattr(state, "revision", None)
    return revision if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0 else None


def _quote_invalid(
    platform_order_id: str,
    record: dict[str, Any],
    reason: str,
) -> OrderRepriceAuthorizationResult:
    return OrderRepriceAuthorizationResult(
        "QUOTE_INVALID", platform_order_id=platform_order_id,
        quote_id=str(record.get("quote_id") or "") or None,
        quote_hash=str(record.get("quote_hash") or record.get("terms_fingerprint") or "") or None,
        quote_generation=_positive_generation(record),
        binding_revision=_positive_binding_revision(record), reason=reason,
    )


def _order_identity_matches(
    order: Mapping[str, Any],
    identity: Mapping[str, str],
    platform_order_id: str,
) -> bool:
    order_id = order.get("platform_order_id", order.get("order_id"))
    if order_id is not None and str(order_id).strip() != platform_order_id:
        return False
    for identity_field in ("tenant_id", "shop_id", "buyer_id"):
        value = order.get(identity_field)
        if value is None or str(value).strip() != identity[identity_field]:
            return False
    chat = order.get("chat_id")
    return chat is None or str(chat).strip() == identity["chat_id"]


def _manual_takeover(state: object | None) -> bool:
    return getattr(state, "automation_control", None) == "human_hold"


def canonical_reprice_identity(
    *,
    platform_order_id: str,
    quote_id: str,
    quote_generation: int,
    binding_revision: int,
    target_amount_fen: int,
) -> tuple[str, str, int, int, int]:
    """Return the cross-runtime identity used for one authorized reprice.

    Ownership, quote hash, and transaction revision remain mandatory command
    bindings, but this five-field identity is the single dedupe/receipt key
    input shared with the Plugin. Keeping the ordered tuple explicit avoids
    language-specific object-key ordering differences.
    """
    order_id = _required(platform_order_id, "platform_order_id")
    quote_key = _required(quote_id, "quote_id")
    if not isinstance(quote_generation, int) or isinstance(quote_generation, bool) or quote_generation < 1:
        raise ValueError("quote_generation_invalid")
    if not isinstance(binding_revision, int) or isinstance(binding_revision, bool) or binding_revision < 1:
        raise ValueError("binding_revision_invalid")
    if not isinstance(target_amount_fen, int) or isinstance(target_amount_fen, bool) or target_amount_fen <= 0:
        raise ValueError("target_amount_fen_invalid")
    return order_id, quote_key, quote_generation, binding_revision, target_amount_fen


def canonical_reprice_idempotency_key(
    *,
    platform_order_id: str,
    quote_id: str,
    quote_generation: int,
    binding_revision: int,
    target_amount_fen: int,
) -> str:
    identity = canonical_reprice_identity(
        platform_order_id=platform_order_id, quote_id=quote_id,
        quote_generation=quote_generation, binding_revision=binding_revision,
        target_amount_fen=target_amount_fen,
    )
    material = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    digest = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()).decode().rstrip("=")
    return f"price_change:v1:{digest}"


def _price_change_idempotency_key(
    *,
    tenant_id: str,
    shop_id: str,
    buyer_id: str,
    chat_id: str,
    platform_order_id: str,
    quote_id: str,
    quote_hash: str,
    quote_generation: int,
    binding_revision: int,
    target_amount_fen: int,
) -> str:
    # Keep the legacy private call signature for existing callers. The
    # canonical identity intentionally contains only the five reprice facts;
    # ownership and quote hash are independently validated in the command.
    return canonical_reprice_idempotency_key(
        platform_order_id=platform_order_id, quote_id=quote_id,
        quote_generation=quote_generation, binding_revision=binding_revision,
        target_amount_fen=target_amount_fen,
    )
