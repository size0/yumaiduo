from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ProbeOrder, ProbeStatus


class DurableProbeStore:
    """Durable ProbeOrder and show gate store using SQLite CAS updates."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS probe_orders (probe_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS probe_show_locks (
                    show_id TEXT PRIMARY KEY,
                    probe_id TEXT NOT NULL,
                    lock_state TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL
                )"""
            )

    def create(self, order: ProbeOrder) -> ProbeOrder:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO probe_orders(probe_id, payload) VALUES (?, ?)",
                (order.probe_id, json.dumps(order.model_dump(mode="json"), ensure_ascii=False)),
            )
            connection.commit()
        return order

    def get(self, probe_id: str) -> ProbeOrder | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM probe_orders WHERE probe_id = ?", (probe_id,)).fetchone()
        return ProbeOrder.model_validate(json.loads(row[0])) if row else None

    def list_all(self) -> list[ProbeOrder]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM probe_orders ORDER BY probe_id").fetchall()
        return [ProbeOrder.model_validate(json.loads(row[0])) for row in rows]

    def recoverable(self) -> list[ProbeOrder]:
        terminal = {ProbeStatus.RELEASE_VERIFIED.value, ProbeStatus.FAILED.value}
        return [item for item in self.list_all() if item.status.value not in terminal]

    def update(self, probe_id: str, *, expected_revision: int, **changes: Any) -> ProbeOrder:
        current = self.get(probe_id)
        if current is None:
            raise ValueError("probe_not_found")
        if current.revision != expected_revision:
            raise ValueError("probe_revision_conflict")
        invalid = set(changes) - set(ProbeOrder.model_fields)
        if invalid:
            raise ValueError("probe_update_field_invalid")
        payload = current.model_dump(mode="json")
        payload.update(changes)
        payload["revision"] = current.revision + 1
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        updated = ProbeOrder.model_validate(payload)
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE probe_orders SET payload = ? WHERE probe_id = ? AND json_extract(payload, '$.revision') = ?",
                (json.dumps(updated.model_dump(mode="json"), ensure_ascii=False), probe_id, current.revision),
            )
            connection.commit()
        if result.rowcount != 1:
            raise ValueError("probe_revision_conflict")
        return updated

    def transition(self, probe_id: str, status: ProbeStatus, **changes: Any) -> ProbeOrder:
        current = self.get(probe_id)
        if current is None:
            raise ValueError("probe_not_found")
        allowed = _ALLOWED_TRANSITIONS[current.status]
        if status not in allowed:
            raise ValueError("probe_transition_invalid")
        return self.update(probe_id, expected_revision=current.revision, status=status, **changes)

    def try_acquire_show(self, show_id: str, probe_id: str, *, now: datetime, lease_expires_at: str) -> bool:
        now_text = now.astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT probe_id, lock_state, lease_expires_at FROM probe_show_locks WHERE show_id = ?", (show_id,)
            ).fetchone()
            if row is not None:
                same_probe = row[0] == probe_id
                pending = row[1] == "RELEASE_UNVERIFIED"
                active = row[2] > now_text
                if pending or (active and not same_probe):
                    connection.commit()
                    return False
                if same_probe and row[1] == "ACTIVE":
                    connection.commit()
                    return True
                connection.execute("DELETE FROM probe_show_locks WHERE show_id = ?", (show_id,))
            connection.execute(
                "INSERT INTO probe_show_locks(show_id, probe_id, lock_state, lease_expires_at) VALUES (?, ?, 'ACTIVE', ?)",
                (show_id, probe_id, lease_expires_at),
            )
            connection.commit()
            return True

    def mark_show_release_unverified(self, show_id: str, probe_id: str, *, lease_expires_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO probe_show_locks(show_id, probe_id, lock_state, lease_expires_at) VALUES (?, ?, 'RELEASE_UNVERIFIED', ?) "
                "ON CONFLICT(show_id) DO UPDATE SET probe_id=excluded.probe_id, lock_state='RELEASE_UNVERIFIED', lease_expires_at=excluded.lease_expires_at",
                (show_id, probe_id, lease_expires_at),
            )
            connection.commit()

    def release_show(self, show_id: str, probe_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM probe_show_locks WHERE show_id = ? AND probe_id = ? AND lock_state = 'ACTIVE'",
                (show_id, probe_id),
            )
            connection.commit()
            return result.rowcount == 1

    def show_lock(self, show_id: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT show_id, probe_id, lock_state, lease_expires_at FROM probe_show_locks WHERE show_id = ?",
                (show_id,),
            ).fetchone()
        return dict(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection


_ALLOWED_TRANSITIONS = {
    ProbeStatus.CREATED: frozenset({ProbeStatus.CREATED, ProbeStatus.LOCKED, ProbeStatus.CANCEL_REQUESTED, ProbeStatus.FAILED}),
    ProbeStatus.LOCKED: frozenset({ProbeStatus.LOCKED, ProbeStatus.PRICE_READ, ProbeStatus.CANCEL_REQUESTED, ProbeStatus.FAILED}),
    ProbeStatus.PRICE_READ: frozenset({ProbeStatus.PRICE_READ, ProbeStatus.CANCEL_REQUESTED}),
    ProbeStatus.CANCEL_REQUESTED: frozenset({ProbeStatus.CANCEL_REQUESTED, ProbeStatus.CANCEL_CONFIRMED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_UNVERIFIED}),
    ProbeStatus.CANCEL_CONFIRMED: frozenset({ProbeStatus.CANCEL_CONFIRMED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED}),
    ProbeStatus.RELEASE_CHECKING: frozenset({ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED, ProbeStatus.CANCEL_CONFIRMED}),
    ProbeStatus.RELEASE_VERIFIED: frozenset({ProbeStatus.RELEASE_VERIFIED}),
    ProbeStatus.RELEASE_UNVERIFIED: frozenset({ProbeStatus.RELEASE_UNVERIFIED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_VERIFIED}),
    ProbeStatus.FAILED: frozenset({ProbeStatus.FAILED, ProbeStatus.CANCEL_REQUESTED, ProbeStatus.RELEASE_CHECKING, ProbeStatus.RELEASE_UNVERIFIED}),
}
