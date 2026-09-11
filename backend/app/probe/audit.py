from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


_SENSITIVE = {"token", "access_token", "cookie", "csrf", "csrf_token", "phone", "authorization", "raw_response"}


class ProbeAuditStore:
    """Durable lifecycle audit with a strict public-field allow-list."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS probe_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, probe_id TEXT, event TEXT, payload TEXT, created_at TEXT)"
            )

    def record(self, probe_id: str, event: str, payload: Mapping[str, object] | None = None) -> None:
        clean = _redact(payload or {})
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO probe_audit(probe_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                (probe_id, event, json.dumps(clean, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def list_for(self, probe_id: str) -> list[dict[str, object]]:
        with sqlite3.connect(self._path) as connection:
            rows = connection.execute(
                "SELECT event, payload, created_at FROM probe_audit WHERE probe_id = ? ORDER BY id", (probe_id,)
            ).fetchall()
        return [{"event": row[0], "payload": json.loads(row[1]), "created_at": row[2]} for row in rows]


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    return value
