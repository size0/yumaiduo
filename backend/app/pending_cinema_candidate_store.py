from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .models import MovieImageInfo


class PendingCinemaCandidateStore:
    """Small durable, expiring store for explicit cinema-choice context.

    The key contains tenant, shop, buyer and chat identity. Only the parsed
    recognition result is stored; no image bytes or credentials are persisted.
    """

    def __init__(self, path: str | Path, *, ttl_seconds: int = 15 * 60) -> None:
        self._path = Path(path)
        self._ttl = max(60, int(ttl_seconds))
        self._lock = RLock()

    def get(self, key: str) -> MovieImageInfo | None:
        with self._lock:
            data = self._read()
            entry = data.get("entries", {}).get(key)
            if not isinstance(entry, dict) or self._expired(entry):
                if entry is not None:
                    data.get("entries", {}).pop(key, None)
                    self._write(data)
                return None
            try:
                return MovieImageInfo.model_validate(entry["recognition"])
            except Exception:
                data.get("entries", {}).pop(key, None)
                self._write(data)
                return None

    def save(self, key: str, recognition: MovieImageInfo) -> None:
        with self._lock:
            data = self._read()
            entries = data.setdefault("entries", {})
            self._purge(entries)
            entries[key] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=self._ttl)).isoformat(),
                "recognition": {
                    key: value for key, value in recognition.model_dump(mode="json").items()
                    if key in MovieImageInfo.model_fields
                },
            }
            self._write(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read()
            if key in data.get("entries", {}):
                data["entries"].pop(key, None)
                self._write(data)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": 1, "entries": {}}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": 1, "entries": {}}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._path)

    @staticmethod
    def _expired(entry: dict[str, Any]) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(entry["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= expires_at
        except (KeyError, TypeError, ValueError):
            return True

    def _purge(self, entries: dict[str, Any]) -> None:
        for key, entry in list(entries.items()):
            if not isinstance(entry, dict) or self._expired(entry):
                entries.pop(key, None)
