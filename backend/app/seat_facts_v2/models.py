from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SeatRequestType = Literal["EXACT_SEATS", "WPLUS_AREA", "MANUAL_MARK_REQUIRED"]
SeatFactStatus = Literal["AVAILABLE", "OCCUPIED", "UNKNOWN"]


class ExactSeatFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    seat_label: str = Field(min_length=1, max_length=80)
    wanda_seat_id: str | None = Field(default=None, max_length=100)
    seat_id: str | None = Field(default=None, max_length=100)
    row: int | None = Field(default=None, ge=1, le=999)
    col: int | None = Field(default=None, ge=1, le=999)
    area_code: str | None = Field(default=None, max_length=100)
    zone_type: str | None = Field(default=None, max_length=100)
    seat_type: str | None = Field(default=None, max_length=100)
    member_price_group: str | None = Field(default=None, max_length=160)
    status: SeatFactStatus
    is_wplus_exclusive: bool = False
    area_original_price_fen: int | None = Field(default=None, ge=0)
    area_member_price_fen: int | None = Field(default=None, ge=0)
    has_valid_area_member_price: bool = False
    area_member_activity_code_hint: str | None = Field(default=None, max_length=160)

    @property
    def is_wplus(self) -> bool:
        """Compatibility view for the precise is_wplus_exclusive field."""
        return self.is_wplus_exclusive


class WplusAreaFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_code: str | None = Field(default=None, max_length=100)
    zone_type: str | None = Field(default=None, max_length=100)
    available_seat_ids: list[str] = Field(default_factory=list, max_length=10000)
    available_seat_count: int = Field(default=0, ge=0, le=10000)
    wplus_available: bool = False
    area_original_price_fen: int | None = Field(default=None, ge=0)
    area_member_price_fen: int | None = Field(default=None, ge=0)
    has_valid_area_member_price: bool = False
    area_member_activity_code_hint: str | None = Field(default=None, max_length=160)


class SeatFactsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "EXACT_SEATS_RESOLVED", "WPLUS_AREA_RESOLVED", "MANUAL_MARK_REQUIRED",
        "SEAT_NOT_FOUND", "SEAT_UNAVAILABLE", "PROVIDER_UNAVAILABLE", "INPUT_INCOMPLETE",
    ]
    seat_request_type: SeatRequestType
    wanda_store_id: str | None = Field(default=None, max_length=100)
    wanda_show_id: str | None = Field(default=None, max_length=100)
    has_manual_mark: bool | None = None
    has_selected_seats: bool = False
    quote_scope: Literal["EXACT_SEATS", "WPLUS_AREA", "MISSING_CONTEXT"] = "MISSING_CONTEXT"
    wplus_price_authoritative: Literal[False] = False
    authoritative_wplus_price_fen: None = None
    exact_seats: list[ExactSeatFact] = Field(default_factory=list, max_length=30)
    wplus_areas: list[WplusAreaFact] = Field(default_factory=list, max_length=100)
    same_type_reference: ExactSeatFact | None = None
    resolution_reason: str | None = Field(default=None, max_length=160)
