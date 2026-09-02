from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.transaction_state_store import StateRevisionConflict, TransactionStateStore


class Protector:
    def protect(self, value: str) -> str:
        return "sealed:" + value[::-1]

    def unprotect(self, value: str) -> str:
        if not value.startswith("sealed:"):
            raise ValueError("invalid")
        return value.removeprefix("sealed:")[::-1]


def identity() -> dict[str, str]:
    return {
        "tenant_id": "tenant-1",
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
    }


def test_state_store_initializes_and_applies_revision_cas_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "transaction-states.json"
    store = TransactionStateStore(path, protector=Protector())

    initial = store.get_or_create(**identity())
    assert initial.revision == 0
    assert initial.flow_state == "NEW"
    assert initial.quote_status == "none"

    updated = store.transition(
        **identity(), expected_revision=0, event_id="event-1",
        transition_code="buyer_message_received",
        flow_state="COLLECTING",
        updates={"expected_inputs": ["city", "cinema", "movie", "date", "showtime"]},
    )
    duplicate = store.transition(
        **identity(), expected_revision=0, event_id="event-1",
        transition_code="buyer_message_received",
        flow_state="COLLECTING",
        updates={"expected_inputs": ["ignored"]},
    )

    assert updated.revision == 1
    assert duplicate == updated
    assert updated.last_transition_code == "buyer_message_received"
    assert updated.expected_inputs == ["city", "cinema", "movie", "date", "showtime"]

    with pytest.raises(StateRevisionConflict):
        store.transition(
            **identity(), expected_revision=0, event_id="event-2",
            transition_code="stale_writer", flow_state="FACTS_READY", updates={},
        )

    reloaded = TransactionStateStore(path, protector=Protector()).get(**identity())
    assert reloaded == updated
    raw = path.read_text(encoding="utf-8")
    assert "buyer-1" not in raw
    assert "chat-1" not in raw
    assert json.loads(raw)["version"] == 1


def test_state_store_rejects_illegal_transition_and_unapproved_fields(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector())
    store.get_or_create(**identity())

    with pytest.raises(ValueError, match="transaction_state_transition_invalid"):
        store.transition(
            **identity(), expected_revision=0, event_id="event-jump",
            transition_code="jump", flow_state="COMPLETED", updates={},
        )
    bootstrapped = store.transition(
        **identity(), expected_revision=0, event_id="event-bootstrap",
        transition_code="compat.bootstrap.order_paid", flow_state="PAID_WAITING_FULFILLMENT",
        updates={"order_status": "paid", "payment_status": "verified_paid"},
    )
    assert bootstrapped.flow_state == "PAID_WAITING_FULFILLMENT"

    recoverable = TransactionStateStore(tmp_path / "recoverable-states.json", protector=Protector())
    current = recoverable.get_or_create(**identity())
    current = recoverable.transition(
        **identity(), expected_revision=current.revision, event_id="event-collect",
        transition_code="collect", flow_state="COLLECTING", updates={},
    )
    current = recoverable.transition(
        **identity(), expected_revision=current.revision, event_id="event-quote",
        transition_code="quote", flow_state="QUOTED", updates={"quote_status": "ready"},
    )
    current = recoverable.transition(
        **identity(), expected_revision=current.revision, event_id="event-quote-failed",
        transition_code="quote_input_incomplete", flow_state="COLLECTING", updates={"quote_status": "collecting"},
    )
    assert current.flow_state == "COLLECTING"

    second_store = TransactionStateStore(tmp_path / "other-states.json", protector=Protector())
    second_store.get_or_create(**identity())
    with pytest.raises(ValueError, match="transaction_state_update_field_invalid"):
        store.transition(
            **identity(), expected_revision=0, event_id="event-field",
            transition_code="bad_field", flow_state="COLLECTING",
            updates={"tenant_id": "other-tenant"},
        )


def test_state_store_isolates_tenants_and_bounds_processed_events(tmp_path: Path) -> None:
    store = TransactionStateStore(tmp_path / "states.json", protector=Protector(), max_event_ids=3)
    first = store.get_or_create(**identity())
    for index in range(4):
        first = store.transition(
            **identity(), expected_revision=first.revision, event_id=f"event-{index}",
            transition_code="collect", flow_state="COLLECTING", updates={},
        )

    other = store.get_or_create(
        tenant_id="tenant-2", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
    )
    assert first.processed_event_ids == ["event-1", "event-2", "event-3"]
    assert other.revision == 0
    assert other.tenant_id == "tenant-2"
