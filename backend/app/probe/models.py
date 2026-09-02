from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProbeStatus(StrEnum):
    CREATED = "CREATED"
    LOCKED = "LOCKED"
    PRICE_READ = "PRICE_READ"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    RELEASE_CHECKING = "RELEASE_CHECKING"
    RELEASE_VERIFIED = "RELEASE_VERIFIED"
    RELEASE_UNVERIFIED = "RELEASE_UNVERIFIED"
    FAILED = "FAILED"


class ProbeOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=200)
    shop_id: str = Field(min_length=1, max_length=200)
    show_id: str = Field(min_length=1, max_length=240)
    account_ref: str = Field(default="", max_length=160)
    seat_ids: list[str] = Field(default_factory=list, max_length=100)
    temporary_order_reference: str | None = Field(default=None, max_length=240)
    status: ProbeStatus = ProbeStatus.CREATED
    error_code: str | None = Field(default=None, max_length=120)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    locked_at: str | None = None
    price_read_at: str | None = None
    cancel_requested_at: str | None = None
    cancel_confirmed_at: str | None = None
    release_verified_at: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    revision: int = Field(default=0, ge=0)


class ProbeSeatTypePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_code: str = Field(min_length=1, max_length=120)
    zone_type: str = Field(min_length=1, max_length=120)
    representative_seat_id: str = Field(min_length=1, max_length=240)
    original_price_cents: int = Field(gt=0, le=2_000_000)
    member_price_cents: int = Field(gt=0, le=2_000_000)


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(min_length=1, max_length=160)
    provider: Literal["WANDA"] = "WANDA"
    show_id: str = Field(min_length=1, max_length=240)
    status: Literal["SUCCESS", "FAILED"]
    seat_type_prices: list[ProbeSeatTypePrice] = Field(default_factory=list, max_length=100)
    release_verified: bool
    error_code: str | None = Field(default=None, max_length=120)
