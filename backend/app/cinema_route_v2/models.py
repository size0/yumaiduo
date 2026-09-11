from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WandaCinemaCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wanda_store_id: str = Field(min_length=1, max_length=100)
    cinema_name: str = Field(min_length=1, max_length=240)
    address: str | None = Field(default=None, max_length=300)
    liangpiao_cinema_id: str | None = Field(default=None, max_length=100)
    canonical_cinema_identity_id: str | None = Field(default=None, max_length=160)
    verification_status: Literal["CANDIDATE", "VERIFIED"] = "CANDIDATE"
    verification_level: str = Field(default="FINGERPRINT_CANDIDATE", max_length=80)


class CinemaRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["WANDA_SELF", "LIANGPIAO", "UNRESOLVED"]
    wanda_city_id: str | None = Field(default=None, max_length=100)
    liangpiao_cinema_id: str | None = Field(default=None, max_length=100)
    canonical_cinema_identity_id: str | None = Field(default=None, max_length=160)
    wanda_store_id: str | None = Field(default=None, max_length=100)
    wanda_city_name: str | None = Field(default=None, max_length=100)
    wanda_cinema_name: str | None = Field(default=None, max_length=240)
    wanda_cinema_address: str | None = Field(default=None, max_length=300)
    resolution_reason: str = Field(min_length=1, max_length=120)
    candidate_count: int = Field(default=0, ge=0, le=1000)
    candidates: list[WandaCinemaCandidate] = Field(default_factory=list, max_length=100)
    verification_status: Literal["CANDIDATE", "VERIFIED"] = "CANDIDATE"
    verification_level: str = Field(default="NONE", max_length=80)
    show_fingerprint: dict[str, Any] = Field(default_factory=dict)
    provider_debug: dict[str, Any] = Field(default_factory=dict)
