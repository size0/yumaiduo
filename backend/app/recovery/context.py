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
