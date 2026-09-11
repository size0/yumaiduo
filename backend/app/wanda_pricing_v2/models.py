from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PricingStatus = Literal["PRICED", "PRICING_REQUIRES_COST", "INPUT_INCOMPLETE"]
PricingRequestType = Literal["WPLUS_AREA", "EXACT_SEATS"]
PricingCostSource = Literal["SHOWTIME_WPLUS", "REALTIME_AREA_WPLUS", "LOCKED_ALLOT_SEAT"]


class WandaPricingCostItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_label: str | None = Field(default=None, max_length=80)
    area_code: str | None = Field(default=None, max_length=100)
    zone_type: str | None = Field(default=None, max_length=100)
    cost_fen: int = Field(gt=0)
    cost_source: PricingCostSource


class WandaSeatQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_id: str = Field(min_length=1, max_length=100)
    seat_label: str = Field(min_length=1, max_length=80)
    cost_fen: int = Field(gt=0)
    sell_price_fen: int = Field(gt=0)
    cost_source: PricingCostSource


class WandaPricingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PricingStatus
    request_type: PricingRequestType | None = None
    unit_sell_price_fen: int | None = Field(default=None, gt=0)
    total_sell_price_fen: int | None = Field(default=None, gt=0)
    ticket_count: int | None = Field(default=None, ge=1, le=20)
    needs_ticket_count: bool = False
    seat_quotes: list[WandaSeatQuote] = Field(default_factory=list, max_length=30)
    cost_items: list[WandaPricingCostItem] = Field(default_factory=list, max_length=30)
    cost_sources: list[PricingCostSource] = Field(default_factory=list, max_length=30)
    probe_targets: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    pricing_rule_revision: int | None = Field(default=None, ge=0)
    pricing_rule_version: str | None = Field(default=None, min_length=1, max_length=160)
    pricing_engine_applied: bool = False
    pricing_source: str = Field(default="", max_length=300)
    price_source: str | None = Field(default=None, max_length=120)
    calculation_evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=160)
