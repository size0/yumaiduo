from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.quote_record_store import QuoteRecordStore
from app.rules_first_migration import migrate_legacy_quote_records
from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return "enc:" + value

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def test_legacy_quote_migration_is_read_only_by_default_and_imports_only_authoritative_records(tmp_path: Path) -> None:
    protector = PlainProtector()
    source = tmp_path / "quote-records.json"
    legacy = QuoteRecordStore(source, protector=protector)
    legacy.save({
        "record_id": "record-1", "quote_id": "quote-1", "tenant_id": "tenant",
        "shop_id": "shop", "buyer_id": "buyer", "chat_id": "chat",
        "created_at": datetime.now(timezone.utc).isoformat(), "provider_amount_fen": 8000,
        "buyer_amount_fen": 8800, "selected_seats": [{"rowNo": 5, "colNo": 8}],
        "provider_show_id": "show-1",
    })
    destination = tmp_path / "rules.sqlite3"
    dry = migrate_legacy_quote_records(source, destination, protector=protector)
    assert dry.dry_run is True
    assert dry.imported == 0
    assert not destination.exists()

    applied = migrate_legacy_quote_records(source, destination, apply=True, protector=protector)
    assert applied.imported == 1
    assert RulesFirstStore(destination, protector=protector).get_selected_seat_quote("quote-1") is not None
