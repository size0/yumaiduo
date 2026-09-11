from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RecognitionResult(BaseModel):
    """Provider facts only; no IDs or fields that authorize downstream business work."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: Literal["LIANGPIAO"] = "LIANGPIAO"
    provider_recognize_id: str | None = Field(default=None, max_length=100)
    platform_text: str | None = Field(default=None, max_length=100)
    city_text: str | None = Field(default=None, max_length=100)
    cinema_text: str | None = Field(default=None, max_length=240)
    cinema_address: str | None = Field(default=None, max_length=500)
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
    # A narrowly-scoped screenshot fact. It is populated only when the image
    # explicitly shows a bottom "W+会员" total and the visible seat count
    # agrees; it is never inferred from area IDs or provider settle prices.
    screenshot_wplus_total_price_fen: int | None = Field(default=None, ge=1)
    screenshot_wplus_ticket_count: int | None = Field(default=None, ge=1, le=20)
    confidence: float | None = Field(default=None, ge=0, le=1)
    # Recognition quality is evidence about the screenshot only.  It is not
    # Wanda realtime availability and cannot authorize a quote by itself.
    seat_matched: bool | None = Field(default=None, alias="seatMatched")
    price_mismatch: bool = Field(default=False, alias="priceMismatch")
    seat_confirm_required: bool = Field(default=False, alias="seatConfirmRequired")
    seat_confirm_reasons: list[str] = Field(
        default_factory=list, max_length=20, alias="seatConfirmReasons",
    )
    # ``has_selected_seats`` means seats were identified; this field is true
    # only when the provider explicitly establishes a complete, confirmable
    # seat set without a price mismatch.
    seat_set_verified: bool = False
    has_manual_mark: bool | None = None
    raw_provider_result: dict[str, Any] = Field(default_factory=dict)
