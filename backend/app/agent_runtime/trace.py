"""Trace recorder with conservative redaction for tenant-safe diagnostics."""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any, Mapping

from .events import AgentEvent

_SENSITIVE = re.compile(
    r"(?i)(token|cookie|csrf|secret|authorization|password|api[_ -]?key|"
    r"ticket[_ -]?(?:code|voucher|number)|ticket(?:code|voucher|number))"
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): "[REDACTED]" if _SENSITIVE.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    text = str(value)
    return text[:500] if len(text) > 500 else text


class TraceRecorder:
    def __init__(self, *, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or secrets.token_urlsafe(12)
        self.events: list[dict[str, Any]] = []

    def record(self, event: AgentEvent | Mapping[str, Any]) -> None:
        if isinstance(event, AgentEvent):
            item = {"name": event.name, "run_id": event.run_id, "session_id": event.session_id, "trace_id": event.trace_id, "tenant_id": event.tenant_id, "shop_id": event.shop_id, "buyer_id": event.buyer_id, "chat_id": event.chat_id, "event_id": event.event_id, "turn_index": event.turn_index, "timestamp": event.timestamp, "payload": redact(event.payload)}
        else:
            item = redact(dict(event))
        self.events.append(item)

    @staticmethod
    def parameter_hash(arguments: Mapping[str, Any]) -> str:
        canonical = repr(sorted((str(k), redact(v)) for k, v in arguments.items())).encode()
        return hashlib.sha256(canonical).hexdigest()[:16]

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.events]
