from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CostStatus = Literal["COST_READY", "PROBE_REQUIRED", "COST_UNAVAILABLE", "INPUT_INCOMPLETE"]
CostRequestType = Literal["WPLUS_AREA", "EXACT_SEATS"]
CostSource = Literal["SHOWTIME_WPLUS", "REALTIME_AREA_WPLUS", "LOCKED_ALLOT_SEAT"]


class WandaCostItem(BaseModel):
    """Cost for one exact seat, or the unbound W+ area cost."""

    model_config = ConfigDict(extra="forbid")

    seat_label: str | None = Field(default=None, max_length=80)
    area_code: str | None = Field(default=None, max_length=100)
    zone_type: str | None = Field(default=None, max_length=100)
    cost_fen: int | None = Field(default=None, gt=0)
    cost_source: CostSource | None = None


class WandaProbeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_code: str = Field(min_length=1, max_length=100)
    zone_type: str = Field(min_length=1, max_length=100)
    seat_id: str = Field(min_length=1, max_length=100)


class WandaCostFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CostStatus
    request_type: CostRequestType | None = None
    cost_items: list[WandaCostItem] = Field(default_factory=list, max_length=30)
    probe_targets: list[WandaProbeTarget] = Field(default_factory=list, max_length=30)
    probe_required: bool = False
    probe_executed: Literal[False] = False
    pricing_called: Literal[False] = False
    normal_seat_member_price_supported: Literal[True] = True
    original_price_used_when_member_missing: Literal[False] = False
    reason: str | None = Field(default=None, max_length=160)
