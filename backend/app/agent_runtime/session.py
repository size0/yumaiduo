"""In-memory agent work sessions with tenant/shop/buyer/chat isolation."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from threading import RLock
from time import monotonic, time
from typing import Any, Mapping, Protocol


SESSION_STATES = frozenset({"IDLE", "RUNNING", "WAITING_TOOL", "RECOVERING", "WAITING_BUYER", "INTERRUPTED", "SETTLED", "FAILED"})


class SessionPersistence(Protocol):
    """Optional persistence boundary for session checkpoints.

    Implementations own encryption, tenant isolation and durable writes.  The
    runtime deliberately treats this interface as best-effort so an audit or
    storage outage cannot break the customer-facing reply path.
    """

    def load(self, session_key: str) -> Mapping[str, Any] | None:
        ...

    def save(self, session_key: str, snapshot: Mapping[str, Any]) -> None:
        ...


@dataclass(slots=True)
class AgentSessionState:
    session_key: str
    run_id: str = ""
    state: str = "IDLE"
    phase: str = "consultation"
    missing_fields: list[str] = field(default_factory=list)
    pending_question: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    updated_at: float = field(default_factory=monotonic)


class AgentSessionStore:
    """Bounded process-local store; callers may persist checkpoints externally."""

    def __init__(
        self,
        *,
        max_sessions: int = 1000,
        ttl_seconds: float = 24 * 3600,
        persistence: SessionPersistence | None = None,
    ) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.persistence = persistence
        self._items: dict[str, AgentSessionState] = {}
        self._interrupted_runs: set[tuple[str, str]] = set()
        self._lock = RLock()

    def get(self, session_key: str) -> AgentSessionState:
        with self._lock:
            item = self._items.get(session_key)
            if item is None or monotonic() - item.updated_at > self.ttl_seconds:
                item = self._restore_locked(session_key) or AgentSessionState(session_key=session_key)
                self._items[session_key] = item
            item.updated_at = monotonic()
            return self._copy(item)

    def begin(self, session_key: str, run_id: str) -> AgentSessionState:
        with self._lock:
            current = self._items.get(session_key) or self._restore_locked(session_key)
            if current and current.state in {"RUNNING", "WAITING_TOOL", "RECOVERING"} and current.run_id != run_id:
                self._interrupted_runs.add((session_key, current.run_id))
                current.state = "INTERRUPTED"
                current.revision += 1
            item = current or AgentSessionState(session_key=session_key)
            item.run_id, item.state, item.updated_at = run_id, "RUNNING", monotonic()
            item.revision += 1
            self._items[session_key] = item
            self._persist_locked(item)
            self._trim()
            return self._copy(item)

    def transition(self, session_key: str, state: str, *, run_id: str | None = None, **updates: Any) -> AgentSessionState:
        state = str(state).upper()
        if state not in SESSION_STATES:
            raise ValueError(f"unsupported session state: {state}")
        with self._lock:
            item = self._items.get(session_key) or AgentSessionState(session_key=session_key)
            if run_id is not None and item.run_id and item.run_id != run_id:
                return self._copy(item)
            item.state, item.updated_at = state, monotonic()
            item.revision += 1
            for key, value in updates.items():
                if hasattr(item, key):
                    setattr(item, key, value)
            self._items[session_key] = item
            self._persist_locked(item)
            return self._copy(item)

    def is_interrupted(self, session_key: str, run_id: str) -> bool:
        with self._lock:
            if (session_key, run_id) in self._interrupted_runs:
                return True
            item = self._items.get(session_key)
            return bool(item and item.run_id == run_id and item.state == "INTERRUPTED")

    def interrupt(self, session_key: str, run_id: str | None = None) -> AgentSessionState:
        with self._lock:
            current = self._items.get(session_key)
            if run_id and (current is None or current.run_id != run_id):
                self._interrupted_runs.add((session_key, run_id))
        return self.transition(session_key, "INTERRUPTED", run_id=run_id)

    def checkpoint_state(self, session_key: str, run_id: str, checkpoint: dict[str, Any]) -> AgentSessionState:
        return self.transition(session_key, "RUNNING", run_id=run_id, checkpoint=dict(checkpoint))

    def _restore_locked(self, session_key: str) -> AgentSessionState | None:
        """Restore a non-expired checkpoint, if an adapter is configured."""
        if self.persistence is None:
            return None
        try:
            record = self.persistence.load(session_key)
        except Exception:
            return None
        if not isinstance(record, Mapping) or record.get("session_key", session_key) != session_key:
            return None
        saved_at = record.get("saved_at")
        try:
            if saved_at is not None and time() - float(saved_at) > self.ttl_seconds:
                return None
        except (TypeError, ValueError):
            return None
        state = str(record.get("state", "IDLE")).upper()
        if state not in SESSION_STATES:
            state = "IDLE"
        missing_fields = record.get("missing_fields", [])
        checkpoint = record.get("checkpoint", {})
        try:
            revision = max(0, int(record.get("revision", 0) or 0))
        except (TypeError, ValueError):
            revision = 0
        return AgentSessionState(
            session_key=session_key,
            run_id=str(record.get("run_id", "")),
            state=state,
            phase=str(record.get("phase", "consultation")),
            missing_fields=list(missing_fields) if isinstance(missing_fields, (list, tuple)) else [],
            pending_question=record.get("pending_question"),
            checkpoint=dict(checkpoint) if isinstance(checkpoint, Mapping) else {},
            revision=revision,
            updated_at=monotonic(),
        )

    def _persist_locked(self, item: AgentSessionState) -> None:
        if self.persistence is None:
            return
        snapshot = {
            "session_key": item.session_key,
            "run_id": item.run_id,
            "state": item.state,
            "phase": item.phase,
            "missing_fields": list(item.missing_fields),
            "pending_question": item.pending_question,
            "checkpoint": dict(item.checkpoint),
            "revision": item.revision,
            "saved_at": time(),
        }
        try:
            self.persistence.save(item.session_key, snapshot)
        except Exception:
            # Persistence is an audit/recovery aid; it must not fail a reply.
            return

    def _trim(self) -> None:
        while len(self._items) > self.max_sessions:
            oldest = min(self._items, key=lambda key: self._items[key].updated_at)
            self._items.pop(oldest, None)
        active_keys = set(self._items)
        self._interrupted_runs = {item for item in self._interrupted_runs if item[0] in active_keys}

    @staticmethod
    def _copy(item: AgentSessionState) -> AgentSessionState:
        values = {field.name: getattr(item, field.name) for field in fields(AgentSessionState)}
        values["missing_fields"] = list(item.missing_fields)
        values["checkpoint"] = dict(item.checkpoint)
        return AgentSessionState(**values)
