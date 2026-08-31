from __future__ import annotations

from datetime import date as CalendarDate
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


_DISPLAYED_AMOUNT_PATTERN = re.compile(
    r"^\s*(?:(?:CNY|RMB)\s*|[¥￥]\s*)?"
    r"(\d{1,12}(?:,\d{3})*(?:\.\d+)?|\d{1,12}(?:\.\d+)?)"
    r"\s*(?:元|CNY|RMB)?\s*$",
    re.IGNORECASE,
)


def _normalize_displayed_amount(value: object) -> object:
    """Strip only explicit currency notation; never interpret qualified or computed prices."""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        match = _DISPLAYED_AMOUNT_PATTERN.fullmatch(value)
        if match:
            return float(match.group(1).replace(",", ""))
    raise ValueError("displayed amount must be a number with optional explicit currency notation")


def _normalize_showtime(value: object) -> object:
    """Repair deterministic JSON number renderings such as 16.2 → 16:20."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip()
    colon = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if colon:
        hour, minute = (int(part) for part in colon.groups())
    elif re.fullmatch(r"\d{3,4}", text):
        hour, minute = int(text[:-2]), int(text[-2:])
    elif re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", text):
        hour_text, separator, fraction = text.partition(".")
        hour = int(hour_text)
        minute = int(fraction.ljust(2, "0")) if separator else 0
        if minute > 59:
            minute = int((Decimal(f"0.{fraction}") * 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        return value
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("showtime must contain a valid hour and minute")
    return f"{hour:02d}:{minute:02d}"


class SelectedSeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_number: str = Field(min_length=1, max_length=40)
    displayed_price: float | None = Field(default=None, ge=0)

    @field_validator("displayed_price", mode="before")
    @classmethod
    def normalize_displayed_price(cls, value: object) -> object:
        return _normalize_displayed_amount(value)


class PriceZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="未知", min_length=1, max_length=40)
    displayed_price: float | None = Field(default=None, ge=0)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_unknown_name(cls, value: object) -> str:
        normalized = str(value or "").strip()
        return normalized or "未知"

    @field_validator("displayed_price", mode="before")
    @classmethod
    def normalize_displayed_price(cls, value: object) -> object:
        return _normalize_displayed_amount(value)


class CinemaCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cinema_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=240)
    city_name: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=300)
    score: float = Field(default=0, ge=0, le=1)


class MovieImageInfo(BaseModel):
    """Only facts visibly present in a screenshot; no inferred sale price or inventory."""

    model_config = ConfigDict(extra="forbid")

    platform: str | None = Field(default=None, max_length=40)
    cinema_name: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default=None, max_length=80)
    movie_name: str | None = Field(default=None, max_length=160)
    date_text: str | None = Field(default=None, max_length=80)
    date: CalendarDate | None = None
    showtime_start: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    showtime_end: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    hall_name: str | None = Field(default=None, max_length=120)
    language: str | None = Field(default=None, max_length=40)
    format: str | None = Field(default=None, max_length=40)
    selected_seats: list[SelectedSeat] = Field(default_factory=list, max_length=30)
    selected_count_visible: int = Field(default=0, ge=0, le=30)
    ticket_codes: list[str] = Field(default_factory=list, max_length=20)
    displayed_total: float | None = Field(default=None, ge=0)
    currency: Literal["CNY"] = "CNY"
    price_zones: list[PriceZone] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0, ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    recognition_id: str | None = Field(default=None, max_length=100)
    match_level: Literal["EXACT", "CANDIDATE", "NONE", "SHOW_EXPIRED"] | None = None
    show_id: str | None = Field(default=None, max_length=100)
    candidate_cinemas: list[CinemaCandidate] = Field(default_factory=list, max_length=5)

    @field_validator("ticket_codes", mode="before")
    @classmethod
    def normalize_ticket_codes(cls, value: object) -> object:
        if value is None:
            return []
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            raise ValueError("ticket_codes must be a list")
        normalized = [re.sub(r"\s+", "", str(item).strip()) for item in values]
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("ticket_codes must be non-empty and unique")
        return normalized

    @field_validator("displayed_total", mode="before")
    @classmethod
    def normalize_displayed_total(cls, value: object) -> object:
        return _normalize_displayed_amount(value)

    @field_validator("showtime_start", "showtime_end", mode="before")
    @classmethod
    def normalize_showtimes(cls, value: object) -> object:
        return _normalize_showtime(value)

    @model_validator(mode="after")
    def visible_seat_count_matches_labels(self) -> "MovieImageInfo":
        if self.selected_count_visible != len(self.selected_seats):
            raise ValueError("selected_count_visible must equal the visible selected seat labels")
        return self

    @computed_field
    @property
    def seat_display(self) -> str:
        if self.selected_seats:
            return "、".join(seat.seat_number for seat in self.selected_seats)
        return "W+座位"

    @computed_field
    @property
    def seat_display_mode(self) -> Literal["specific", "wplus_fallback"]:
        return "specific" if self.selected_seats else "wplus_fallback"

    @computed_field
    @property
    def fulfillment_route(self) -> Literal["LIANGPIAO_AUTO", "WANDA_MANUAL"]:
        """Choose the provider route from explicit seats, not from price-zone text."""
        return "LIANGPIAO_AUTO" if self.selected_seats else "WANDA_MANUAL"


class RecognitionResponse(BaseModel):
    ok: Literal[True] = True
    data: MovieImageInfo


class TicketImageRecognitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str = Field(min_length=1, max_length=2_048)
    city_name: str | None = Field(default=None, max_length=80)


class ChatTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("conversation_id", "text")
    @classmethod
    def strip_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized


class RealSeatQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    seat_number: str = Field(min_length=1, max_length=80)
    seat_zone_type: str = Field(min_length=1, max_length=40)
    original_price_cents: int = Field(gt=0)
    member_price_cents: int | None = Field(default=None, gt=0)
    channel_fee_cents: int = Field(default=0, ge=0)
    unit_quote_cents: int = Field(gt=0)


class RealQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quote_scope: Literal["exact_seats", "area_probe", "area_preview"]
    quote_date: CalendarDate | None = None
    seat_zone_type: str = Field(min_length=1, max_length=40)
    member_unit_price_cents: int | None = Field(default=None, gt=0)
    original_unit_price_cents: int | None = Field(default=None, gt=0)
    seat_type: Literal["wplus", "regular", "mixed"] | None = None
    base_unit_cents: int | None = Field(default=None, gt=0)
    base_total_cents: int | None = Field(default=None, gt=0)
    price_source: Literal["realtime_wplus_area", "realtime_regular_area", "realtime_mixed_area"] | None = None
    unit_quote_cents: int | None = Field(default=None, gt=0)
    total_quote_cents: int | None = Field(default=None, gt=0)
    channel_fee_total_cents: int | None = Field(default=None, ge=0)
    seat_quotes: list[RealSeatQuote] = Field(default_factory=list, max_length=20)
    ticket_count: int | None = Field(default=None, ge=1, le=20)
    needs_ticket_count: bool = False
    same_type_probe_used: bool = False
    pricing_source: str = Field(default="", max_length=300)
    pricing_rule_version: str | None = Field(default=None, max_length=100)
    detail: str = Field(default="", max_length=500)
    matched_cinema_name: str | None = Field(default=None, max_length=300)
    matched_city_name: str | None = Field(default=None, max_length=100)
    matched_movie_name: str | None = Field(default=None, max_length=300)
    matched_showtime_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    matched_showtime_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    matched_hall_name: str | None = Field(default=None, max_length=300)
    buyer_app_purchase_recommended: bool = False
    reply_text: str | None = Field(default=None, max_length=500)
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ChatAssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    conversation_id: str = Field(min_length=1, max_length=128)
    role: Literal["assistant"] = "assistant"
    message_type: Literal["movie_recognition", "guidance", "ai_reply"]
    text: str = Field(min_length=1, max_length=2000)
    recognition: MovieImageInfo | None = None
    quote: RealQuote | None = None


class ChatMessageResponse(BaseModel):
    ok: Literal[True] = True
    message: ChatAssistantMessage


class VisionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    chat_base_url: str | None = Field(default=None, max_length=500)
    chat_model: str | None = Field(default=None, max_length=200)
    enable_thinking: bool = False
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "none"
    vision_prompt: str = Field(min_length=1, max_length=20_000)
    chat_prompt: str | None = Field(default=None, max_length=20_000)
    api_key: str | None = Field(default=None, max_length=2048)
    clear_api_key: bool = False
    chat_api_key: str | None = Field(default=None, max_length=2048)
    clear_chat_api_key: bool = False

    @field_validator("base_url", "model", "vision_prompt")
    @classmethod
    def strip_required_settings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("setting cannot be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def require_https_base_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        return value.rstrip("/")

    @field_validator("chat_base_url", "chat_model", "chat_prompt")
    @classmethod
    def normalize_optional_chat_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("optional service setting cannot be blank")
        return normalized

    @field_validator("chat_base_url")
    @classmethod
    def require_https_chat_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("service base URL must use HTTPS")
        return value.rstrip("/")

    @field_validator("api_key", "chat_api_key")
    @classmethod
    def normalize_optional_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class VisionSettingsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    chat_base_url: str
    chat_model: str
    enable_thinking: bool
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"]
    vision_prompt: str
    chat_prompt: str
    has_api_key: bool
    masked_api_key: str
    has_chat_api_key: bool
    masked_chat_api_key: str
    updated_at: str | None = None


class PricingRulesUpdate(BaseModel):
    """Deterministic integer-cent quote policy; free-form formulas are forbidden."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    wplus_friday_member_day_enabled: bool = True
    regular_adjustment_cents: int = Field(default=100, ge=-100_000, le=100_000)
    wplus_member_price_threshold_cents: int = Field(default=6_000, ge=1, le=200_000)
    wplus_adjustment_cents: int = Field(default=-290, ge=-100_000, le=100_000)
    rounding_increment_cents: Literal[1, 10, 100] = 10


class PricingRulesView(PricingRulesUpdate):
    revision: int = Field(default=0, ge=0)
    rule_version: str = Field(min_length=1, max_length=100)
    updated_at: str | None = None
    calculation_summary: str = Field(min_length=1, max_length=500)


class ModelCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["vision", "chat"] = "vision"
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=2048)

    @field_validator("base_url")
    @classmethod
    def validate_catalog_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        return normalized

    @field_validator("api_key")
    @classmethod
    def normalize_catalog_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ModelCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list, max_length=1000)
    count: int = Field(ge=0, le=1000)
