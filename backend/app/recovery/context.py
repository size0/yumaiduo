from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QuotePipelineContext(BaseModel):
    """Single pipeline snapshot; stages add authoritative facts progressively."""

    identity: dict[str, str] = Field(default_factory=dict)
    conversation_facts: dict[str, Any] = Field(default_factory=dict)
    recognition: Any | None = None
    cinema: Any | None = None
    movie: Any | None = None
    show: Any | None = None
    seat_facts: Any | None = None
    cost: Any | None = None
    pricing: Any | None = None
    quote_record: Any | None = None
    current_gate: str | None = None
    generation: int = 0
    invalidated_fields: set[str] = Field(default_factory=set)
    stale_fields: set[str] = Field(default_factory=set)
    quote_generation: int | None = None

    def merge_facts(self, current: dict[str, Any], *, stored: dict[str, Any] | None = None,
                    candidates: dict[str, Any] | None = None) -> None:
        """Merge facts with explicit input precedence and invalidate dependents."""
        from .invalidation import invalidated_fields

        before = dict(self.conversation_facts)
        merged = dict(stored or {})
        merged.update(self.conversation_facts)
        merged.update(candidates or {})
        merged.update(current)
        changed = {key for key, value in merged.items() if before.get(key) != value}
        aliases = {"city": "city", "cinema": "cinema", "quote_date": "date", "showtime_start": "show",
                   "show_id": "show", "selected_seats": "seat", "ticket_count": "ticket_count"}
        changed.update(aliases[key] for key in tuple(changed) if key in aliases)
        # A new cinema/movie/date/show selection invalidates downstream facts,
        # including the old seat selection persisted in Conversation Facts.
        if changed.intersection({"city", "cinema", "movie", "date", "show"}):
            for stale in ("show_id", "showtime_start", "selected_seats", "hall", "candidate_shows"):
                if stale not in current and stale in merged:
                    merged.pop(stale, None)
                    changed.add(stale)
                    self.stale_fields.add(stale)
        self.conversation_facts = merged
        self.invalidated_fields.update(changed)
        for field in changed:
            for dependent in invalidated_fields(field):
                if dependent in {"show", "show_id"}:
                    self.show = None
                elif dependent == "seat_facts":
                    self.seat_facts = None
                elif dependent == "cost":
                    self.cost = None
                elif dependent == "pricing":
                    self.pricing = None
                elif dependent == "quote_record":
                    self.quote_record = None
        self.generation += 1
