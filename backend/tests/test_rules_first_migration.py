from __future__ import annotations

from pathlib import Path

from app.rules_first_migration import migrate_legacy_transaction_states
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.transaction_state_store import TransactionStateStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def test_legacy_migration_is_dry_run_by_default_and_never_replays(tmp_path: Path) -> None:
    source = tmp_path / "states.json"
    destination = tmp_path / "rules.sqlite3"
    protection = PlainProtector()
    legacy = TransactionStateStore(source, protector=protection)
    state = legacy.transition(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        expected_revision=0, event_id="event-1", transition_code="price_pending",
        flow_state="PRICE_CHANGING", updates={"price_change_status": "unknown"},
        allow_compatible_bootstrap=True,
    )

    dry = migrate_legacy_transaction_states(source, destination, protector=protection)
    assert dry.dry_run is True
    assert dry.ambiguous == 1
    assert not destination.exists()

    applied = migrate_legacy_transaction_states(
        source, destination, apply=True, protector=protection,
    )
    assert applied.imported == 1
    assert applied.manual_tasks_created == 1
    migrated = SqliteTransactionStateStore(destination, protector=protection).get(
        tenant_id=state.tenant_id, shop_id=state.shop_id,
        buyer_id=state.buyer_id, chat_id=state.chat_id,
    )
    assert migrated is not None and migrated.flow_state == "MANUAL_HOLD"
    tasks = RulesFirstStore(destination, protector=protection).list_manual_tasks("tenant-1")
    assert tasks[0]["reason"] == "legacy_migration_ambiguous"
