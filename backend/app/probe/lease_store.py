from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class AccountLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str = Field(min_length=1, max_length=160)
    probe_id: str = Field(min_length=1, max_length=160)
    show_id: str = Field(min_length=1, max_length=240)
    expires_at: str


class DurableAccountLeaseStore:
    """SQLite-backed account lease shared by workers and recoverable on restart."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS probe_account_leases (
                    account_ref TEXT PRIMARY KEY,
                    probe_id TEXT NOT NULL,
                    show_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )

    def try_acquire(self, account_ref: str, probe_id: str, show_id: str, *, now: datetime, ttl_seconds: float) -> bool:
        expires = _iso(now.timestamp() + float(ttl_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM probe_account_leases WHERE expires_at <= ?", (_iso(now.timestamp()),))
            existing = connection.execute(
                "SELECT probe_id FROM probe_account_leases WHERE account_ref = ?", (account_ref,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return bool(existing[0] == probe_id)
            connection.execute(
                "INSERT INTO probe_account_leases(account_ref, probe_id, show_id, expires_at) VALUES (?, ?, ?, ?)",
                (account_ref, probe_id, show_id, expires),
            )
            connection.commit()
            return True

    def renew(self, account_ref: str, probe_id: str, *, now: datetime, ttl_seconds: float) -> bool:
        expires = _iso(now.timestamp() + float(ttl_seconds))
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE probe_account_leases SET expires_at = ? WHERE account_ref = ? AND probe_id = ? AND expires_at > ?",
                (expires, account_ref, probe_id, _iso(now.timestamp())),
            )
            connection.commit()
            return result.rowcount == 1

    def release(self, account_ref: str, probe_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM probe_account_leases WHERE account_ref = ? AND probe_id = ?",
                (account_ref, probe_id),
            )
            connection.commit()
            return result.rowcount == 1

    def recover_expired(self, *, now: datetime) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_ref FROM probe_account_leases WHERE expires_at <= ?",
                (_iso(now.timestamp()),),
            ).fetchall()
            connection.execute("DELETE FROM probe_account_leases WHERE expires_at <= ?", (_iso(now.timestamp()),))
            connection.commit()
        return [str(row[0]) for row in rows]

    def get(self, account_ref: str) -> AccountLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_ref, probe_id, show_id, expires_at FROM probe_account_leases WHERE account_ref = ?",
                (account_ref,),
            ).fetchone()
        return AccountLease.model_validate(dict(zip(("account_ref", "probe_id", "show_id", "expires_at"), row))) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
