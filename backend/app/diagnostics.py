from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from .observability import REQUEST_ID


class DiagnosticsStore:
    """Bounded in-memory diagnostics. Never receives authorization headers or image data."""

    def __init__(self, max_entries: int = 50) -> None:
        if max_entries < 1 or max_entries > 500:
            raise ValueError("max_entries must be between 1 and 500")
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = RLock()

    def add(self, event: str, *, request_id: str | None = None, **details: Any) -> dict[str, Any]:
        entry = {
            "id": uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id or REQUEST_ID.get(),
            "event": event,
            "details": deepcopy(details),
        }
        with self._lock:
            self._entries.append(entry)
        return deepcopy(entry)

    def recent(self, *, limit: int = 20, request_id: str | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 100))
        with self._lock:
            entries = list(self._entries)
        if request_id:
            entries = [entry for entry in entries if entry["request_id"] == request_id]
        return deepcopy(entries[-bounded_limit:])

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
