from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RecognitionResult(BaseModel):
    """Provider facts only; no IDs or fields that authorize downstream business work."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["LIANGPIAO"] = "LIANGPIAO"
    provider_recognize_id: str | None = Field(default=None, max_length=100)
    platform_text: str | None = Field(default=None, max_length=100)
    city_text: str | None = Field(default=None, max_length=100)
    cinema_text: str | None = Field(default=None, max_length=240)
    cinema_truncated: bool = False
    movie: str | None = Field(default=None, max_length=160)
    show_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    hall: str | None = Field(default=None, max_length=120)
    language: str | None = Field(default=None, max_length=40)
    dimension: str | None = Field(default=None, max_length=40)
    selected_seats: list[str] = Field(default_factory=list, max_length=30)
    has_selected_seats: bool = False
    image_total_price_fen: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    has_manual_mark: bool | None = None
    raw_provider_result: dict[str, Any] = Field(default_factory=dict)
