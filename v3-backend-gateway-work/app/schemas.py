from __future__ import annotations

from datetime import date as CalendarDate, datetime
from enum import Enum
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class ModelSettingsUpdate(BaseModel):
    base_url: HttpUrl
    model: Annotated[str, Field(min_length=1, max_length=200)]
    api_key: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    temperature: Annotated[float, Field(ge=0, le=2)] = 0
    max_tokens: Annotated[int, Field(ge=256, le=4096)] = 1200

class ModelSettingsView(BaseModel):
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    has_api_key: bool
    updated_at: datetime | None = None


class WandaQuoteSettingsUpdate(BaseModel):
    account_phone: str = Field(default="", max_length=32)


class WandaQuoteSettingsView(BaseModel):
    account_phone: str = ""
    gateway_configured: bool = False
    account_configured: bool = False
    updated_at: datetime | None = None


class VisionContext(BaseModel):
    account_unb: str | None = Field(default=None, max_length=100)
    chat_id: str | None = Field(default=None, max_length=200)
    message_id: str | None = Field(default=None, max_length=200)


class VisionRecognizeRequest(BaseModel):
    image_url: HttpUrl
    message_text: str = Field(default="", max_length=4000)
    received_at: datetime | None = None
    context: VisionContext = Field(default_factory=VisionContext)


class ImageType(str, Enum):
    SEAT_MAP = "SEAT_MAP"
    ORDER_CONFIRM = "ORDER_CONFIRM"
    CHAT_IMAGE = "CHAT_IMAGE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class SeatZoneType(str, Enum):
    WPLUS = "W+"
    REGULAR = "普通"
    DISCOUNT = "特惠"
    PREMIUM = "优选"
    UNKNOWN = "未知"

    @classmethod
    def _missing_(cls, value: object) -> "SeatZoneType" | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or normalized.upper() == "UNKNOWN" or normalized in {"未知", "不确定", "未识别"}:
            return cls.UNKNOWN
        if "W+" in normalized:
            return cls.WPLUS
        if "特惠" in normalized:
            return cls.DISCOUNT
        if "优选" in normalized:
            return cls.PREMIUM
        if "普通" in normalized:
            return cls.REGULAR
        # Unknown visual labels are safe only as UNKNOWN; they can never
        # select a quote zone or create a price fact.
        return cls.UNKNOWN


class ScreenshotPlatform(str, Enum):
    WANDA = "WANDA"
    MAOYAN = "MAOYAN"
    TAOPIAOPIAO = "TAOPIAOPIAO"
    WANDA_MINI_PROGRAM = "WANDA_MINI_PROGRAM"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> "ScreenshotPlatform" | None:
        text = str(value or "").strip().lower()
        if "猫眼" in text or text == "maoyan": return cls.MAOYAN
        if "淘票票" in text or text in {"taopiaopiao", "tao_piao_piao"}: return cls.TAOPIAOPIAO
        if "小程序" in text and "万达" in text: return cls.WANDA_MINI_PROGRAM
        if "万达" in text or text == "wanda": return cls.WANDA
        if not text or text in {"unknown", "未知"}: return cls.UNKNOWN
        return None


class ScreenshotContainer(str, Enum):
    SEAT_MAP = "SEAT_MAP"
    BOTTOM_SELECTED_SEAT_CARD = "BOTTOM_SELECTED_SEAT_CARD"
    ORDER_CONFIRM_CARD = "ORDER_CONFIRM_CARD"
    CHAT = "CHAT"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> "ScreenshotContainer" | None:
        text = str(value or "").strip().lower()
        if "底部" in text and ("选座" in text or "座位" in text): return cls.BOTTOM_SELECTED_SEAT_CARD
        if "确认" in text or "订单" in text: return cls.ORDER_CONFIRM_CARD
        if "座位图" in text or text == "seat_map": return cls.SEAT_MAP
        if "聊天" in text or text == "chat": return cls.CHAT
        if not text or text in {"unknown", "未知"}: return cls.UNKNOWN
        return None


class VisiblePrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_type: SeatZoneType = SeatZoneType.UNKNOWN
    label: str | None = None
    price_yuan: float = Field(default=0, ge=0)


class OfficialSelectedSeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seat_number: str = Field(min_length=1, max_length=80)
    price: float = Field(default=0, ge=0, description="截图明确展示的单张元价；不可由总价推算")
    ticket_status: str | None = Field(default=None, max_length=80)


class OfficialSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_selected: bool = False
    selected_seat_numbers: list[str] = Field(default_factory=list)
    selected_count: int = Field(default=0, ge=0, le=20)
    seats: list[OfficialSelectedSeat] = Field(default_factory=list, max_length=20)
    total_price: float = Field(default=0, ge=0, description="仅抄录底部官方卡片明确显示的总价")
    ticket_status: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def synchronize_bottom_card_seats(self) -> "OfficialSelection":
        card_seats = list(dict.fromkeys(seat.seat_number.strip() for seat in self.seats if seat.seat_number.strip()))
        if card_seats:
            self.selected_seat_numbers = card_seats
            self.selected_count = len(card_seats)
        return self


class HandDrawnCircle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exists: bool = False
    color: str | None = None
    rough_area: str | None = None
    suspected_row_range: str | None = None
    suspected_zone_type: SeatZoneType = SeatZoneType.UNKNOWN
    estimated_seat_count: int = Field(default=0, ge=0, le=20)
    contains_wplus_icon: bool = False

    @field_validator("suspected_row_range", mode="before")
    @classmethod
    def normalize_row_range(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            values = [str(item).strip() for item in value if str(item).strip()]
            return "-".join(values) or None
        return value

    @field_validator("suspected_zone_type", mode="before")
    @classmethod
    def normalize_suspected_zone_type(cls, value: object) -> object:
        return SeatZoneType.UNKNOWN.value if value is None else value


class ScreenState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    has_please_select_seat: bool = False
    has_confirm_seat_button: bool = False
    has_selected_seat_cards: bool = False


class RecognitionConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overall: float = Field(default=0, ge=0, le=1)
    cinema: float = Field(default=0, ge=0, le=1)
    movie: float = Field(default=0, ge=0, le=1)
    date: float = Field(default=0, ge=0, le=1)
    showtime: float = Field(default=0, ge=0, le=1)
    seat_selection: float = Field(default=0, ge=0, le=1)
    seat_zone: float = Field(default=0, ge=0, le=1)
    price: float = Field(default=0, ge=0, le=1)


class Recognition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: ScreenshotPlatform = ScreenshotPlatform.UNKNOWN
    container: ScreenshotContainer = ScreenshotContainer.UNKNOWN
    image_type: ImageType = ImageType.UNKNOWN
    city: str | None = Field(default=None, max_length=80)
    cinema: str | None = None
    cinema_address_hint: str | None = Field(default=None, max_length=240)
    movie: str | None = None
    date: CalendarDate | None = None
    showtime: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(-\d{2}:\d{2})?$")
    hall: str | None = None
    language_format: str | None = None
    seat_zone_types: list[SeatZoneType] = Field(default_factory=list)
    visible_prices: list[VisiblePrice] = Field(default_factory=list)
    official_selection: OfficialSelection = Field(default_factory=OfficialSelection)
    hand_drawn_circle: HandDrawnCircle = Field(default_factory=HandDrawnCircle)
    screen_state: ScreenState = Field(default_factory=ScreenState)
    confidence: RecognitionConfidence = Field(default_factory=RecognitionConfidence)
    missing_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class VisionRecognizeResponse(BaseModel):
    ok: bool = True
    prompt_version: str = "wanda-vlm-recognition-v3"
    recognition: Recognition


class StorageSettingsView(BaseModel):
    bucket_url: str
    region: str
    has_secret_id: bool
    has_secret_key: bool
    updated_at: datetime | None = None


class ImageUploadResponse(BaseModel):
    url: str
    object_key: str


class QuoteScope(str, Enum):
    EXACT_SEATS = "exact_seats"
    AREA_PROBE = "area_probe"


class QuoteTextFactExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    message_text: Annotated[str, Field(min_length=1, max_length=4000)]
    observed_at: Annotated[int, Field(ge=0)]


class QuoteTextFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_intent: bool
    city: str | None = Field(default=None, max_length=80)
    cinema: str | None = Field(default=None, max_length=160)
    movie: str | None = Field(default=None, max_length=160)
    date: CalendarDate | None = None
    showtime: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    hall: str | None = Field(default=None, max_length=80)
    ticket_count: int | None = Field(default=None, ge=1, le=20)
    seat_numbers: list[str] = Field(default_factory=list, max_length=20)
    requested_row: int | None = Field(default=None, ge=1, le=99)
    refers_to_image_positions: bool = False
    confidence: float = Field(ge=0, le=1)

    @field_validator("seat_numbers")
    @classmethod
    def validate_seat_numbers(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = str(value).replace(" ", "").strip()
            if not re.fullmatch(r"\d{1,2}排\d{1,3}座", normalized):
                raise ValueError("invalid seat number")
            if normalized not in result:
                result.append(normalized)
        return result


class QuoteTextFactExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["extracted", "failed"]
    extractor_version: str = Field(min_length=1, max_length=100)
    facts: QuoteTextFacts | None = None
    failure_code: str | None = Field(default=None, max_length=100)


class QuoteRealtimeRequest(BaseModel):
    recognition: Recognition
    ticket_count: Annotated[int | None, Field(default=None, ge=1, le=20)]


class SeatQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_number: str = Field(min_length=1, max_length=80)
    seat_zone_type: SeatZoneType
    original_price_cents: int = Field(gt=0)
    member_price_cents: int | None = Field(default=None, gt=0)
    channel_fee_cents: int = Field(default=0, ge=0)
    unit_quote_cents: int = Field(gt=0)


class QuoteRealtimeResponse(BaseModel):
    quote_scope: QuoteScope
    seat_zone_type: SeatZoneType
    member_unit_price_cents: int | None = Field(default=None, gt=0)
    unit_quote_cents: int | None = Field(default=None, gt=0)
    total_quote_cents: int | None = Field(default=None, gt=0)
    channel_fee_total_cents: int | None = Field(default=None, ge=0)
    seat_quotes: list[SeatQuote] = Field(default_factory=list, max_length=20)
    ticket_count: int | None = Field(default=None, ge=1, le=20)
    needs_ticket_count: bool
    pricing_source: str
    pricing_account_ref: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    pricing_rule_version: str | None = Field(default=None, min_length=8, max_length=80)
    detail: str
    matched_cinema_name: str | None = Field(default=None, max_length=300)
    buyer_app_purchase_recommended: bool = False
    reply_text: str | None = Field(default=None, max_length=500)
    timings_ms: dict[str, int] = Field(default_factory=dict)


class QuotePreviewIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    buyer_label: Annotated[str, Field(min_length=1, max_length=200)]
    message_text: Annotated[str, Field(default="", max_length=4000)]
    image_url: HttpUrl | None = None
    ticket_count: Annotated[int | None, Field(default=None, ge=1, le=20)]


class QuotePreviewQuoteRequest(QuoteRealtimeRequest):
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]


class AvailableWplusSeatsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recognition: Recognition
    row: Annotated[int | None, Field(default=None, ge=1, le=99)]


class AvailableWplusSeatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row: int | None
    seats: list[str] = Field(default_factory=list, max_length=30)
    available_count: int = Field(ge=0)
    wplus_offer_available: bool
    matched_cinema_name: str | None = Field(default=None, max_length=300)


class QuoteShowtimeResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recognition: Recognition


class QuoteShowtimeResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recognition: Recognition
    matched_cinema_name: str | None = Field(default=None, max_length=300)


class QuoteMatchCandidate(BaseModel):
    """A bounded match-hint correction; never contains money or seat facts."""

    model_config = ConfigDict(extra="forbid")

    city: str | None = Field(default=None, max_length=80)
    cinema: str | None = Field(default=None, max_length=160)
    movie: str | None = Field(default=None, max_length=160)
    date: CalendarDate | None = None
    showtime: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(?:-\d{2}:\d{2})?$")
    hall: str | None = Field(default=None, max_length=80)


class QuoteMatchCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recognition: Recognition


class QuoteMatchCandidateResponse(BaseModel):
    candidates: list[QuoteMatchCandidate] = Field(default_factory=list, max_length=2)


class PendingQuoteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    buyer_label: str
    message_summary: str
    cinema: str | None = None
    movie: str | None = None
    date: str | None = None
    showtime: str | None = None
    hall: str | None = None
    unit_quote_cents: int | None = None
    total_quote_cents: int | None = None
    ticket_count: int | None = Field(default=None, ge=1, le=20)
    seat_quotes: list[SeatQuote] = Field(default_factory=list, max_length=20)
    status: Literal["UNSENT_PREVIEW"]
    created_at: datetime


class PendingQuotesResponse(BaseModel):
    records: list[PendingQuoteRecord] = Field(default_factory=list)


class QuotePreviewIngestResponse(BaseModel):
    status: Literal["preview_ready", "needs_image", "needs_confirmation", "ignored", "failed"]
    duplicate: bool = False
    reply_text: str | None = Field(default=None, max_length=500)
    failure_code: str | None = Field(default=None, max_length=100)
    quote_unit_cents: int | None = Field(default=None, ge=1)
    quote_total_cents: int | None = Field(default=None, ge=1)
    quote_ticket_count: int | None = Field(default=None, ge=1, le=20)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["buyer", "seller"]
    content: Annotated[str, Field(min_length=1, max_length=1000)]
    source: Literal["buyer", "plugin", "external_seller", "unknown"] | None = None
    sent_at: datetime | None = None


class ReplyPreviewIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    buyer_label: Annotated[str, Field(min_length=1, max_length=200)]
    latest_message: Annotated[str, Field(min_length=1, max_length=1000)]
    history: Annotated[list[ConversationMessage], Field(min_length=1, max_length=20)]


class ReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["票价咨询", "选座核价", "补充信息", "订单进度", "售后咨询", "人工接管", "其他"]
    confidence: float = Field(ge=0, le=1)
    needs_human: bool
    reply: Annotated[str, Field(min_length=1, max_length=500)]
    reason: Annotated[str, Field(min_length=1, max_length=160)]


class ReplyPreviewIngestResponse(BaseModel):
    status: Literal["preview_ready", "failed"]
    duplicate: bool = False
    draft: ReplyDraft | None = None
    failure_code: str | None = Field(default=None, max_length=100)


AgentAction = Literal[
    "respond",
    "ask_for_image",
    "ask_for_city",
    "ask_for_missing_information",
    "start_quote",
    "resolve_ticket_identity",
    "recognize_image",
    "resolve_showtime",
    "quote_realtime",
    "read_active_quote",
    "request_price_change",
    "create_manual_task",
    "get_manual_task_status",
    "show_available_wplus_seats",
    "record_seat_preference",
    "confirm_quote",
    "get_order_status",
    "read_linked_order",
    "inspect_ticket_request",
    "handoff",
    "wait",
]


class AgentToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "warning", "error"]
    tool: Annotated[str, Field(min_length=1, max_length=64)]
    summary: Annotated[str, Field(max_length=200)] = ""
    facts: dict[str, Any] = Field(default_factory=dict)
    next_actions: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)
    stop_reason: Annotated[str | None, Field(max_length=100)] = None

    @model_validator(mode="after")
    def validate_bounded_facts(self) -> "AgentToolObservation":
        if len(self.facts) > 30 or len(str(self.facts)) > 6000:
            raise ValueError("agent observation facts are too large")
        return self


class AgentTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    latest_message: Annotated[str, Field(min_length=1, max_length=1000)]
    history: Annotated[list[ConversationMessage], Field(min_length=1, max_length=20)]
    state: dict[str, Any] = Field(default_factory=dict)
    observations: Annotated[list[AgentToolObservation], Field(max_length=8)] = Field(default_factory=list)
    has_image: bool = False

    @model_validator(mode="after")
    def validate_bounded_state(self) -> "AgentTurnRequest":
        if len(self.state) > 20 or len(str(self.state)) > 12000:
            raise ValueError("agent state is too large")
        return self


class AgentExperienceCandidate(BaseModel):
    """Generalized, low-risk shop experience. Never contains transaction facts or raw identities."""

    model_config = ConfigDict(extra="forbid")

    topic: Literal["问候与结束语", "图片要求", "服务范围", "服务流程", "沟通方式"]
    question_pattern: Annotated[str, Field(min_length=5, max_length=120)]
    response_guidance: Annotated[str, Field(min_length=5, max_length=300)]
    example_reply: Annotated[str, Field(min_length=1, max_length=300)]
    outcome_signal: Literal["buyer_acknowledged", "buyer_progressed"]
    confidence: float = Field(ge=0.85, le=1)

    @model_validator(mode="after")
    def validate_low_risk_content(self) -> "AgentExperienceCandidate":
        combined = " ".join((self.question_pattern, self.response_guidance, self.example_reply))
        forbidden = re.compile(
            r"(?:[0-9０-９]|[零一二三四五六七八九十百千万]{2,}|元|块钱|价格|优惠|折扣|会员价|"
            r"订单|付款|支付|改价|出票|发货|退款|库存|余票|可售|微信|手机号|电话|https?://|www\.|@)",
            re.IGNORECASE,
        )
        if forbidden.search(combined):
            raise ValueError("unsafe conversation experience")
        return self


class ConversationExperienceIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: Annotated[str, Field(min_length=1, max_length=128)]
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    candidate: AgentExperienceCandidate


class AgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["票价咨询", "选座核价", "补充信息", "订单进度", "售后咨询", "人工接管", "其他"]
    confidence: float = Field(ge=0, le=1)
    goal: Annotated[str, Field(max_length=160)] = ""
    action: AgentAction
    arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: Annotated[list[str], Field(max_length=10)] = Field(default_factory=list)
    reply: Annotated[str, Field(max_length=500)] = ""
    needs_human: bool = False
    reason: Annotated[str, Field(min_length=1, max_length=160)]
    experience_candidate: AgentExperienceCandidate | None = None

    @model_validator(mode="after")
    def validate_bounded_arguments(self) -> "AgentPlan":
        forbidden = {"amount", "amount_cents", "price", "price_fee", "discount", "fee", "total", "order_id", "token", "secret", "authorization", "cookie"}
        if len(self.arguments) > 12 or len(str(self.arguments)) > 2000:
            raise ValueError("agent arguments are too large")

        def contains_forbidden(value: object, depth: int = 0) -> bool:
            if depth > 3:
                return True
            if isinstance(value, dict):
                return any(str(key).lower() in forbidden or contains_forbidden(item, depth + 1) for key, item in value.items())
            if isinstance(value, list):
                return any(contains_forbidden(item, depth + 1) for item in value)
            return False

        if contains_forbidden(self.arguments):
            raise ValueError("agent arguments contain transaction authority")
        parameterless_actions = {"request_price_change", "confirm_quote", "get_manual_task_status", "show_available_wplus_seats"}
        if self.action in parameterless_actions and self.arguments:
            raise ValueError("agent action accepts no model arguments")
        tool_actions = {"start_quote", "recognize_image", "resolve_showtime", "quote_realtime", "read_active_quote", "request_price_change", "create_manual_task", "get_manual_task_status", "show_available_wplus_seats", "record_seat_preference", "confirm_quote", "get_order_status", "read_linked_order", "inspect_ticket_request"}
        if self.action in tool_actions and self.reply.strip():
            raise ValueError("agent tool actions must wait for authoritative observations before replying")
        return self


class AgentTurnResponse(BaseModel):
    status: Literal["planned", "failed"]
    plan: AgentPlan | None = None
    failure_code: Annotated[str | None, Field(max_length=100)] = None
