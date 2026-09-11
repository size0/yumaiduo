from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WandaShowCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wanda_show_id: str = Field(min_length=1, max_length=100)
    wanda_film_id: str | None = Field(default=None, max_length=100)
    movie_name: str | None = Field(default=None, max_length=160)
    start_time: str | None = Field(default=None, max_length=20)
    hall_name: str | None = Field(default=None, max_length=160)
    language: str | None = Field(default=None, max_length=40)
    dimension: str | None = Field(default=None, max_length=40)
    sales_price_fen: int | None = Field(default=None, ge=0)
    min_area_price_fen: int | None = Field(default=None, ge=0)
    wplus_activity_price_fen: int | None = Field(default=None, ge=0)
    wplus_activity_code_hint: str | None = Field(default=None, max_length=160)


class ShowResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "RESOLVED", "CANDIDATE_REQUIRED", "NOT_FOUND",
        "PROVIDER_UNAVAILABLE", "INPUT_INCOMPLETE",
    ]
    wanda_store_id: str | None = Field(default=None, max_length=100)
    wanda_show_id: str | None = Field(default=None, max_length=100)
    wanda_film_id: str | None = Field(default=None, max_length=100)
    movie_name: str | None = Field(default=None, max_length=160)
    show_date: str | None = Field(default=None, max_length=20)
    start_time: str | None = Field(default=None, max_length=20)
    hall_name: str | None = Field(default=None, max_length=160)
    language: str | None = Field(default=None, max_length=40)
    dimension: str | None = Field(default=None, max_length=40)
    sales_price_fen: int | None = Field(default=None, ge=0)
    min_area_price_fen: int | None = Field(default=None, ge=0)
    wplus_activity_price_fen: int | None = Field(default=None, ge=0)
    wplus_activity_code_hint: str | None = Field(default=None, max_length=160)
    candidate_count: int = Field(default=0, ge=0, le=1000)
    candidates: list[WandaShowCandidate] = Field(default_factory=list, max_length=100)
    resolution_reason: str | None = Field(default=None, max_length=120)
