"""Typed lifecycle events and optional streaming sink."""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

EVENT_NAMES = frozenset({"agent_start", "turn_start", "model_response_start", "model_response_end", "tool_call_start", "tool_call_end", "tool_blocked", "tool_retry", "context_restore", "context_compaction", "reply_validation", "reply_suppressed", "reply_sent", "agent_settled", "agent_failed", "agent_interrupted"})


@dataclass(frozen=True, slots=True)
class AgentEvent:
    name: str
    run_id: str
    session_id: str
    trace_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    event_id: str | None = None
    turn_index: int = 0
    timestamp: float = field(default_factory=time.time)
    payload: Mapping[str, Any] = field(default_factory=dict)


EventSubscriber = Callable[[AgentEvent], Any | Awaitable[Any]]


class AgentEventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, subscriber: EventSubscriber) -> None:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    async def publish(self, event: AgentEvent) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                result = subscriber(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Observability must never break customer-service execution.
                continue


class StreamSink:
    async def emit(self, event: AgentEvent) -> None:  # pragma: no cover - interface hook
        return None
