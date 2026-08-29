from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .rule_contracts import FlowState
from .settings_store import SecretProtector, default_secret_protector


QuoteStatus = Literal["none", "collecting", "ready", "expired", "invalidated"]
ConfirmationStatus = Literal["none", "pending", "confirmed", "invalidated"]
OrderStatus = Literal[
    "none", "unverified", "bound", "pending_payment", "paid", "shipped",
    "completed", "closed", "refund_pending", "refunded",
]
PriceChangeStatus = Literal[
    "none", "pending", "submitted", "succeeded", "unknown", "failed",
    "skipped", "rejected",
]
PaymentStatus = Literal[
    "unpaid", "verification_required", "verified_paid", "mismatch", "refund_pending", "refunded",
]
FulfillmentStatus = Literal["none", "pending", "claimed", "ticket_issued", "shipped", "completed"]
AutomationControl = Literal["active", "human_hold", "safety_hold"]


class StateRevisionConflict(RuntimeError):
    pass


class TransactionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_id: str = Field(pattern=r"^ts-[0-9a-f]{40}$")
    tenant_id: str = Field(min_length=1, max_length=200)
    shop_id: str = Field(min_length=1, max_length=200)
    buyer_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    flow_state: FlowState = "NEW"
    automation_control: AutomationControl = "active"
    quote_status: QuoteStatus = "none"
    confirmation_status: ConfirmationStatus = "none"
    order_status: OrderStatus = "none"
    price_change_status: PriceChangeStatus = "none"
    payment_status: PaymentStatus = "unpaid"
    fulfillment_status: FulfillmentStatus = "none"
    active_quote_record_id: str | None = Field(default=None, max_length=240)
    confirmed_quote_record_id: str | None = Field(default=None, max_length=240)
    confirmation_version: str | None = Field(default=None, max_length=240)
    confirmation_source: Literal["buyer_message", "order_created"] | None = None
    confirmation_event_id: str | None = Field(default=None, max_length=240)
    confirmed_ticket_count: int | None = Field(default=None, ge=1, le=20)
    order_id: str | None = Field(default=None, max_length=240)
    target_amount_cents: int | None = Field(default=None, ge=1, le=200_000)
    price_change_command_id: str | None = Field(default=None, max_length=240)
    fulfillment_task_id: str | None = Field(default=None, max_length=240)
    expected_inputs: list[str] = Field(default_factory=list, max_length=20)
    last_transition_code: str = Field(default="initialized", min_length=1, max_length=120)
    processed_event_ids: list[str] = Field(default_factory=list)
    updated_at: str


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW": frozenset({"NEW", "COLLECTING", "MANUAL_HOLD"}),
    "COLLECTING": frozenset({"COLLECTING", "FACTS_READY", "QUOTED", "PRICE_CHANGING", "ORDER_UNVERIFIED", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "CANCELLED"}),
    "FACTS_READY": frozenset({"FACTS_READY", "QUOTED", "COLLECTING", "ORDER_UNVERIFIED", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD"}),
    "QUOTED": frozenset({"QUOTED", "CONFIRMED", "PRICE_CHANGING", "QUOTE_EXPIRED", "ORDER_UNVERIFIED", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "CANCELLED"}),
    "CONFIRMED": frozenset({"CONFIRMED", "ORDER_BOUND", "PRICE_CHANGING", "QUOTE_EXPIRED", "ORDER_UNVERIFIED", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "CANCELLED"}),
    "ORDER_BOUND": frozenset({"ORDER_BOUND", "PRICE_CHANGING", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "ORDER_UNVERIFIED", "MANUAL_HOLD", "CANCELLED"}),
    "PRICE_CHANGING": frozenset({"PRICE_CHANGING", "WAITING_PAYMENT", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "ORDER_UNVERIFIED", "CANCELLED"}),
    "WAITING_PAYMENT": frozenset({"WAITING_PAYMENT", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "CANCELLED", "REFUND_PENDING"}),
    "PAID_WAITING_FULFILLMENT": frozenset({"PAID_WAITING_FULFILLMENT", "FULFILLMENT_IN_PROGRESS", "TICKET_SENT", "COMPLETED", "REFUND_PENDING", "MANUAL_HOLD"}),
    "FULFILLMENT_IN_PROGRESS": frozenset({"FULFILLMENT_IN_PROGRESS", "TICKET_SENT", "COMPLETED", "REFUND_PENDING", "MANUAL_HOLD"}),
    "TICKET_SENT": frozenset({"TICKET_SENT", "COMPLETED", "REFUND_PENDING", "MANUAL_HOLD"}),
    "COMPLETED": frozenset({"COMPLETED"}),
    "QUOTE_EXPIRED": frozenset({"QUOTE_EXPIRED", "COLLECTING", "FACTS_READY", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD"}),
    "ORDER_UNVERIFIED": frozenset({"ORDER_UNVERIFIED", "ORDER_BOUND", "PAID_WAITING_FULFILLMENT", "TICKET_SENT", "COMPLETED", "MANUAL_HOLD", "CANCELLED"}),
    "MANUAL_HOLD": frozenset({
        "MANUAL_HOLD", "COLLECTING", "QUOTED", "CONFIRMED", "ORDER_BOUND",
        "PRICE_CHANGING", "WAITING_PAYMENT", "ORDER_UNVERIFIED", "PAID_WAITING_FULFILLMENT",
        "TICKET_SENT", "COMPLETED", "CANCELLED", "REFUND_PENDING",
    }),
    "CANCELLED": frozenset({"CANCELLED", "REFUND_PENDING", "REFUNDED"}),
    "REFUND_PENDING": frozenset({"REFUND_PENDING", "REFUNDED", "MANUAL_HOLD"}),
    "REFUNDED": frozenset({"REFUNDED"}),
}

_MUTABLE_FIELDS = frozenset(TransactionState.model_fields) - {
    "state_id", "tenant_id", "shop_id", "buyer_id", "chat_id", "generation", "revision",
    "flow_state", "last_transition_code", "processed_event_ids", "updated_at",
}


class TransactionStateStore:
    """Encrypted tenant/shop/chat state with revision CAS and event idempotency."""

    def __init__(
        self, path: Path, *, protector: SecretProtector | None = None,
        max_states: int = 10_000, max_event_ids: int = 200,
    ) -> None:
        self._path = path
        self._protector = protector or default_secret_protector()
        self._max_states = max(1, int(max_states))
        self._max_event_ids = max(1, min(int(max_event_ids), 1_000))
        self._lock = RLock()

    @staticmethod
    def _identity(tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> tuple[str, str, str, str]:
        values = tuple(str(value or "").strip() for value in (tenant_id, shop_id, buyer_id, chat_id))
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("transaction_state_identity_invalid")
        return values  # type: ignore[return-value]

    @staticmethod
    def _state_id(identity: tuple[str, str, str, str]) -> str:
        return "ts-" + hashlib.sha256("\0".join(identity).encode()).hexdigest()[:40]

    def get(self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> TransactionState | None:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        state_id = self._state_id(identity)
        with self._lock:
            item = next((value for value in self._read() if value.get("state_id") == state_id), None)
        return TransactionState.model_validate(item) if item else None

    def find_by_order(self, *, tenant_id: str, order_id: str) -> TransactionState | None:
        tenant = str(tenant_id or "").strip()
        order = str(order_id or "").strip()
        if not tenant or not order:
            return None
        with self._lock:
            matches = [
                TransactionState.model_validate(item)
                for item in self._read()
                if item.get("tenant_id") == tenant and item.get("order_id") == order
            ]
        matches.sort(key=lambda item: item.updated_at, reverse=True)
        return matches[0] if len(matches) == 1 else None

    def get_or_create(self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> TransactionState:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        with self._lock:
            records = self._read()
            state_id = self._state_id(identity)
            item = next((value for value in records if value.get("state_id") == state_id), None)
            if item is None:
                now = datetime.now(timezone.utc).isoformat()
                state = TransactionState(
                    state_id=state_id, tenant_id=identity[0], shop_id=identity[1],
                    buyer_id=identity[2], chat_id=identity[3], updated_at=now,
                )
                records.append(state.model_dump())
                self._write(records)
                return state
            return TransactionState.model_validate(item)

    def transition(
        self, *, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str,
        expected_revision: int, event_id: str, transition_code: str,
        flow_state: FlowState, updates: dict[str, Any],
        allow_compatible_bootstrap: bool = False,
    ) -> TransactionState:
        identity = self._identity(tenant_id, shop_id, buyer_id, chat_id)
        normalized_event_id = str(event_id or "").strip()
        normalized_code = str(transition_code or "").strip()
        if not normalized_event_id or len(normalized_event_id) > 240:
            raise ValueError("transaction_state_event_id_invalid")
        if not normalized_code or len(normalized_code) > 120:
            raise ValueError("transaction_state_transition_code_invalid")
        invalid_fields = set(updates) - _MUTABLE_FIELDS
        if invalid_fields:
            raise ValueError("transaction_state_update_field_invalid")
        with self._lock:
            records = self._read()
            state_id = self._state_id(identity)
            index = next((i for i, value in enumerate(records) if value.get("state_id") == state_id), None)
            current = (
                TransactionState.model_validate(records[index])
                if index is not None
                else TransactionState(
                    state_id=state_id, tenant_id=identity[0], shop_id=identity[1],
                    buyer_id=identity[2], chat_id=identity[3],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            if normalized_event_id in current.processed_event_ids:
                return current
            if current.revision != expected_revision:
                raise StateRevisionConflict("transaction_state_revision_conflict")
            compatible_bootstrap = (
                current.revision == 0
                and current.flow_state == "NEW"
                and (allow_compatible_bootstrap or normalized_code.startswith("compat.bootstrap."))
            )
            if flow_state not in _ALLOWED_TRANSITIONS[current.flow_state] and not compatible_bootstrap:
                raise ValueError("transaction_state_transition_invalid")
            payload = current.model_dump()
            payload.update(updates)
            payload.update({
                "revision": current.revision + 1,
                "flow_state": flow_state,
                "last_transition_code": normalized_code,
                "processed_event_ids": [
                    *current.processed_event_ids, normalized_event_id,
                ][-self._max_event_ids :],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            updated = TransactionState.model_validate(payload)
            if index is None:
                records.append(updated.model_dump())
            else:
                records[index] = updated.model_dump()
            self._write(records)
            return updated

    def _read(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            protected = payload.get("states_protected") if isinstance(payload, dict) else None
            decoded = json.loads(self._protector.unprotect(protected)) if isinstance(protected, str) else []
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            return []
        return [item for item in decoded if isinstance(item, dict)] if isinstance(decoded, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        bounded = records[: self._max_states]
        protected = self._protector.protect(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")))
        payload = {"version": 1, "states_protected": protected}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._path)
