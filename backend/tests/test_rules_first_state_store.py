from __future__ import annotations

import sqlite3
from pathlib import Path

from app.rules_first_state_store import SqliteTransactionStateStore
from app.transaction_state_store import StateRevisionConflict


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def identity() -> dict[str, str]:
    return {"tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1"}


def test_sqlite_transaction_state_and_transition_evidence_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    store = SqliteTransactionStateStore(path, protector=PlainProtector())
    current = store.get_or_create(**identity())

    updated = store.transition(
        **identity(), expected_revision=current.revision, event_id="event-1:decision",
        transition_code="facts_collected", flow_state="FACTS_READY",
        updates={"expected_inputs": []}, allow_compatible_bootstrap=True,
    )

    assert updated.revision == 1
    assert updated.flow_state == "FACTS_READY"
    assert store.get(**identity()) == updated
    with sqlite3.connect(path) as connection:
        transition = connection.execute(
            "SELECT revision_before,revision_after,state_before,state_after,transition_code FROM state_transitions"
        ).fetchone()
    assert transition == (0, 1, "NEW", "FACTS_READY", "facts_collected")


def test_replaced_trade_terms_start_a_new_active_generation_without_regressing_old_state(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    store = SqliteTransactionStateStore(path, protector=PlainProtector())
    quoted = store.transition(
        **identity(), expected_revision=0, event_id="quote-1", transition_code="quoted",
        flow_state="QUOTED", updates={"quote_status": "ready", "active_quote_record_id": "quote-1"},
        allow_compatible_bootstrap=True,
    )

    replacement = store.start_new_generation(
        **identity(), expected_revision=quoted.revision,
        event_id="quote-2:generation", reason="trade_terms_replaced",
    )

    assert replacement.generation == 2
    assert replacement.flow_state == "COLLECTING"
    assert replacement.state_id != quoted.state_id
    assert store.get(**identity()) == replacement
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT generation,is_active,close_reason FROM transactions ORDER BY generation",
        ).fetchall()
    assert rows == [(1, 0, "trade_terms_replaced"), (2, 1, None)]


def test_generation_change_fails_closed_when_an_order_is_already_bound(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    bound = store.transition(
        **identity(), expected_revision=0, event_id="order-1", transition_code="bound",
        flow_state="ORDER_BOUND", updates={"order_id": "order-1", "order_status": "bound"},
        allow_compatible_bootstrap=True,
    )
    try:
        store.start_new_generation(
            **identity(), expected_revision=bound.revision,
            event_id="quote-2:generation", reason="trade_terms_replaced",
        )
    except ValueError as error:
        assert str(error) == "transaction_generation_has_active_order"
    else:
        raise AssertionError("bound order must prevent automatic generation replacement")


def test_sqlite_state_keeps_event_idempotency_and_revision_cas(tmp_path: Path) -> None:
    store = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    first = store.transition(
        **identity(), expected_revision=0, event_id="event-1", transition_code="collecting",
        flow_state="COLLECTING", updates={},
    )
    duplicate = store.transition(
        **identity(), expected_revision=0, event_id="event-1", transition_code="collecting",
        flow_state="COLLECTING", updates={},
    )
    assert duplicate == first

    try:
        store.transition(
            **identity(), expected_revision=0, event_id="event-2", transition_code="stale",
            flow_state="COLLECTING", updates={},
        )
    except StateRevisionConflict:
        pass
    else:
        raise AssertionError("stale revision must be rejected")
