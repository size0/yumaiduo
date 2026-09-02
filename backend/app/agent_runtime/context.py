"""Conversation context handling; authority facts are kept separate from text."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class BusinessContext:
    messages: list[dict[str, Any]] = field(default_factory=list)
    authoritative: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)
    phase: str = "consultation"


class BusinessContextCompactor:
    """Compact natural-language messages while retaining structured business keys."""

    def __init__(self, *, max_messages: int = 24) -> None:
        self.max_messages = max(2, int(max_messages))

    def compact(self, context: BusinessContext) -> BusinessContext:
        messages = list(context.messages)
        if len(messages) > self.max_messages:
            messages = messages[-self.max_messages :]
        return BusinessContext(messages, dict(context.authoritative), list(context.missing_fields), list(context.recent_errors[-3:]), context.phase)

    def build(self, history: list[dict[str, Any]], runtime_context: Mapping[str, Any]) -> BusinessContext:
        authority_keys = (
            "cinema", "movie", "show", "seats", "ticket_count", "quote", "current_quote",
            "confirmed_facts", "order", "payment", "fulfillment", "inventory_verified",
            "available_seats", "seat_inventory", "selected_seats", "seat_verified",
            "order_status", "status", "price_verified", "unit_quote_cents", "total_quote_cents",
            "_agent_authoritative_quote_verified", "_agent_order_status_verified",
        )
        nested = runtime_context.get("authoritative_facts")
        authoritative = {
            key: nested[key] for key in authority_keys
            if isinstance(nested, Mapping) and key in nested
        }
        authoritative.update({key: runtime_context[key] for key in authority_keys if key in runtime_context})
        missing = runtime_context.get("missing_fields", [])
        errors = runtime_context.get("recent_tool_errors", [])
        phase = str(runtime_context.get("phase") or "consultation")
        return self.compact(BusinessContext([dict(item) for item in history if isinstance(item, Mapping)], authoritative, [str(x) for x in missing], [str(x) for x in errors], phase))
