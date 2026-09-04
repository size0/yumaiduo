from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import Settings
from app.rules_first_store import RulesFirstStore, check_sqlite_integrity


def test_selected_seat_real_write_gates_default_closed(monkeypatch) -> None:
    for name in (
        "LIANGPIAO_SELECTED_SEAT_QUOTE_ENABLED",
        "LIANGPIAO_ORDER_CREATE_ENABLED",
        "EXTERNAL_WRITES_ENABLED",
        "LIANGPIAO_CALLBACK_ENABLED",
        "WANDA_EXTERNAL_WRITES_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.liangpiao_selected_seat_quote_enabled is False
    assert settings.liangpiao_order_create_enabled is False
    assert settings.external_writes_enabled is False
    assert settings.liangpiao_callback_enabled is False


def test_selected_seat_real_write_gates_read_explicit_environment(monkeypatch) -> None:
    monkeypatch.setenv("LIANGPIAO_SELECTED_SEAT_QUOTE_ENABLED", "true")
    monkeypatch.setenv("LIANGPIAO_ORDER_CREATE_ENABLED", "1")
    monkeypatch.setenv("EXTERNAL_WRITES_ENABLED", "on")
    monkeypatch.setenv("LIANGPIAO_CALLBACK_ENABLED", "yes")

    settings = Settings.from_env()

    assert settings.liangpiao_selected_seat_quote_enabled is True
    assert settings.liangpiao_order_create_enabled is True
    assert settings.external_writes_enabled is True
    assert settings.liangpiao_callback_enabled is True


def test_rules_store_applies_versioned_snapshot_migration_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    runtime = RulesFirstStore(path)
    assert runtime.schema_version() == 3

    with sqlite3.connect(path) as connection:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
        connection.execute("DELETE FROM schema_migrations WHERE version=3")

    expected_columns = {
        "provider", "provider_show_id", "selected_seats_json",
        "preflight_request_json", "preflight_response_json",
        "provider_amount_fen", "buyer_amount_fen", "quote_hash",
        "quote_generation", "quote_expires_at", "confirmation_id",
        "out_order_no", "provider_order_no", "provider_payload_hash",
        "provider_status",
    }
    assert versions == [1, 2, 3]
    assert expected_columns <= columns

    RulesFirstStore(path)

    backup = path.with_name(f"{path.name}.pre-migration-v3.bak")
    assert backup.exists()
    assert check_sqlite_integrity(path) is True
    assert check_sqlite_integrity(backup) is True
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
