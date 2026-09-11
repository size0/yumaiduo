from __future__ import annotations

from datetime import date as CalendarDate
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_serializer, model_validator


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
    row_no: int | None = Field(default=None, ge=1, le=999)
    col_no: int | None = Field(default=None, ge=1, le=999)
    area_id: str | None = Field(default=None, max_length=80)
    seat_no: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=40)

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


class MovieCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movie_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=240)
    score: float = Field(default=0, ge=0, le=1)


class ShowCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_id: str = Field(min_length=1, max_length=100)
    cinema_id: int | None = Field(default=None, gt=0)
    movie_id: int | None = Field(default=None, gt=0)
    movie_name: str | None = Field(default=None, max_length=160)
    hall_name: str | None = Field(default=None, max_length=120)
    start_time: str | None = Field(default=None, max_length=50)
    end_time: str | None = Field(default=None, max_length=50)
    dimension: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, max_length=40)
    score: float = Field(default=0, ge=0, le=1)


class ProviderPriceOption(BaseModel):
    """Provider list-price metadata; never treated as a selected-seat sale price."""

    model_config = ConfigDict(extra="forbid")

    ticket_mode: str = Field(min_length=1, max_length=40)
    price_mode: str = Field(min_length=1, max_length=40)
    price_cents: int | None = Field(default=None, ge=0)
    max_price_cents: int | None = Field(default=None, ge=0)
    original_price_cents: int | None = Field(default=None, ge=0)
    stop_sale_time: str | None = Field(default=None, max_length=50)
    available: bool = False


class MovieImageInfo(BaseModel):
    """Screenshot facts plus provider-matched IDs/candidates; never a final sale quote."""

    model_config = ConfigDict(extra="forbid")

    platform: str | None = Field(default=None, max_length=40)
    is_seat_selection: bool | None = None
    cinema_truncated: bool | None = None
    cinema_id: int | None = Field(default=None, gt=0)
    cinema_name: str | None = Field(default=None, max_length=240)
    cinema_address: str | None = Field(default=None, max_length=300)
    brand_name: str | None = Field(default=None, max_length=80)
    city_code: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=80)
    movie_name: str | None = Field(default=None, max_length=160)
    movie_id: int | None = Field(default=None, gt=0)
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
    recognition_cached: bool | None = None
    # Keep the provider enum text open-ended. Liangpiao may add enum values;
    # silently converting a future value to None would erase the reason why a
    # recognition cannot continue.
    match_level: str | None = Field(default=None, max_length=80)
    no_match_reason: str | None = Field(default=None, max_length=100)
    provider_match_level: str | None = Field(default=None, max_length=80)
    provider_no_match_reason: str | None = Field(default=None, max_length=100)
    recognition_blocker: str | None = Field(default=None, max_length=100)
    resolution_diagnostic: str | None = Field(default=None, max_length=100)
    provider_request_id: str | None = Field(default=None, max_length=160)
    trace_id: str | None = Field(default=None, max_length=128)
    # These three objects form the lossless provider observation. The stable
    # fields above remain the normalized facts consumed by deterministic code.
    raw_results: dict[str, Any] = Field(default_factory=dict)
    final_results: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    cinema_hit_count: int | None = Field(default=None, ge=0)
    price_mismatch: bool | None = None
    seat_matched: bool | None = None
    show_id: str | None = Field(default=None, max_length=100)
    candidate_cinemas: list[CinemaCandidate] = Field(default_factory=list, max_length=5)
    candidate_movies: list[MovieCandidate] = Field(default_factory=list, max_length=5)
    candidate_shows: list[ShowCandidate] = Field(default_factory=list, max_length=10)
    provider_prices: list[ProviderPriceOption] = Field(default_factory=list, max_length=12)

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

    # Compatibility projection of Pricing QuoteResult. New quote.preview code
    # uses QuoteResult directly; these fields preserve canonical route and
    # lineage when an older transaction boundary still requires RealQuote.
    quote_id: str | None = None
    record_id: str | None = None
    event_id: str | None = None
    recognition_snapshot_id: str | None = None
    provider: Literal["WANDA", "LIANGPIAO"] | None = None
    quote_route: Literal["WANDA_SELF", "LIANGPIAO_LIMIT", "LIANGPIAO_FIXED"] | None = None
    generation: int | None = Field(default=None, ge=1)
    quote_expires_at: str | None = None
    provider_max_amount_cents: int | None = Field(default=None, gt=0)
    buyer_quote_cents: int | None = Field(default=None, gt=0)
    order_max_price_cents: int | None = Field(default=None, gt=0)
    calculation_evidence: dict[str, Any] = Field(default_factory=dict)

    quote_scope: Literal["exact_seats", "area_probe", "area_preview"]
    quote_date: CalendarDate | None = None
    seat_zone_type: str = Field(min_length=1, max_length=40)
    member_unit_price_cents: int | None = Field(default=None, gt=0)
    original_unit_price_cents: int | None = Field(default=None, gt=0)
    seat_type: Literal["wplus", "regular", "mixed"] | None = None
    base_unit_cents: int | None = Field(default=None, gt=0)
    base_total_cents: int | None = Field(default=None, gt=0)
    price_source: Literal[
        "realtime_wplus_area", "realtime_regular_area", "realtime_mixed_area",
        "realtime_vip_area", "liangpiao_realtime_preflight",
    ] | None = None
    price_mode: Literal["FIXED", "LIMIT"] = "FIXED"
    max_price_cents: int | None = Field(default=None, gt=0)
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
    provider_quote_id: str | None = Field(default=None, max_length=160)
    provider_quote_hash: str | None = Field(default=None, max_length=64)
    # Provider quote generation is required to bind a paid event to the exact
    # preflight snapshot that authorized the transaction.
    quote_generation: int | None = Field(default=None, ge=1)
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


class LiangpiaoPricingBand(BaseModel):
    """One bounded percentage band for the Liangpiao pricing policy."""

    model_config = ConfigDict(extra="forbid")

    min_discount_percent: float = Field(ge=0, le=100)
    max_discount_percent: float = Field(gt=0, le=100)
    markup_percent: float = Field(ge=-100, le=1000)


class WandaPricingBand(BaseModel):
    """One bounded percentage band for the Wanda W+ pricing policy."""

    model_config = ConfigDict(extra="forbid")

    min_discount_percent: float = Field(ge=0, le=100)
    max_discount_percent: float = Field(gt=0, le=100)
    # New rules use a percentage markup; the fixed field is retained for old
    # persisted policies and is ignored when markup_percent is present.
    fixed_adjustment_cents: int = Field(default=0, ge=-100_000, le=100_000)
    markup_percent: float | None = Field(default=None, ge=-100, le=1000)

    @model_serializer(mode="wrap")
    def _serialize_legacy_compatible(self, handler):
        data = handler(self)
        if self.markup_percent is None:
            data.pop("markup_percent", None)
        return data


class PricingRulesUpdate(BaseModel):
    """Deterministic pricing policy; free-form formulas are forbidden."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    wplus_friday_member_day_enabled: bool = True
    regular_adjustment_cents: int = Field(default=100, ge=-100_000, le=100_000)
    wplus_member_price_threshold_cents: int = Field(default=6_000, ge=1, le=200_000)
    wplus_adjustment_cents: int = Field(default=-290, ge=-100_000, le=100_000)
    vip_fixed_cost_cents: int = Field(default=5_000, ge=1, le=200_000)
    vip_discount_threshold_cents: int = Field(default=6_000, ge=1, le=200_000)
    vip_high_price_discount_percent: int = Field(default=90, ge=1, le=100)
    vip_low_price_discount_cents: int = Field(default=200, ge=0, le=100_000)
    liangpiao_price_mode: Literal["FIXED", "LIMIT"] = "FIXED"
    rounding_increment_cents: Literal[1, 10, 100] = 10
    # Empty lists retain the legacy fixed-rule behavior for old persisted files.
    # The operations UI sends complete 0–100 bands when dynamic rules are used.
    liangpiao_rules: list[LiangpiaoPricingBand] = Field(default_factory=list, max_length=30)
    # Optional independent bands for the FIXED fallback channel.  When this
    # field is absent in an old persisted policy, the store migrates the
    # legacy Liangpiao bands into it; an explicit empty list disables the
    # fixed-channel operator rule.
    liangpiao_fixed_rules: list[LiangpiaoPricingBand] = Field(default_factory=list, max_length=30)
    wanda_rules: list[WandaPricingBand] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_dynamic_bands(self) -> "PricingRulesUpdate":
        for field_name, bands in (
            ("liangpiao_rules", self.liangpiao_rules),
            ("liangpiao_fixed_rules", self.liangpiao_fixed_rules),
            ("wanda_rules", self.wanda_rules),
        ):
            if not bands:
                continue
            previous_max = 0.0
            for band in bands:
                if band.max_discount_percent <= band.min_discount_percent:
                    raise ValueError(f"{field_name} bands must have increasing bounds")
                if abs(band.min_discount_percent - previous_max) > 1e-9:
                    raise ValueError(f"{field_name} bands must cover 0 to 100 without gaps")
                previous_max = band.max_discount_percent
            if abs(previous_max - 100.0) > 1e-9:
                raise ValueError(f"{field_name} bands must cover 0 to 100 without gaps")
        return self


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
