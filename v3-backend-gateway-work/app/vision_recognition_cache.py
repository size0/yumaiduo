"""Bounded, content-addressed cache for image-only vision facts."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from collections.abc import Callable
from typing import Final

from .schemas import Recognition

CACHE_VERSION: Final = 1
DEFAULT_TTL_SECONDS: Final = 24 * 60 * 60
DEFAULT_MAX_ENTRIES: Final = 500
_CACHE_KEY = re.compile(r"^[a-f0-9]{64}$")


class VisionRecognitionCache:
    """Persist successful recognition only; never store URLs or buyer context."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(1, min(int(max_entries), 5_000))
        self.now = now
        self._lock = threading.RLock()
        self._state: dict[str, object] | None = None

    def get(self, key: str) -> Recognition | None:
        if not _CACHE_KEY.fullmatch(str(key)):
            return None
        with self._lock:
            state = self._read()
            entry = state["entries"].get(key)
            if not isinstance(entry, dict):
                return None
            if float(entry.get("expires_at") or 0) <= self.now():
                state["entries"].pop(key, None)
                return None
            try:
                return Recognition.model_validate(entry["recognition"])
            except (KeyError, TypeError, ValueError):
                state["entries"].pop(key, None)
                return None

    def put(self, key: str, recognition: Recognition) -> None:
        if not _CACHE_KEY.fullmatch(str(key)):
            raise ValueError("invalid vision recognition cache key")
        with self._lock:
            state = self._read()
            now = self.now()
            entries = state["entries"]
            entries[key] = {
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "recognition": recognition.model_dump(mode="json"),
            }
            live = sorted(
                (
                    (entry_key, entry)
                    for entry_key, entry in entries.items()
                    if isinstance(entry, dict) and float(entry.get("expires_at") or 0) > now
                ),
                key=lambda item: float(item[1].get("created_at") or 0),
                reverse=True,
            )[: self.max_entries]
            state["entries"] = dict(live)
            self._write(state)

    def _read(self) -> dict[str, object]:
        if self._state is not None:
            return self._state
        if self.path is None:
            self._state = {"version": CACHE_VERSION, "entries": {}}
            return self._state
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if raw.get("version") != CACHE_VERSION or not isinstance(raw.get("entries"), dict):
                raise ValueError("unsupported vision cache")
            self._state = raw
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            self._state = {"version": CACHE_VERSION, "entries": {}}
        return self._state

    def _write(self, state: dict[str, object]) -> None:
        self._state = state
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), "utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)
