from __future__ import annotations

import hashlib
import inspect
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from time import monotonic, perf_counter
from collections.abc import Mapping
from typing import Annotated, Callable, Protocol
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import AgentHarness, build_read_only_registry
from .agent.model import OpenAICompatibleModel
from .automation_mode import normalize_automation_mode
from .chat import build_guidance_reply, build_recognition_reply
from .chat_service import CustomerServiceChatService
from .cinema_routing import CinemaRouteResolver
from .conversation_policy_store import ConversationPolicyStore
from .diagnostics import DiagnosticsStore
from .errors import ImageValidationError, ProviderError, RecognitionError
from .keyword_image_store import MAX_KEYWORD_IMAGE_BYTES, KeywordImageStore
from .knowledge_store import KnowledgeStore
from .liangpiao_exact_quote import LiangpiaoExactQuoteAdapter
from .liangpiao_callbacks import CallbackError, CallbackVerifier, LiangpiaoCallbackHandler
from .liangpiao_client import LiangpiaoClient
from .liangpiao_order_service import LiangpiaoOrderService
from .model_catalog import ModelCatalogService
from .models import (
    ChatAssistantMessage,
    ChatMessageResponse,
    ChatTextRequest,
    ModelCatalogRequest,
    ModelCatalogResponse,
    MovieImageInfo,
    PricingRulesUpdate,
    PricingRulesView,
    RealQuote,
    RecognitionResponse,
    TicketImageRecognitionRequest,
    VisionSettingsUpdate,
    VisionSettingsView,
)
from .observability import LOGGER, REQUEST_ID
from .pending_cinema_candidate_store import PendingCinemaCandidateStore
from .pricing_store import PricingRulesStore
from .plugin_automation import (
    RulesFirstDecisionEngine,
    _public_recognition_payload,
    validate_image_url,
)
from .quote_record_store import QuoteRecordStore
from .recognition_snapshot_store import RecognitionSnapshotStore
from .reminder_service import plan_shipped_order_reminders
from .reminder_store import ReminderStore
from .reply_template_store import ReplyTemplateStore, render_template
from .rule_state_coordinator import RuleStateCoordinator
from .rules_first_runtime import RulesFirstRuntime
from .rules_first_state_store import SqliteTransactionStateStore
from .rules_first_store import RulesFirstStore
from .service import MovieImageRecognitionService
from .selected_seat_quote_service import QuoteServiceError, SelectedSeatQuoteRequest, SelectedSeatQuoteService
from .settings_store import PersistentSettingsStore
from .shop_automation_store import ShopAutomationStore
from .transaction_state_store import TransactionStateStore
from .wanda_direct_quote import WandaDirectQuoteService


APP_NAME = "wanda-movie-image-recognition"
UPLOAD_READ_LIMIT = 20 * 1024 * 1024
# Local development keeps ``app`` under ``backend`` while production flattens
# the package into the release root. Resolve the UI from either layout; this
# also remains correct when ``current`` is a symlink to a release directory.
_resolved_app_root = Path(__file__).resolve().parents[1]
_source_root = Path(__file__).resolve().parents[2]
PLUGIN_UI_PATH = _source_root / "plugin-runtime" / "wanda-seat-autoquote" / "ui"
if not PLUGIN_UI_PATH.is_dir():
    PLUGIN_UI_PATH = _resolved_app_root / "plugin-runtime" / "wanda-seat-autoquote" / "ui"
INDEX_PATH = PLUGIN_UI_PATH / "index.html"


async def _execute_agent_recognition_tool(
    recognition_service: object,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """Execute the global Agent recognition contract for one or more images.

    The plugin runtime has a request-scoped executor, but the public chat
    service also receives the same tool schema.  Keep this executor aligned
    with that schema: validate ``image_urls`` as a bounded batch, preserve
    per-image failures, and retain the single-image response shape for older
    callers.  This helper intentionally does not quote or mutate orders.
    """
    has_single_url = arguments.get("image_url") is not None
    has_batch_urls = arguments.get("image_urls") is not None
    if has_single_url and has_batch_urls:
        return {"ok": False, "error": "image_arguments_conflict"}
    raw_urls = arguments.get("image_urls")
    if raw_urls is None:
        raw_urls = [arguments.get("image_url")]
    if not isinstance(raw_urls, list) or not 1 <= len(raw_urls) <= 3:
        return {"ok": False, "error": "image_count_invalid"}
    try:
        image_urls = [validate_image_url(value) for value in raw_urls]
    except ValueError:
        return {"ok": False, "error": "image_url_invalid"}
    if len(set(image_urls)) != len(image_urls):
        return {"ok": False, "error": "duplicate_image_url"}
    recognize_from_url = getattr(recognition_service, "recognize_from_url", None)
    if not callable(recognize_from_url):
        return {"ok": False, "error": "recognition_tool_unavailable"}
    buyer_message = str(arguments.get("buyer_message") or "")[:2_000]
    recognitions: list[dict[str, object]] = []
    image_results: list[dict[str, object]] = []
    for image_index, image_url in enumerate(image_urls):
        try:
            recognition = await recognize_from_url(
                image_url, buyer_message=buyer_message,
            )
            if not isinstance(recognition, MovieImageInfo):
                raise TypeError("recognition_result_invalid")
        except Exception as error:  # noqa: BLE001 - isolate each image
            LOGGER.warning(
                "event=agent_recognition_tool_failed image_index=%d error_type=%s",
                image_index, type(error).__name__,
            )
            image_results.append({
                "image_index": image_index,
                "status": "error",
                "error": "recognition_failed",
            })
            continue
        recognitions.append(_public_recognition_payload(recognition))
        image_results.append({"image_index": image_index, "status": "success"})
    if not recognitions:
        return {
            "ok": False,
            "error": "recognition_failed",
            "image_count": len(image_urls),
            "recognized_image_count": 0,
            "partial_failure": False,
            "image_results": image_results,
        }
    if len(image_urls) == 1:
        return {"ok": True, "recognition": recognitions[0]}
    return {
        "ok": True,
        "recognitions": recognitions,
        "image_count": len(image_urls),
        "recognized_image_count": len(recognitions),
        "partial_failure": len(recognitions) != len(image_urls),
        "image_results": image_results,
    }


def _liangpiao_quote_public(
    quote: Mapping[str, object],
    buyer_record: Mapping[str, object] | None = None,
) -> dict[str, object]:
    snapshot = quote.get("snapshot") if isinstance(quote.get("snapshot"), Mapping) else {}
    preflight = snapshot.get("preflight_response") if isinstance(snapshot.get("preflight_response"), Mapping) else {}
    seats = quote.get("seats") if isinstance(quote.get("seats"), list) else snapshot.get("seats")
    seat_count = len(seats) if isinstance(seats, list) else 0
    market_amount = snapshot.get("market_amount_fen")
    if market_amount is None:
        market_amount = preflight.get("marketAmount")
    provider_base = snapshot.get("provider_base_amount_fen")
    if provider_base is None:
        provider_base = preflight.get("totalAmount") if snapshot.get("price_mode") == "FIXED" else preflight.get("estimateAmount")
    buyer_amount = quote.get("buyer_amount_fen")
    buyer_record = buyer_record or {}
    return {
        "source": "liangpiao",
        "record_id": quote.get("quote_id"),
        "quote_id": quote.get("quote_id"),
        "status": "succeeded" if str(quote.get("status") or "active") in {"active", "ok", "succeeded"} else "failed",
        "city": buyer_record.get("city"),
        "cinema": buyer_record.get("cinema") or "影院待确认",
        "movie": buyer_record.get("movie"),
        "quote_date": buyer_record.get("quote_date") or buyer_record.get("date_text"),
        "showtime_start": buyer_record.get("showtime_start"),
        "seat_display": buyer_record.get("seat_display") or "、".join(
            str(item.get("seat_no") or item.get("seatNo") or "")
            for item in seats if isinstance(item, Mapping)
        ),
        "price_mode": snapshot.get("price_mode") or quote.get("price_mode"),
        "price_mode_label": "良票一口价" if (snapshot.get("price_mode") or quote.get("price_mode")) == "FIXED" else "良票预估价",
        "market_amount_fen": market_amount,
        "provider_base_amount_fen": provider_base,
        "buyer_unit_amount_fen": (
            int(buyer_amount) // seat_count
            if seat_count and isinstance(buyer_amount, (int, float)) and int(buyer_amount) % seat_count == 0
            else None
        ),
        "buyer_amount_fen": buyer_amount,
        "ticket_count": seat_count,
        "buyer_id": buyer_record.get("buyer_id"),
        "buyer_nick": buyer_record.get("buyer_nick") or buyer_record.get("buyer_id"),
        "created_at": quote.get("created_at"),
        "expires_at": quote.get("expires_at"),
    }


def _liangpiao_order_no(value: Mapping[str, object]) -> str:
    for key in ("orderNo", "order_no", "providerOrderNo", "provider_order_no"):
        order_no = str(value.get(key) or "").strip()
        if order_no:
            return order_no
    return ""


def _liangpiao_items(value: Mapping[str, object]) -> list[dict[str, object]]:
    items = value.get("list") or value.get("items") or value.get("orders")
    return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _liangpiao_local_public(local: Mapping[str, object], quote: Mapping[str, object] | None = None) -> dict[str, object]:
    payload = local.get("payload") if isinstance(local.get("payload"), Mapping) else {}
    return {
        "source": "liangpiao",
        "out_order_no": local.get("out_order_no"),
        "provider_order_no": local.get("provider_order_no"),
        "buyer_id": local.get("buyer_id"),
        "buyer_nick": local.get("buyer_nick") or local.get("buyer_id"),
        "conversation_id": local.get("conversation_id"),
        "shop_id": local.get("shop_id"),
        "quote_id": local.get("quote_id"),
        "quote_amount_fen": quote.get("buyer_amount_fen") if quote else None,
        "payload": {
            key: payload.get(key) for key in ("showId", "seats", "ticketMode", "priceMode") if payload.get(key) is not None
        },
    }


class RecognitionService(Protocol):
    async def recognize(
        self,
        image: bytes,
        content_type: str,
        buyer_message: str = "",
        *,
        prior_recognitions: list[MovieImageInfo] | None = None,
    ) -> MovieImageInfo: ...


class ChatReplyService(Protocol):
    async def reply(
        self,
        text: str,
        conversation_id: str,
        runtime_context: Mapping[str, object] | None = None,
    ) -> str: ...


async def _invoke_chat_reply(
    service: ChatReplyService,
    text: str,
    conversation_id: str,
    *,
    runtime_context: Mapping[str, object] | None = None,
) -> str:
    """Call current and legacy injected chat services safely.

    Older integrations implement the original two-argument protocol.  Keep
    those integrations usable while allowing the built-in Agent service to
    receive simulation context and trace hooks.
    """
    if runtime_context is None:
        return await service.reply(text, conversation_id)
    try:
        parameters = inspect.signature(service.reply).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_context = (
        "runtime_context" in parameters
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    )
    if accepts_context:
        return await service.reply(text, conversation_id, runtime_context=runtime_context)
    return await service.reply(text, conversation_id)


class QuoteService(Protocol):
    async def quote(self, recognition: MovieImageInfo) -> RealQuote: ...


async def _resolve_wanda_seat_target(
    recognition: MovieImageInfo,
    *,
    route_resolver: CinemaRouteResolver | None = None,
) -> tuple[MovieImageInfo, str | None]:
    """Resolve the Wanda namespace before a W+ seat-map read.

    ``MovieImageInfo.cinema_id`` normally comes from Liangpiao recognition,
    while ``WandaDirectQuoteService`` requires the local Wanda catalog ID.
    Never trust a model-provided ``cinema_id`` as a Wanda ID; use the canonical
    cinema route resolver to pair the provider identity with the local catalog.
    """
    if route_resolver is None:
        return recognition, None
    route = await route_resolver.resolve(recognition)
    if route.route != "WANDA_SELF":
        return route.recognition, None
    return route.recognition, route.wanda_cinema_id


class ConversationRecognitionStore:
    """Bounded, expiring in-memory context; images and Base64 are never stored."""

    def __init__(
        self,
        *,
        max_conversations: int = 1000,
        max_images_per_conversation: int = 3,
        ttl_seconds: float = 24 * 60 * 60,
        policy_provider: Callable[[], object] | None = None,
    ) -> None:
        self._max_conversations = max_conversations
        self._max_images = max_images_per_conversation
        self._ttl_seconds = ttl_seconds
        self._policy_provider = policy_provider
        self._items: dict[str, tuple[float, list[MovieImageInfo]]] = {}
        self._lock = RLock()

    def recent(self, conversation_id: str) -> list[MovieImageInfo]:
        with self._lock:
            now = monotonic()
            entry = self._items.get(conversation_id)
            ttl_seconds = self._ttl_seconds
            if self._policy_provider is not None:
                ttl_seconds = float(getattr(self._policy_provider(), "ttl_seconds", ttl_seconds))
            if entry is None or now - entry[0] >= ttl_seconds:
                self._items.pop(conversation_id, None)
                return []
            return list(entry[1])

    def add(self, conversation_id: str, recognition: MovieImageInfo) -> None:
        with self._lock:
            now = monotonic()
            existing = self.recent(conversation_id)
            self._items[conversation_id] = (now, [*existing, recognition][-self._max_images:])
            if len(self._items) > self._max_conversations:
                oldest = min(self._items, key=lambda key: self._items[key][0])
                self._items.pop(oldest, None)


class FixedWindowRateLimiter:
    """Small process-local guard for the expensive model endpoint."""

    def __init__(self, *, limit: int = 20, window_seconds: float = 60) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, client_id: str) -> bool:
        now = monotonic()
        started_at, count = self._windows.get(client_id, (now, 0))
        if now - started_at >= self._window_seconds:
            started_at, count = now, 0
        count += 1
        self._windows[client_id] = (started_at, count)
        if len(self._windows) > 10_000:
            self._windows = {
                key: value
                for key, value in self._windows.items()
                if now - value[0] < self._window_seconds
            }
        return count <= self._limit


def create_app(
    service: RecognitionService | None = None,
    *,
    rate_limiter: FixedWindowRateLimiter | None = None,
    settings_store: PersistentSettingsStore | None = None,
    diagnostics_store: DiagnosticsStore | None = None,
    model_catalog_service: ModelCatalogService | None = None,
    chat_reply_service: ChatReplyService | None = None,
    conversation_store: ConversationRecognitionStore | None = None,
    quote_service: QuoteService | None = None,
    pricing_rules_store: PricingRulesStore | None = None,
    plugin_automation: RulesFirstDecisionEngine | None = None,
    shop_automation_store: ShopAutomationStore | None = None,
    reply_template_store: ReplyTemplateStore | None = None,
    conversation_policy_store: ConversationPolicyStore | None = None,
    quote_record_store: QuoteRecordStore | None = None,
    reminder_store: ReminderStore | None = None,
    knowledge_store: KnowledgeStore | None = None,
    keyword_image_store: KeywordImageStore | None = None,
    pending_cinema_candidate_store: PendingCinemaCandidateStore | None = None,
    recognition_snapshot_store: RecognitionSnapshotStore | None = None,
    transaction_state_store: TransactionStateStore | SqliteTransactionStateStore | None = None,
    rules_first_store: RulesFirstStore | None = None,
    rules_first_runtime: RulesFirstRuntime | None = None,
    liangpiao_client: LiangpiaoClient | None = None,
    selected_seat_quote_service: SelectedSeatQuoteService | None = None,
    liangpiao_order_service: LiangpiaoOrderService | None = None,
    liangpiao_callback_handler: LiangpiaoCallbackHandler | None = None,
) -> FastAPI:
    settings_path = Path(os.getenv("WANDA_VISION_SETTINGS_PATH", "data/vision-settings.json"))
    pricing_path = Path(os.getenv("WANDA_PRICING_RULES_PATH", "data/pricing-rules.json"))
    shops_path = Path(os.getenv("WANDA_SHOP_AUTOMATION_PATH", "data/shop-automation.json"))
    templates_path = Path(os.getenv("WANDA_REPLY_TEMPLATES_PATH", "data/reply-templates.json"))
    conversation_policy_path = Path(os.getenv("WANDA_CONVERSATION_POLICY_PATH", "data/conversation-policy.json"))
    quote_records_path = Path(os.getenv("WANDA_QUOTE_RECORDS_PATH", "data/quote-records.json"))
    reminders_path = Path(os.getenv("WANDA_REMINDERS_PATH", "data/reminders.json"))
    knowledge_path = Path(os.getenv("WANDA_KNOWLEDGE_BASE_PATH", "data/knowledge-base.json"))
    keyword_images_path = Path(os.getenv("WANDA_KEYWORD_IMAGES_PATH", "data/keyword-images"))
    pending_candidates_path = Path(
        os.getenv("WANDA_PENDING_CANDIDATES_PATH", "data/pending-recognition-candidates.json")
    )
    rules_database_path = Path(os.getenv("WANDA_RULES_DB_PATH", "data/rules-first.sqlite3"))
    recognition_snapshots_path = Path(
        os.getenv("WANDA_RECOGNITION_SNAPSHOTS_PATH", "data/recognition-snapshots.sqlite3")
    )
    persistent_settings = settings_store or PersistentSettingsStore(settings_path)
    persistent_pricing_rules = pricing_rules_store or PricingRulesStore(pricing_path)
    persistent_shop_automation = shop_automation_store or ShopAutomationStore(shops_path)
    persistent_reply_templates = reply_template_store or ReplyTemplateStore(templates_path)
    persistent_conversation_policy = conversation_policy_store or ConversationPolicyStore(conversation_policy_path)
    persistent_quote_records = quote_record_store or QuoteRecordStore(quote_records_path)
    persistent_reminders = reminder_store or ReminderStore(reminders_path)
    persistent_knowledge = knowledge_store or KnowledgeStore(knowledge_path)
    persistent_keyword_images = keyword_image_store or KeywordImageStore(keyword_images_path)
    persistent_pending_candidates = (
        pending_cinema_candidate_store
        or PendingCinemaCandidateStore(pending_candidates_path)
    )
    persistent_recognition_snapshots = (
        recognition_snapshot_store
        or RecognitionSnapshotStore(recognition_snapshots_path)
    )
    persistent_rules_store = rules_first_store or RulesFirstStore(rules_database_path)
    persistent_transaction_states = transaction_state_store or SqliteTransactionStateStore(rules_database_path)
    runtime_settings = persistent_settings.current()
    configured_liangpiao_client = liangpiao_client
    if configured_liangpiao_client is None and (
        runtime_settings.liangpiao_selected_seat_quote_enabled
        or runtime_settings.liangpiao_order_create_enabled
        or runtime_settings.liangpiao_callback_enabled
    ) and runtime_settings.liangpiao_app_key and runtime_settings.liangpiao_app_secret:
        configured_liangpiao_client = LiangpiaoClient(runtime_settings)
    configured_quote_service = selected_seat_quote_service
    if configured_quote_service is None and configured_liangpiao_client is not None:
        configured_quote_service = SelectedSeatQuoteService(
            configured_liangpiao_client, quote_store=persistent_rules_store,
            pricing_rules=persistent_pricing_rules.current,
        )
    # Liangpiao order creation is intentionally not wired into the Backend
    # request path. The future implementation must emit an Outbox command for
    # the Node executor instead of constructing a provider-writing service here.
    async def create_fixed_switch_quote(payload: Mapping[str, object]) -> Mapping[str, object]:
        if configured_quote_service is None:
            raise RuntimeError("liangpiao_fixed_quote_unavailable")
        request = SelectedSeatQuoteRequest.model_validate(payload)
        result = await configured_quote_service.quote(request)
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    configured_callback_handler = liangpiao_callback_handler
    if configured_callback_handler is None and runtime_settings.liangpiao_callback_enabled and runtime_settings.liangpiao_app_secret:
        configured_callback_handler = LiangpiaoCallbackHandler(
            CallbackVerifier(runtime_settings.liangpiao_app_secret, app_key=runtime_settings.liangpiao_app_key),
            state_store=persistent_transaction_states, mapping_store=persistent_rules_store,
            client=configured_liangpiao_client, enabled=True,
        )
    rule_state_coordinator = RuleStateCoordinator(
        persistent_transaction_states, quote_store=persistent_quote_records,
        reply_template_store=persistent_reply_templates,
        liangpiao_order_phone=runtime_settings.liangpiao_order_phone,
    )
    diagnostics = diagnostics_store or DiagnosticsStore()
    recognition_service = service or MovieImageRecognitionService(
        persistent_settings.current,
        diagnostics=diagnostics,
    )
    catalog_service = model_catalog_service or ModelCatalogService(diagnostics=diagnostics)
    ai_assist_enabled = os.getenv("WANDA_AI_ASSIST_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    authoritative_quote_service = quote_service or (
        WandaDirectQuoteService(
            persistent_settings.current,
            diagnostics=diagnostics,
            pricing_rules=persistent_pricing_rules.current,
            reply_templates=persistent_reply_templates.current,
        )
        if service is None
        else None
    )

    # The chat Agent may use these read-only query tools to complete
    # missing consultation fields and obtain authoritative facts.  Transaction
    # writes remain on RulesFirstDecisionEngine/RulesFirstRuntime so a model
    # cannot bypass ownership, quote, idempotency or state gates.
    agent_tool_schemas: list[dict[str, object]] = []
    simulation_tool_schemas: list[dict[str, object]] = []
    agent_tool_executor = None
    simulation_agent_tool_executor = None
    simulation_states: dict[str, dict[str, object]] = {}
    if configured_liangpiao_client is not None or authoritative_quote_service is not None:
        string_argument = {"type": "string", "minLength": 1, "maxLength": 200}
        positive_integer_argument = {"type": "integer", "minimum": 1}
        seat_item_argument = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "seat_number": string_argument,
                "seat_no": string_argument,
                "row_no": positive_integer_argument,
                "col_no": positive_integer_argument,
                "area_id": string_argument,
            },
        }
        cinema_query_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "cityCode": string_argument, "city_code": string_argument,
                "cityName": string_argument, "city_name": string_argument,
                "keyword": string_argument, "brandId": positive_integer_argument,
                "page": positive_integer_argument, "pageSize": positive_integer_argument,
                "page_size": positive_integer_argument,
            },
        }
        show_query_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "cinemaId": positive_integer_argument, "cinema_id": positive_integer_argument,
                "movieId": positive_integer_argument, "movie_id": positive_integer_argument,
                "showDate": string_argument, "show_date": string_argument,
                "date": string_argument, "page": positive_integer_argument,
                "pageSize": positive_integer_argument, "page_size": positive_integer_argument,
            },
        }
        show_detail_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "showId": string_argument, "show_id": string_argument,
                "cinemaId": positive_integer_argument, "cinema_id": positive_integer_argument,
            },
            "anyOf": [{"required": ["showId"]}, {"required": ["show_id"]}],
        }
        quote_query_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "showId": string_argument, "show_id": string_argument,
                "cinemaId": positive_integer_argument, "cinema_id": positive_integer_argument,
                "cinema_name": string_argument,
                "movieId": positive_integer_argument, "movie_id": positive_integer_argument,
                "movie_name": string_argument,
                "city": string_argument, "city_code": string_argument,
                "date": string_argument, "date_text": string_argument,
                "showtime_start": string_argument, "showtime_end": string_argument,
                "hall_name": string_argument, "language": string_argument,
                "format": string_argument, "recognition_id": string_argument,
                "match_level": string_argument,
                "snapshot_id": string_argument, "target_id": string_argument,
                "snapshot_revision": positive_integer_argument,
                "ticketMode": string_argument, "ticket_mode": string_argument,
                "priceMode": string_argument, "price_mode": string_argument,
                "seatNos": {"type": "array", "items": string_argument, "minItems": 1},
                "seat_nos": {"type": "array", "items": string_argument, "minItems": 1},
                "seats": {"type": "array", "items": seat_item_argument, "minItems": 1},
                "selected_seats": {"type": "array", "items": seat_item_argument, "minItems": 1},
            },
        }
        order_detail_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "orderNo": string_argument,
            },
            "required": ["orderNo"],
        }
        current_order_state_parameters = {
            "type": "object", "additionalProperties": False,
            "properties": {},
        }
        agent_tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": "recognize_screenshot",
                    "description": "一次识别当前消息中的1至3张图片，合并影院、影片、日期、场次和座位字段；冲突字段必须追问，结果不是最终报价。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "image_url": {"type": "string"},
                            "image_urls": {
                                "type": "array", "minItems": 1, "maxItems": 3,
                                "uniqueItems": True, "items": {"type": "string"},
                            },
                            "buyer_message": {"type": "string"},
                        },
                        "oneOf": [
                            {"required": ["image_url"]},
                            {"required": ["image_urls"]},
                        ],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cinema.list",
                    "description": "查询良票影院候选，不创建订单。",
                    "parameters": cinema_query_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_cinema",
                    "description": "仅根据当前买家明确回复，从已持久化候选影院中选择一项；不得替买家猜测。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "snapshot_id", "snapshot_revision", "target_id",
                            "cinema_id", "buyer_message",
                        ],
                        "properties": {
                            "snapshot_id": string_argument,
                            "snapshot_revision": positive_integer_argument,
                            "target_id": string_argument,
                            "cinema_id": {"type": "integer"},
                            "buyer_message": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_image_conflict",
                    "description": "仅根据当前买家的明确文字澄清已记录的非金额截图冲突；只能更新返回的冲突字段。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "snapshot_id", "snapshot_revision", "target_id",
                            "buyer_message", "field_updates",
                        ],
                        "properties": {
                            "snapshot_id": string_argument,
                            "snapshot_revision": positive_integer_argument,
                            "target_id": string_argument,
                            "buyer_message": {"type": "string"},
                            "field_updates": {
                                "type": "object", "additionalProperties": False,
                                "properties": {
                                    "city": {"type": "string"},
                                    "cinema_name": {"type": "string"},
                                    "movie_name": {"type": "string"},
                                    "date_text": {"type": "string"},
                                    "showtime_start": {"type": "string"},
                                    "hall_name": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_movie",
                    "description": "仅根据当前买家的明确回复，从当前识别快照的影片候选中选择一项，并通过良票官方确认接口收敛；不得替买家猜测。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "snapshot_id", "snapshot_revision", "target_id",
                            "movie_id", "buyer_message",
                        ],
                        "properties": {
                            "snapshot_id": string_argument,
                            "snapshot_revision": positive_integer_argument,
                            "target_id": string_argument,
                            "movie_id": {"type": "integer", "minimum": 1},
                            "buyer_message": {"type": "string", "minLength": 1, "maxLength": 2000},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "show.list",
                    "description": "查询影院影片场次及可用报价模式，不创建订单。",
                    "parameters": show_query_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_showtime",
                    "description": "仅根据当前买家的明确回复，从已持久化权威场次候选中选择一项；不得替买家猜测。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "snapshot_id", "snapshot_revision", "target_id",
                            "show_id", "buyer_message",
                        ],
                        "properties": {
                            "snapshot_id": string_argument,
                            "snapshot_revision": positive_integer_argument,
                            "target_id": string_argument,
                            "show_id": {"type": "string"},
                            "buyer_message": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "show.detail",
                    "description": "强制刷新并查询单个场次详情，不创建订单。",
                    "parameters": show_detail_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "seat.list",
                    "description": "查询万达官方实时座位图，不锁座。没有明确已选座位时，按买家提出的排数或相对位置偏好解析可处理的W+座位；不能用截图颜色推断库存。解析‘前面一排/后面一排’时必须基于当前参考位置。不要向买家解释工具或座位选择机制。",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "showId": string_argument, "show_id": string_argument,
                            "cinemaId": positive_integer_argument, "cinema_id": positive_integer_argument,
                            "movieId": positive_integer_argument, "movie_id": positive_integer_argument,
                            "date": string_argument, "show_date": string_argument,
                            "showtime_start": string_argument, "hall_name": string_argument,
                            "row_no": positive_integer_argument, "seat_preference": string_argument,
                        },
                        "required": ["show_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "quote.preflight_current",
                    "description": "对明确的场次和座位执行良票精确预报价，不创建订单、不冻结资金。",
                    "parameters": quote_query_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "order.detail",
                    "description": "查询良票订单权威状态和取票信息，不修改订单。",
                    "parameters": order_detail_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_quote",
                    "description": "使用明确的场次和座位执行权威预报价；不创建订单、不修改金额。",
                    "parameters": quote_query_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_order_state",
                    "description": "读取良票订单权威状态；不修改订单。",
                    "parameters": current_order_state_parameters,
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reprice_seats",
                    "description": "使用买家明确纠正的标准座位重新执行权威预报价；不创建订单。",
                    "parameters": quote_query_parameters,
                },
            },
        ]
        alias_tools = {
            "get_authoritative_quote": "使用已确认的场次和座位获取权威报价。",
        }
        agent_tool_schemas.extend({
            "type": "function",
            "function": {
                "name": name, "description": description,
                "parameters": quote_query_parameters,
            },
        } for name, description in alias_tools.items())
        # Preserve the complete action space for the isolated workbench
        # simulator; the production action space is narrowed immediately below.
        simulation_tool_schemas = list(agent_tool_schemas)
        # Legacy aliases remain executable in the dispatcher, but only one
        # canonical quote tool is advertised to fresh Agent turns.
        agent_tool_schemas = [
            schema for schema in agent_tool_schemas
            if isinstance(schema.get("function"), Mapping)
            and schema["function"].get("name") in {
                "recognize_screenshot", "cinema.list", "resolve_cinema",
                "resolve_image_conflict", "resolve_movie", "show.list",
                "resolve_showtime", "show.detail", "seat.list",
                "quote.preflight_current", "get_order_state",
            }
        ]
        agent_tool_schemas.append({
            "type": "function",
            "function": {
                "name": "recognition.resolve_seats",
                "description": "根据买家当前消息明确的座位纠正，更新报价目标；不重复识图。",
                "parameters": quote_query_parameters,
            },
        })
        simulation_tool_schemas.append(agent_tool_schemas[-1])

        # The plugin workbench has a separate simulator action space. It sees
        # every canonical business tool so that an operator can test the full
        # conversation orchestration. Simulation write actions are intercepted
        # below and never reach a platform order/payment/fulfillment endpoint.
        simulation_write_parameters = {
            "type": "object", "additionalProperties": True,
        }
        for simulation_tool_name, simulation_description in {
            "create_order": "模拟创建订单，不产生真实订单。",
            "create_liangpiao_order": "模拟创建良票订单，不产生真实订单。",
            "order.create": "模拟创建订单，不产生真实订单。",
            "change_price": "模拟改价，不修改真实订单。",
            "change_order_price": "模拟改价，不修改真实订单。",
            "order.change_price": "模拟改价，不修改真实订单。",
            "cancel_order": "模拟取消订单，不修改真实订单。",
            "order.cancel": "模拟取消订单，不修改真实订单。",
            "urge_order": "模拟催单，不发送真实催单。",
            "order.urge": "模拟催单，不发送真实催单。",
            "switch_fixed": "模拟切换一口价，不修改真实订单。",
            "quote.switch_fixed": "模拟切换一口价，不修改真实订单。",
            "submit_fulfillment": "模拟提交出票履约，不触发真实出票。",
            "send_ticket": "模拟发送电影票，不发送真实票码。",
            "refund_or_intercept": "模拟退款或拦截，不触发真实退款。",
            "pay_order": "模拟买家付款，不触发真实支付。",
        }.items():
            simulation_tool_schemas.append({
                "type": "function",
                "function": {
                    "name": simulation_tool_name,
                    "description": simulation_description,
                    "parameters": simulation_write_parameters,
                },
            })

        tool_methods = {
            "cinema.list": "cinema_list",
            "show.list": "show_list",
            "show.detail": "show_detail",
            "seat.list": "seat_list",
            "order.preflight": "order_preflight",
            "quote.preflight_current": "order_preflight",
            "order.detail": "order_detail",
            "get_quote": "order_preflight",
            "get_authoritative_quote": "order_preflight",
            "get_order_state": "order_detail",
            "reprice_seats": "order_preflight",
            "recognition.resolve_seats": "order_preflight",
        }

        async def execute_agent_tool(name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
            tool_name = str(name).strip()
            if not isinstance(arguments, Mapping) or len(arguments) > 80:
                return {"ok": False, "error": "tool_arguments_invalid"}
            safe_arguments = {
                str(key): value for key, value in arguments.items()
                if isinstance(key, str) and len(key) <= 80
            }

            if tool_name == "recognize_screenshot":
                # Keep the process-wide executor in lockstep with the
                # request-scoped plugin executor: both accept a bounded image
                # batch and return per-image status.  It remains a pure read
                # tool; quoting is a separate operation.
                return await _execute_agent_recognition_tool(
                    recognition_service, safe_arguments,
                )

            # W+ consultation is a seat-map read, not a Liangpiao quote. When
            # the buyer gives a row/preference without marked seats, query the
            # Wanda realtime map and return available member-seat labels for
            # the Agent to show. Do not turn this into a quote or a lock.
            if tool_name == "seat.list" and authoritative_quote_service is not None:
                list_wplus = getattr(authoritative_quote_service, "list_wplus_seats", None)
                if callable(list_wplus):
                    aliases = {
                        "showId": "show_id", "cinemaId": "cinema_id", "movieId": "movie_id",
                        "show_date": "date",
                    }
                    request_values = {
                        aliases.get(key, key): value for key, value in safe_arguments.items()
                        if key in MovieImageInfo.model_fields or key in aliases
                    }
                    request_values.pop("row_no", None)
                    request_values.pop("seat_preference", None)
                    try:
                        request = MovieImageInfo.model_validate(request_values)
                        request, mapped_wanda_cinema_id = await _resolve_wanda_seat_target(
                            request, route_resolver=cinema_route_resolver,
                        )
                        result = await list_wplus(
                            request,
                            row_no=safe_arguments.get("row_no"),
                            seat_preference=safe_arguments.get("seat_preference"),
                            wanda_cinema_id=mapped_wanda_cinema_id,
                        )
                    except Exception as error:  # noqa: BLE001 - read tool fails closed
                        LOGGER.warning("event=wanda_wplus_seat_list_failed error_type=%s", type(error).__name__)
                        return {"ok": False, "error": "provider_read_failed"}
                    return {"ok": True, "cinema_id_namespace": "wanda", **dict(result)} if isinstance(result, Mapping) else {
                        "ok": False, "error": "provider_response_invalid",
                    }

            # Local Wanda quotation is authoritative too. Provider-specific
            # preflight remains available for Liangpiao arguments that contain
            # IDs rather than MovieImageInfo fields.
            if tool_name in {
                "quote.preflight_current", "get_quote", "get_authoritative_quote",
                "reprice_seats", "recognition.resolve_seats",
            } and authoritative_quote_service is not None:
                try:
                    request = MovieImageInfo.model_validate({
                        key: value for key, value in safe_arguments.items()
                        if key in MovieImageInfo.model_fields
                    })
                    quote = await authoritative_quote_service.quote(request)
                except Exception:
                    quote = None
                if quote is not None:
                    return {"ok": True, "quote": quote.model_dump(mode="json")}

            method_name = tool_methods.get(tool_name)
            if not method_name or configured_liangpiao_client is None:
                return {"ok": False, "error": "tool_not_allowed"}
            method = getattr(configured_liangpiao_client, method_name, None)
            if not callable(method):
                return {"ok": False, "error": "tool_unavailable"}
            try:
                result = await method(**safe_arguments)
            except Exception as error:  # noqa: BLE001 - fail closed at tool boundary
                LOGGER.warning("event=agent_read_tool_failed name=%s error_type=%s", tool_name, type(error).__name__)
                return {"ok": False, "error": "provider_read_failed"}
            if isinstance(result, Mapping):
                technical = {
                    "raw_response", "trace_id", "request_id", "http_status",
                    "sign", "signature", "token", "access_token", "cookie",
                    "csrf", "csrf_token", "app_secret",
                }
                public_result = {
                    key: value for key, value in result.items()
                    if str(key).lower() not in technical
                }
                return {"ok": True, **public_result}
            return {"ok": False, "error": "provider_response_invalid"}

        simulation_write_tools = {
            "create_order", "create_liangpiao_order", "order.create", "change_price",
            "change_order_price", "order.change_price", "cancel_order", "order.cancel",
            "urge_order", "order.urge", "switch_fixed", "quote.switch_fixed",
            "submit_fulfillment", "send_ticket", "refund_or_intercept",
            "pay_order",
        }
        simulation_order_read_tools = {"order.detail", "get_order_state"}

        async def execute_simulation_agent_tool(
            name: str, arguments: Mapping[str, object], conversation_id: str | None = None,
        ) -> Mapping[str, object]:
            # Reads use the configured adapters so the workbench can exercise
            # real recognition/catalog/seat observations. Transactional tools
            # return a deterministic sandbox result and cannot mutate
            # production data, even if the model requests them.
            key = str(conversation_id if conversation_id is not None else "default").strip()[:128] or "default"
            state = simulation_states.setdefault(key, {
                "order_status": "none", "fulfillment_status": "pending",
                "price_mode": "LIMIT", "refund_status": "none", "order_id": None,
                "order_seq": 0,
            })
            # A workbench simulation must never read a real order either.  The
            # production executor intentionally requires request-scoped buyer/
            # chat/order identity; the isolated simulator has no such identity,
            # so return only its own deterministic state snapshot.
            if name in simulation_order_read_tools:
                return {
                    "ok": True, "simulation": True, "status": "simulated",
                    "tool": name, "order": {
                        "orderId": state.get("order_id"),
                        "orderStatus": state.get("order_status"),
                        "fulfillmentStatus": state.get("fulfillment_status"),
                        "priceMode": state.get("price_mode"),
                        "refundStatus": state.get("refund_status"),
                    },
                    "summary": "simulation:order_state",
                }
            if name in simulation_write_tools:
                order_status = str(state.get("order_status") or "none")
                blocked_reason: str | None = None
                next_state = order_status
                if name in {"create_order", "create_liangpiao_order", "order.create"}:
                    if order_status not in {"none", "cancelled", "refunded"}: blocked_reason = "order_already_exists"
                    else:
                        state["order_seq"] = int(state.get("order_seq") or 0) + 1
                        state["order_id"] = (
                            f"sim-{hashlib.sha256(key.encode()).hexdigest()[:12]}-"
                            f"{state['order_seq']}"
                        )
                        state["order_status"] = "pending_payment"
                        state["fulfillment_status"] = "pending"
                        state["price_mode"] = "LIMIT"
                        state["refund_status"] = "none"
                        next_state = "awaiting_payment"
                elif name in {"change_price", "change_order_price", "order.change_price"}:
                    if order_status in {"none", "cancelled", "refunded"}: blocked_reason = "order_not_changeable"
                    else: state["order_status"] = "price_changed"; next_state = "awaiting_payment"
                elif name in {"cancel_order", "order.cancel"}:
                    if order_status in {"none", "cancelled", "refunded"}: blocked_reason = "order_not_cancellable"
                    else: state["order_status"] = "cancelled"; state["fulfillment_status"] = "cancelled"; next_state = "closed"
                elif name in {"urge_order", "order.urge"}:
                    if order_status in {"none", "cancelled", "refunded"}: blocked_reason = "order_missing"
                    else: state["order_status"] = "urged"; next_state = "awaiting_provider"
                elif name in {"switch_fixed", "quote.switch_fixed"}:
                    if order_status in {"cancelled", "refunded"}: blocked_reason = "order_closed"
                    else: state["price_mode"] = "FIXED"; next_state = "awaiting_payment"
                elif name == "pay_order":
                    if order_status not in {"pending_payment", "price_changed", "urged"}:
                        blocked_reason = "order_not_payable"
                    else:
                        state["order_status"] = "paid"; next_state = "paid_waiting_fulfillment"
                elif name == "submit_fulfillment":
                    if order_status != "paid": blocked_reason = "payment_required_before_fulfillment"
                    else: state["order_status"] = "ticketing"; state["fulfillment_status"] = "ticketing"; next_state = "ticketing"
                elif name == "send_ticket":
                    if state.get("fulfillment_status") != "ticketing": blocked_reason = "ticket_not_ready"
                    else: state["order_status"] = "fulfilled"; state["fulfillment_status"] = "sent"; next_state = "fulfilled"
                elif name == "refund_or_intercept":
                    if order_status in {"none", "refunded"}: blocked_reason = "order_not_refundable"
                    else: state["order_status"] = "refunded"; state["refund_status"] = "refunded"; next_state = "refunded"
                status = "blocked" if blocked_reason else "simulated"
                return {
                    "ok": blocked_reason is None, "simulation": True, "status": status, "tool": name,
                    "order_id": state.get("order_id"), "order_status": state.get("order_status"),
                    "fulfillment_status": state.get("fulfillment_status"), "price_mode": state.get("price_mode"),
                    "refund_status": state.get("refund_status"), "next_state": next_state,
                    "blocked_reason": blocked_reason, "summary": f"simulation:{name}:{status}", "next_actions": [],
                }
            return await execute_agent_tool(name, arguments)

        agent_tool_executor = execute_agent_tool
        simulation_agent_tool_executor = execute_simulation_agent_tool

    async def run_simulation_tool(
        name: str, arguments: Mapping[str, object], conversation_id: str,
    ) -> Mapping[str, object]:
        """Fail closed when a lightweight test app has no provider adapters."""
        if not callable(simulation_agent_tool_executor):
            return {
                "ok": False, "simulation": True, "status": "blocked",
                "tool": name, "blocked_reason": "simulation_tool_unavailable",
                "summary": "simulation:tool_unavailable",
            }
        return await simulation_agent_tool_executor(name, arguments, conversation_id)
    liangpiao_exact_quote_adapter = None
    cinema_route_resolver = None
    if configured_liangpiao_client is not None:
        # Cinema routing is also required by the W+ seat-map read. It must not
        # be gated by the separate Liangpiao exact-quote feature flag.
        local_wanda_matcher = getattr(authoritative_quote_service, "match_cached_cinema", None)
        cinema_route_resolver = CinemaRouteResolver(
            configured_liangpiao_client,
            local_wanda_matcher=local_wanda_matcher if callable(local_wanda_matcher) else None,
        )
        if runtime_settings.liangpiao_selected_seat_quote_enabled:
            liangpiao_exact_quote_adapter = LiangpiaoExactQuoteAdapter(
                configured_quote_service,
                price_mode_provider=lambda: persistent_pricing_rules.current().liangpiao_price_mode,
            )
    new_agent_harness = None
    if runtime_settings.new_agent_harness_enabled and runtime_settings.agent_harness_read_only:
        new_agent_harness = AgentHarness(
            model=OpenAICompatibleModel(persistent_settings.current),
            registry=build_read_only_registry(
                recognition_service=recognition_service,
                quote_service=authoritative_quote_service,
                provider_client=configured_liangpiao_client,
                quote_recorder=persistent_quote_records.save,
                route_resolver=cinema_route_resolver,
            ),
            max_rounds=8,
            max_tool_calls=8,
            deadline_seconds=min(runtime_settings.request_timeout_seconds, 60),
            per_tool_timeout_seconds=min(12, runtime_settings.request_timeout_seconds),
        )
    ai_chat_service = chat_reply_service or (
        CustomerServiceChatService(
            persistent_settings.current,
            diagnostics=diagnostics,
            conversation_policy_provider=persistent_conversation_policy.current,
            knowledge_provider=persistent_knowledge.active_for_prompt,
            tool_executor=agent_tool_executor,
            tool_schemas=agent_tool_schemas,
            simulation_tool_schemas=simulation_tool_schemas,
            tool_call_recorder=persistent_rules_store.record_agent_tool_call,
            agent_harness=new_agent_harness,
            new_agent_harness_enabled=runtime_settings.new_agent_harness_enabled,
        )
        if service is None and ai_assist_enabled
        else None
    )
    recognition_context = conversation_store or ConversationRecognitionStore(
        policy_provider=persistent_conversation_policy.current,
    )
    automated_plugin = plugin_automation or (
        RulesFirstDecisionEngine(
            recognition_service,
            authoritative_quote_service,
            chat_service=ai_chat_service,
            shop_store=persistent_shop_automation,
            automation_mode_provider=persistent_rules_store.resolve_automation_mode,
            template_provider=persistent_reply_templates.current,
            conversation_policy_provider=persistent_conversation_policy.current,
            quote_recorder=persistent_quote_records.save,
            quote_finder=lambda confirmed=False, **context: (
                persistent_quote_records.find_confirmed(**context)
                if confirmed else persistent_quote_records.find_recent(**context)
            ),
            quote_confirmer=persistent_quote_records.confirm_latest,
            quote_binder=persistent_quote_records.bind_order,
            quote_order_confirmer=persistent_quote_records.confirm_for_order,
            cinema_route_resolver=cinema_route_resolver,
            liangpiao_exact_quote_adapter=liangpiao_exact_quote_adapter,
            transaction_state_store=persistent_transaction_states,
            liangpiao_order_finder=persistent_rules_store.find_liangpiao_order,
            liangpiao_quote_finder=persistent_rules_store.get_selected_seat_quote,
            liangpiao_fixed_quote_creator=create_fixed_switch_quote if configured_quote_service is not None else None,
            liangpiao_order_phone=runtime_settings.liangpiao_order_phone,
            ai_assist_enabled=ai_assist_enabled,
            pending_cinema_candidate_store=persistent_pending_candidates,
            recognition_snapshot_store=persistent_recognition_snapshots,
            agent_harness=new_agent_harness,
            new_agent_harness_enabled=runtime_settings.new_agent_harness_enabled,
        )
        if service is None and authoritative_quote_service is not None
        else None
    )
    durable_runtime = rules_first_runtime or (
        RulesFirstRuntime(
            persistent_rules_store, automated_plugin, rule_state_coordinator,
            persistent_transaction_states,
            max_concurrent_events=runtime_settings.rules_first_max_concurrent_events,
            event_preprocessor=lambda body: plan_shipped_order_reminders(
                body,
                quote_store=persistent_quote_records,
                reminder_store=persistent_reminders,
                reply_templates=persistent_reply_templates.current(),
            ),
        )
        if automated_plugin is not None else None
    )
    limiter = rate_limiter or FixedWindowRateLimiter(
        limit=int(os.getenv("RECOGNITION_RATE_LIMIT_PER_MINUTE", "20")),
    )
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if durable_runtime is not None:
            await durable_runtime.start()
        try:
            yield
        finally:
            if durable_runtime is not None:
                await durable_runtime.stop()
            close = getattr(authoritative_quote_service, "aclose", None)
            if callable(close):
                await close()
            close_recognition = getattr(recognition_service, "aclose", None)
            if callable(close_recognition):
                await close_recognition()
            close_automation = getattr(automated_plugin, "aclose", None)
            if callable(close_automation):
                await close_automation()
            close_liangpiao = getattr(configured_liangpiao_client, "aclose", None)
            if callable(close_liangpiao):
                await close_liangpiao()

    app = FastAPI(title="万达电影票 AI 识图", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def observe_and_secure_request(request: Request, call_next):
        request_id = uuid4().hex[:16]
        context_token = REQUEST_ID.set(request_id)
        started_at = perf_counter()
        status_code = 500
        LOGGER.info("event=request_started method=%s path=%s", request.method, request.url.path)
        try:
            response = await call_next(request)
            status_code = response.status_code
            duration_ms = round((perf_counter() - started_at) * 1000, 1)
            response.headers["X-Request-ID"] = request_id
            response.headers["Server-Timing"] = f"total;dur={duration_ms}"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; img-src 'self' blob: data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
            )
            return response
        except Exception as error:
            LOGGER.error("event=request_unhandled error_type=%s", type(error).__name__)
            raise
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 1)
            LOGGER.info(
                "event=request_completed method=%s path=%s status=%d duration_ms=%.1f",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )
            if request.url.path != "/api/diagnostics/recent":
                diagnostics.add(
                    "request_completed",
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=duration_ms,
                )
            REQUEST_ID.reset(context_token)

    @app.exception_handler(RecognitionError)
    async def recognition_error_handler(_: Request, error: RecognitionError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"ok": False, "error": {"code": error.code, "message": error.message}},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": APP_NAME}

    @app.post("/api/liangpiao/selected-seat/quote")
    async def liangpiao_selected_seat_quote(body: dict[str, object]) -> dict[str, object]:
        if not runtime_settings.liangpiao_selected_seat_quote_enabled or configured_quote_service is None:
            raise HTTPException(status_code=503, detail="liangpiao_selected_seat_quote_disabled")
        try:
            request_body = dict(body)
            request_body.setdefault("price_mode", persistent_pricing_rules.current().liangpiao_price_mode)
            result = await configured_quote_service.quote(SelectedSeatQuoteRequest.model_validate(request_body))
        except QuoteServiceError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return result.model_dump(mode="json")

    @app.post("/api/liangpiao/order/create")
    async def liangpiao_order_create(
        body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
        x_yumaiduo_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        if not x_yumaiduo_tenant_id or str(body.get("tenant_id") or "").strip() != x_yumaiduo_tenant_id.strip():
            raise HTTPException(status_code=403, detail="liangpiao_order_tenant_mismatch")
        # This endpoint is intentionally a hard stop. Backend must not own a
        # real Liangpiao provider write path; order creation will be reintroduced
        # only as an Outbox command executed by the platform runtime.
        raise HTTPException(status_code=410, detail="liangpiao_direct_order_api_disabled")

    @app.post("/api/liangpiao/callback")
    async def liangpiao_callback(
        request: Request,
        x_liangpiao_sign: str | None = Header(default=None),
        x_liangpiao_timestamp: str | None = Header(default=None),
        x_liangpiao_nonce: str | None = Header(default=None),
    ) -> dict[str, object]:
        if not runtime_settings.liangpiao_callback_enabled or configured_callback_handler is None:
            raise HTTPException(status_code=503, detail="liangpiao_callback_disabled")
        raw = await request.body()
        callback_record = persistent_rules_store.record_liangpiao_callback(
            raw, signature=x_liangpiao_sign or "", timestamp=x_liangpiao_timestamp or "", nonce=x_liangpiao_nonce or "",
        )
        try:
            result = await configured_callback_handler.handle(
                raw, signature=x_liangpiao_sign or "", timestamp=x_liangpiao_timestamp or "", nonce=x_liangpiao_nonce or "",
            )
        except CallbackError as error:
            persistent_rules_store.update_liangpiao_callback(
                int(callback_record["callback_id"]), verification_status="rejected", processing_status="failed",
                result_code=error.code, reason=error.message,
            )
            raise HTTPException(status_code=409, detail=error.code) from error
        linked = persistent_rules_store.find_liangpiao_order(
            out_order_no=str(result.get("out_order_no") or "") or None,
            provider_order_no=str(result.get("provider_order_no") or "") or None,
        )
        persistent_rules_store.update_liangpiao_callback(
            int(callback_record["callback_id"]), tenant_id=linked.get("tenant_id") if linked else None,
            verification_status="verified", processing_status=str(result.get("status") or "processed"),
            result_code=str(result.get("code") or ""),
        )
        plan = result.get("reply_plan") if isinstance(result.get("reply_plan"), Mapping) else None
        platform_actions = result.get("platform_actions") if isinstance(result.get("platform_actions"), list) else []
        if linked and linked.get("tenant_id") and (plan or platform_actions):
            commands = [dict(item) for item in platform_actions if isinstance(item, Mapping)]
            template_key = str(plan.get("template_key") or "") if plan else ""
            template = {
                "flow.fulfillment.liangpiao_ticketed": persistent_reply_templates.current().liangpiao_ticketed_template,
                "flow.fulfillment.liangpiao_failed": persistent_reply_templates.current().liangpiao_failed_template,
                "flow.fulfillment.liangpiao_fixed_failed": persistent_reply_templates.current().liangpiao_fixed_failed_template,
                "flow.fulfillment.liangpiao_limit_refund_pending": (
                    "很抱歉，特惠渠道未能完成出票（{失败原因}）。"
                    "我正在为您关闭原订单并按闲鱼平台流程退款，"
                    "确认关闭后再询问您是否切换一口价渠道。"
                ),
            }.get(template_key) if plan else None
            message = render_template(template, dict(plan.get("variables") or {})) if template and plan else ""
            session = {key: linked.get(key) for key in ("shop_id", "buyer_id", "chat_id")}
            if all(str(value or "").strip() for value in session.values()):
                callback_event = f"liangpiao-callback-{callback_record['callback_id']}-{result.get('state_revision', 0)}"
                if message:
                    commands.append({
                        "id": f"{callback_event}:reply", "type": "send_message", "text": message,
                        "dedupe_key": str(result.get("reply_dedupe_key") or callback_event),
                        "preserve_on_new_buyer_message": True, "rule_governed": True,
                        "safety_notice": True,
                    })
                if commands:
                    persistent_rules_store.append_system_commands(
                        tenant_id=str(linked["tenant_id"]), event_id=callback_event,
                        session={"accountUnb": session["shop_id"], "peerUnb": session["buyer_id"], "chatId": session["chat_id"]},
                        commands=commands, state_revision=int(result.get("state_revision") or 0),
                    )
                    result["reply_enqueued"] = bool(message)
                    result["platform_actions_enqueued"] = len(commands) - (1 if message else 0)
        return result

    @app.get("/api/plugin/liangpiao-callbacks")
    async def list_plugin_liangpiao_callbacks(
        x_wanda_tenant_id: str | None = Header(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        records = persistent_rules_store.list_liangpiao_callbacks(tenant_id, limit=limit)
        return {"records": records, "count": len(records)}

    def require_plugin_bridge(value: str | None) -> None:
        expected = os.getenv("WANDA_AI_V2_BRIDGE_KEY", "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail="v4_plugin_bridge_not_configured")
        if not value or not secrets.compare_digest(value, expected):
            raise HTTPException(status_code=401, detail="v4_plugin_bridge_unauthorized")

    @app.post("/api/wanda-ai-v2/plugin/shops/sync")
    async def plugin_sync_shops(
        body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        tenant_id = str(body.get("tenant_id") or "").strip()
        shops = body.get("shops")
        accepted = persistent_shop_automation.sync(tenant_id, shops if isinstance(shops, list) else [])
        return {"accepted": accepted}

    def require_panel_tenant(value: str | None) -> str:
        tenant_id = str(value or "").strip()
        if not tenant_id or len(tenant_id) > 160:
            raise HTTPException(status_code=401, detail="panel_tenant_required")
        return tenant_id

    def require_panel_operator(value: str | None) -> str:
        operator_id = str(value or "").strip()
        if not operator_id or len(operator_id) > 200:
            raise HTTPException(status_code=401, detail="panel_operator_required")
        return operator_id

    @app.post("/api/settings/reply-keyword-images")
    async def upload_reply_keyword_image(
        image: UploadFile = File(...),
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        content_type = str(image.content_type or "").split(";", 1)[0].strip().lower()
        data = await image.read(MAX_KEYWORD_IMAGE_BYTES + 1)
        try:
            saved = persistent_keyword_images.save(
                tenant_id, data, content_type, image.filename or "keyword-image",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info(
            "event=reply_keyword_image_saved tenant_id=%s asset_id=%s size=%d",
            tenant_id, saved["asset_id"], saved["size"],
        )
        return saved

    @app.get("/api/wanda-ai-v2/plugin/keyword-images/{asset_id}")
    async def plugin_keyword_image(
        asset_id: str,
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
        x_yumaiduo_tenant_id: str | None = Header(default=None),
    ) -> Response:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        tenant_id = require_panel_tenant(x_yumaiduo_tenant_id)
        try:
            asset = persistent_keyword_images.get(tenant_id, asset_id)
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="keyword_image_not_found") from None
        return Response(
            content=asset["data"],
            media_type=str(asset["content_type"]),
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Content-Type-Options": "nosniff",
                "X-Keyword-Image-Sha256": str(asset["sha256"]),
                "X-Keyword-Image-Filename": str(asset["filename"]),
            },
        )

    @app.get("/api/plugin/quote-records")
    async def list_plugin_quote_records(
        x_wanda_tenant_id: str | None = Header(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        records = persistent_quote_records.list(tenant_id, limit=limit)
        liangpiao_quotes = persistent_rules_store.list_selected_seat_quotes(tenant_id, limit=limit)
        buyer_by_quote_id = {
            str(record.get("provider_quote_id")): record
            for record in records
            if str(record.get("provider_quote_id") or "").strip()
        }
        liangpiao_records = [
            _liangpiao_quote_public(quote, buyer_by_quote_id.get(str(quote.get("quote_id"))))
            for quote in liangpiao_quotes
        ]
        return {
            "records": records,
            "count": len(records),
            "liangpiao_records": liangpiao_records,
            "liangpiao_count": len(liangpiao_records),
        }

    @app.put("/api/plugin/quote-records/{record_id}/selected-offer")
    async def select_plugin_quote_offer(
        record_id: str,
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        offer_id = str(body.get("offer_id") or "").strip()
        if not offer_id or len(offer_id) > 100:
            raise HTTPException(status_code=422, detail="offer_id_required")
        record = persistent_quote_records.get_record(tenant_id=tenant_id, record_id=record_id)
        if record is None:
            raise HTTPException(status_code=409, detail="quote_offer_unavailable")
        offers = record.get("offers") if isinstance(record.get("offers"), list) else []
        offer = next(
            (item for item in offers if isinstance(item, Mapping) and str(item.get("offer_id") or "") == offer_id),
            None,
        )
        authoritative_offer = None
        if len(offers) > 1:
            if (
                not isinstance(offer, Mapping)
                or record.get("quote_route") != "liangpiao_exact"
                or configured_quote_service is None
            ):
                raise HTTPException(status_code=409, detail="quote_offer_preflight_unavailable")
            seats = record.get("selected_seats")
            try:
                current_offer = record.get("selected_offer")
                current_generation = (
                    current_offer.get("generation")
                    if isinstance(current_offer, Mapping)
                    else record.get("quote_generation")
                )
                generation = int(current_generation or 0) + 1
                price_mode = str(offer.get("price_mode") or "").upper()
                if price_mode not in {"FIXED", "LIMIT"}:
                    raise ValueError("quote_offer_price_mode_invalid")
                request = SelectedSeatQuoteRequest.model_validate({
                    "tenant_id": tenant_id,
                    "conversation_id": ":".join(str(record.get(key) or "") for key in ("tenant_id", "shop_id", "chat_id")),
                    "cinema_id": record.get("cinema_id"), "show_id": record.get("show_id"),
                    "cinema_name": record.get("cinema"), "movie_name": record.get("movie"),
                    "show_date": record.get("quote_date"), "showtime_start": record.get("showtime_start"),
                    "hall_name": record.get("hall"), "seats": seats,
                    "ticket_mode": offer.get("ticket_mode") or record.get("ticket_mode") or "STANDARD",
                    "price_mode": price_mode, "generation": generation,
                    "trace_id": f"offer-select-{hashlib.sha256(f'{tenant_id}:{record_id}:{offer_id}'.encode()).hexdigest()[:40]}",
                })
                result = await configured_quote_service.quote(request)
                refreshed = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
                count = len(refreshed.get("seats") or [])
                total = refreshed.get("buyer_amount_fen")
                authoritative_offer = {
                    "price_mode": refreshed.get("price_mode"),
                    "ticket_mode": request.ticket_mode,
                    "total_quote_cents": total,
                    "unit_quote_cents": total // count if isinstance(total, int) and count > 0 and total % count == 0 else None,
                    "ticket_count": count,
                    "quote_expires_at": refreshed.get("expires_at"),
                    "quote_id": refreshed.get("quote_id"), "quote_hash": refreshed.get("quote_hash"),
                    "generation": refreshed.get("generation"),
                    "preflight_verified": refreshed.get("preflight_verified"),
                    "provider_amount_fen": refreshed.get("provider_amount_fen"),
                    "max_price_fen": refreshed.get("max_price_fen"),
                    "pricing_rule_version": refreshed.get("pricing_rule_version"),
                }
            except Exception as error:  # noqa: BLE001 - provider failures must fail closed
                LOGGER.warning(
                    "event=quote_offer_preflight_failed error_type=%s", type(error).__name__,
                )
                raise HTTPException(status_code=409, detail="quote_offer_preflight_failed") from None
        selected = persistent_quote_records.select_offer(
            tenant_id=tenant_id, record_id=record_id, offer_id=offer_id,
            selection_source="operator_panel", authoritative_offer=authoritative_offer,
        )
        if selected is None:
            raise HTTPException(status_code=409, detail="quote_offer_unavailable")
        return selected

    @app.get("/api/plugin/liangpiao-orders")
    async def list_plugin_liangpiao_orders(
        x_wanda_tenant_id: str | None = Header(default=None),
        status: str | None = Query(default=None, max_length=40),
        start_time: str | None = Query(default=None, max_length=40),
        end_time: str | None = Query(default=None, max_length=40),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        local_orders = persistent_rules_store.list_liangpiao_orders(tenant_id, limit=500)
        if configured_liangpiao_client is None:
            return {"orders": [], "count": 0, "total": 0, "page": page, "pageSize": page_size, "available": False}
        payload = {"page": page, "pageSize": page_size}
        if status:
            payload["status"] = status
        if start_time:
            payload["startTime"] = start_time
        if end_time:
            payload["endTime"] = end_time
        try:
            response = await configured_liangpiao_client.order_list(**payload)
        except (ProviderError, ValueError) as error:
            raise HTTPException(status_code=503, detail="liangpiao_order_list_unavailable") from error
        known = {
            str(item.get("provider_order_no") or "").strip(): item
            for item in local_orders if str(item.get("provider_order_no") or "").strip()
        }
        orders: list[dict[str, object]] = []
        for item in _liangpiao_items(response):
            provider_order_no = _liangpiao_order_no(item)
            local = known.get(provider_order_no)
            if not local:
                continue
            quote = persistent_rules_store.get_selected_seat_quote(str(local.get("quote_id") or ""))
            orders.append({**item, **_liangpiao_local_public(local, quote)})
        return {
            "orders": orders, "count": len(orders),
            "total": int(response.get("total") or len(orders)), "page": page,
            "pageSize": page_size, "available": True,
        }

    @app.get("/api/plugin/liangpiao-orders/{order_no}")
    async def get_plugin_liangpiao_order(
        order_no: str,
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        normalized_order_no = str(order_no or "").strip()
        local = persistent_rules_store.find_liangpiao_order(
            provider_order_no=normalized_order_no, tenant_id=tenant_id,
        )
        if local is None:
            raise HTTPException(status_code=404, detail="liangpiao_order_not_found")
        if configured_liangpiao_client is None:
            raise HTTPException(status_code=503, detail="liangpiao_order_detail_unavailable")
        try:
            detail = await configured_liangpiao_client.order_detail(orderNo=normalized_order_no)
        except (ProviderError, ValueError) as error:
            raise HTTPException(status_code=503, detail="liangpiao_order_detail_unavailable") from error
        quote = persistent_rules_store.get_selected_seat_quote(str(local.get("quote_id") or ""))
        technical = {"raw_response", "trace_id", "request_id", "http_status"}
        safe_detail = {key: value for key, value in detail.items() if key not in technical}
        return {"order": {**safe_detail, **_liangpiao_local_public(local, quote)}}

    @app.get("/api/plugin/shops")
    async def list_plugin_shops(
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        shops = persistent_shop_automation.list_shops(tenant_id)
        return {"shops": [
            {
                **shop,
                "automation_mode": persistent_rules_store.resolve_automation_mode(
                    tenant_id=tenant_id, shop_id=str(shop.get("shop_id") or ""),
                ),
            }
            for shop in shops
        ]}

    @app.get("/api/plugin/shops/{shop_id}/automation-mode")
    async def get_plugin_shop_automation_mode(
        shop_id: str,
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        return {
            "mode": persistent_rules_store.resolve_automation_mode(
                tenant_id=tenant_id, shop_id=shop_id,
            ),
            "scope": "shop",
        }

    @app.put("/api/plugin/shops/{shop_id}/automation-mode")
    async def update_plugin_shop_automation_mode(
        shop_id: str,
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        try:
            mode = normalize_automation_mode(body.get("mode"))
            saved = persistent_rules_store.set_shop_automation_mode(tenant_id, shop_id, mode)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="automation_mode_invalid") from None
        LOGGER.info("event=shop_automation_mode_saved scope=shop mode=%s", mode)
        return saved

    @app.get("/api/plugin/conversations/{buyer_id}/{chat_id}/automation-mode")
    async def get_plugin_conversation_automation_mode(
        buyer_id: str,
        chat_id: str,
        shop_id: str = Query(..., min_length=1, max_length=200),
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        try:
            override = persistent_rules_store.get_conversation_automation_mode(
                tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="automation_mode_identity_invalid") from None
        if override is not None:
            return override
        return {
            "mode": persistent_rules_store.resolve_automation_mode(
                tenant_id=tenant_id, shop_id=shop_id,
            ),
            "scope": "shop",
        }

    @app.put("/api/plugin/conversations/{buyer_id}/{chat_id}/automation-mode")
    async def update_plugin_conversation_automation_mode(
        buyer_id: str,
        chat_id: str,
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        shop_id = str(body.get("shop_id") or "").strip()
        try:
            mode = normalize_automation_mode(body.get("mode"))
            saved = persistent_rules_store.set_conversation_automation_mode(
                tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id,
                chat_id=chat_id, mode=mode,
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="automation_mode_invalid") from None
        LOGGER.info("event=conversation_automation_mode_saved scope=conversation mode=%s", mode)
        return saved

    @app.delete("/api/plugin/conversations/{buyer_id}/{chat_id}/automation-mode")
    async def clear_plugin_conversation_automation_mode(
        buyer_id: str,
        chat_id: str,
        shop_id: str = Query(..., min_length=1, max_length=200),
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        try:
            persistent_rules_store.clear_conversation_automation_mode(
                tenant_id=tenant_id, shop_id=shop_id, buyer_id=buyer_id, chat_id=chat_id,
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="automation_mode_identity_invalid") from None
        mode = persistent_rules_store.resolve_automation_mode(
            tenant_id=tenant_id, shop_id=shop_id,
        )
        LOGGER.info("event=conversation_automation_mode_cleared scope=conversation")
        return {"mode": mode, "scope": "shop"}

    @app.put("/api/plugin/shops/{shop_id}")
    async def update_plugin_shop(
        shop_id: str,
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            raise HTTPException(status_code=422, detail="enabled_boolean_required")
        try:
            shop = persistent_shop_automation.set_enabled(tenant_id, shop_id, enabled)
        except KeyError:
            raise HTTPException(status_code=404, detail="shop_not_found") from None
        LOGGER.info("event=shop_automation_saved enabled=%s", str(enabled).lower())
        return {"shop": shop}

    @app.post("/api/wanda-ai-v2/plugin/events/process", status_code=202)
    async def plugin_process_event(
        body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        accepted = (
            durable_runtime.accept(body)
            if durable_runtime is not None else persistent_rules_store.enqueue_event(body)
        )
        LOGGER.info(
            "event=rules_first_event_accepted event_id=%s duplicate=%s",
            accepted["event_id"], str(accepted["duplicate"]).lower(),
        )
        return accepted

    @app.post("/api/wanda-ai-v2/plugin/commands/claim")
    async def plugin_claim_commands(
        body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        if durable_runtime is None:
            return {"commands": []}
        if runtime_settings.agent_harness_read_only:
            held = durable_runtime.open_transaction_write_fuse()
            return {
                "commands": durable_runtime.claim_commands(limit=int(body.get("limit") or 10)),
                "write_fuse": "transaction_writes_closed",
                "manual_tasks_created": held,
            }
        if not runtime_settings.external_writes_enabled:
            held = durable_runtime.open_write_fuse()
            return {"commands": [], "write_fuse": "open", "manual_tasks_created": held}
        return {"commands": durable_runtime.claim_commands(limit=int(body.get("limit") or 10))}

    @app.post("/api/wanda-ai-v2/plugin/commands/{command_id}/result")
    async def plugin_command_result(
        command_id: str, body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        if durable_runtime is None:
            raise HTTPException(status_code=503, detail="rules_first_runtime_unavailable")
        result = body.get("result") if isinstance(body.get("result"), dict) else None
        lease_token = str(body.get("lease_token") or "").strip()
        if result is None or not lease_token:
            raise HTTPException(status_code=422, detail="command_result_invalid")
        command = persistent_rules_store.get_command(command_id)
        try:
            recorded = durable_runtime.record_command_result(
                command_id=command_id, lease_token=lease_token, result=result,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="command_missing") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if command is not None:
            action = command.get("action") if isinstance(command.get("action"), dict) else {}
            message_id = str(result.get("message_id") or "").strip()
            if (
                action.get("id") in {
                    f"{command['event_id']}:reply",
                    f"{command['event_id']}:repriced-quote",
                }
                and result.get("status") == "succeeded" and message_id
            ):
                delivered_at = datetime.now(timezone.utc)
                persistent_quote_records.mark_delivered(
                    tenant_id=command["tenant_id"], record_id=command["event_id"],
                    delivered_at=delivered_at, message_id=message_id,
                )
                persistent_quote_records.mark_event_quotes_delivered(
                    tenant_id=command["tenant_id"], event_id=command["event_id"],
                    delivered_at=delivered_at, message_id=message_id,
                )
        return recorded

    def enrich_panel_identities(
        tenant_id: str, records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        shop_names = {
            str(shop.get("shop_id") or ""): str(shop.get("shop_name") or "").strip()
            for shop in persistent_shop_automation.list_shops(tenant_id)
        }
        identity_cache: dict[tuple[str, str, str], dict[str, str | None]] = {}
        enriched: list[dict[str, object]] = []
        for source in records:
            record = dict(source)
            shop_id = str(record.get("shop_id") or "").strip()
            buyer_id = str(record.get("buyer_id") or "").strip()
            chat_id = str(record.get("chat_id") or "").strip()
            if shop_id:
                record["shop_name"] = shop_names.get(shop_id) or record.get("shop_name")
            if shop_id and buyer_id and chat_id and not record.get("buyer_name"):
                cache_key = (shop_id, buyer_id, chat_id)
                labels = identity_cache.get(cache_key)
                if labels is None:
                    labels = persistent_rules_store.identity_labels(
                        tenant_id, shop_id, buyer_id, chat_id,
                    )
                    identity_cache[cache_key] = labels
                record["buyer_name"] = labels.get("buyer_name")
            enriched.append(record)
        return enriched

    @app.get("/api/rules-first/event-audits")
    async def list_event_audits(
        x_wanda_tenant_id: str | None = Header(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        records = enrich_panel_identities(
            tenant_id, persistent_rules_store.list_event_audits(tenant_id, limit=limit),
        )
        return {"records": records, "count": len(records)}

    @app.get("/api/rules-first/agent-tool-calls")
    async def list_agent_tool_calls(
        x_wanda_tenant_id: str | None = Header(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        records = persistent_rules_store.list_agent_tool_calls(tenant_id, limit=limit)
        public_records = enrich_panel_identities(tenant_id, [
            {key: value for key, value in record.items() if key not in {"arguments", "result"}}
            for record in records
        ])
        return {"records": public_records, "count": len(public_records)}

    @app.get("/api/rules-first/manual-tasks")
    async def list_rules_manual_tasks(
        x_wanda_tenant_id: str | None = Header(default=None),
        x_wanda_operator_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        operator_id = str(x_wanda_operator_id or "").strip()
        tasks = persistent_rules_store.list_manual_tasks(tenant_id)
        public_tasks = []
        for task in tasks:
            public = {
                key: value for key, value in task.items()
                if key not in {"lease_token", "details"}
            }
            if operator_id and task.get("claimed_by") == operator_id:
                public["lease_token"] = task.get("lease_token")
            public_tasks.append(public)
        return {"tasks": enrich_panel_identities(tenant_id, public_tasks)}

    @app.post("/api/rules-first/manual-tasks/{task_id}/claim")
    async def claim_rules_manual_task(
        task_id: str, body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
        x_wanda_operator_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        operator_id = require_panel_operator(x_wanda_operator_id)
        expected_revision = int(body.get("expected_revision", -1))
        try:
            existing = next(
                (item for item in persistent_rules_store.list_manual_tasks(tenant_id) if item["task_id"] == task_id),
                None,
            )
            if existing is None:
                raise KeyError("manual_task_missing")
            if existing["reason"] == "fulfillment_required":
                raise ValueError("fulfillment_task_waits_for_official_shipment")
            task = persistent_rules_store.claim_manual_task(
                tenant_id, task_id, expected_revision=expected_revision,
                operator_id=operator_id,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="manual_task_missing") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return {"task": task}

    @app.post("/api/rules-first/manual-tasks/{task_id}/complete")
    async def complete_rules_manual_task(
        task_id: str, body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
        x_wanda_operator_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        operator_id = require_panel_operator(x_wanda_operator_id)
        expected_revision = int(body.get("expected_revision", -1))
        resolution = str(body.get("resolution") or "")
        try:
            existing = next(
                (item for item in persistent_rules_store.list_manual_tasks(tenant_id) if item["task_id"] == task_id),
                None,
            )
            if existing is None:
                raise KeyError("manual_task_missing")
            lease_token = str(body.get("lease_token") or "")
            if (
                existing.get("claimed_by") != operator_id
                or existing.get("status") != "claimed" or existing.get("lease_token") != lease_token
                or not existing.get("lease_until")
                or str(existing["lease_until"]) <= datetime.now(timezone.utc).isoformat()
            ):
                raise ValueError("manual_task_lease_conflict")
            current = persistent_transaction_states.get(
                tenant_id=tenant_id, shop_id=existing["shop_id"],
                buyer_id=existing["buyer_id"], chat_id=existing["chat_id"],
            )
            if resolution == "resume":
                if current is None or current.revision != expected_revision or current.flow_state != "MANUAL_HOLD":
                    raise ValueError("manual_resume_revision_conflict")
                persistent_transaction_states.transition(
                    tenant_id=tenant_id, shop_id=existing["shop_id"],
                    buyer_id=existing["buyer_id"], chat_id=existing["chat_id"],
                    expected_revision=expected_revision, event_id=f"manual-resume:{task_id}",
                    transition_code="manual_revision_resume", flow_state="ORDER_UNVERIFIED",
                    updates={"automation_control": "active", "order_status": "unverified"},
                )
            elif resolution in {"resolved", "cancel"}:
                # Closing a task while its transaction remains MANUAL_HOLD
                # would orphan the paused conversation. A trusted order/event
                # transition must update the state first; this endpoint only
                # closes the now-obsolete task.
                if current is None or current.flow_state == "MANUAL_HOLD":
                    raise ValueError("manual_resolution_state_still_on_hold")
                if current.revision < expected_revision:
                    raise ValueError("manual_task_revision_conflict")
                if current.revision != expected_revision:
                    persistent_rules_store.update_manual_task_revision(
                        tenant_id, task_id, previous_revision=expected_revision,
                        new_revision=current.revision,
                    )
                    expected_revision = current.revision
            task = persistent_rules_store.complete_manual_task(
                tenant_id, task_id, expected_revision=expected_revision,
                lease_token=lease_token, resolution=resolution,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="manual_task_missing") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return {"task": task}

    @app.post("/api/wanda-ai-v2/plugin/reminders/claim")
    async def plugin_claim_reminders(
        body: dict[str, object], x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        now = ReminderStore._datetime(body.get("now")) if body.get("now") else None
        tasks = persistent_reminders.claim_due(now=now, limit=int(body.get("limit") or 10))
        return {"tasks": tasks}

    @app.post("/api/wanda-ai-v2/plugin/reminders/{task_id}/complete")
    async def plugin_complete_reminder(
        task_id: str, body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        ok = persistent_reminders.complete(
            task_id, str(body.get("lease_token") or ""),
            body.get("result") if isinstance(body.get("result"), dict) else {},
        )
        if not ok:
            raise HTTPException(status_code=409, detail="reminder_lease_conflict")
        return {"ok": True}

    @app.post("/api/wanda-ai-v2/plugin/reminders/{task_id}/fail")
    async def plugin_fail_reminder(
        task_id: str, body: dict[str, object],
        x_wanda_ai_v2_bridge_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        require_plugin_bridge(x_wanda_ai_v2_bridge_key)
        ok = persistent_reminders.fail(
            task_id, str(body.get("lease_token") or ""), str(body.get("reason") or "reminder_failed"),
        )
        if not ok:
            raise HTTPException(status_code=409, detail="reminder_lease_conflict")
        return {"ok": True}

    @app.get("/api/diagnostics/recent")
    async def recent_diagnostics(
        limit: int = Query(default=20, ge=1, le=100),
        request_id: str | None = Query(default=None, max_length=80),
    ) -> dict[str, object]:
        return {"entries": diagnostics.recent(limit=limit, request_id=request_id)}

    @app.delete("/api/diagnostics/recent")
    async def clear_diagnostics() -> dict[str, object]:
        diagnostics.clear()
        return {"ok": True}

    @app.get("/api/settings/vision", response_model=VisionSettingsView)
    async def get_vision_settings() -> VisionSettingsView:
        return persistent_settings.view()

    @app.get("/api/settings/operations", response_model=PricingRulesView)
    async def get_operations_settings() -> PricingRulesView:
        return persistent_pricing_rules.view()

    @app.get("/api/settings/knowledge")
    async def get_knowledge() -> dict[str, object]:
        return persistent_knowledge.current().model_dump()

    @app.post("/api/settings/knowledge")
    async def create_knowledge(body: dict[str, object]) -> dict[str, object]:
        try:
            entry = persistent_knowledge.create(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=knowledge_entry_created entry_id=%s", entry.id)
        return entry.model_dump()

    @app.put("/api/settings/knowledge/{entry_id}")
    async def update_knowledge(entry_id: str, body: dict[str, object]) -> dict[str, object]:
        try:
            entry = persistent_knowledge.update(entry_id, body)
        except KeyError:
            raise HTTPException(status_code=404, detail="knowledge_not_found") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=knowledge_entry_updated entry_id=%s", entry.id)
        return entry.model_dump()

    @app.delete("/api/settings/knowledge/{entry_id}")
    async def delete_knowledge(entry_id: str) -> dict[str, object]:
        try:
            persistent_knowledge.delete(entry_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="knowledge_not_found") from None
        LOGGER.info("event=knowledge_entry_deleted entry_id=%s", entry_id)
        return {"ok": True}

    @app.put("/api/settings/knowledge")
    async def save_knowledge(body: dict[str, object]) -> dict[str, object]:
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=422, detail="knowledge_entries_required")
        try:
            saved = persistent_knowledge.save(entries)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=knowledge_saved revision=%d entries=%d", saved.revision, len(saved.entries))
        return saved.model_dump()

    @app.get("/api/settings/reminders")
    async def get_reminder_settings() -> dict[str, object]:
        return persistent_reminders.settings().model_dump()

    @app.put("/api/settings/reminders")
    async def save_reminder_settings(body: dict[str, object]) -> dict[str, object]:
        try:
            saved = persistent_reminders.save_settings(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=reminder_settings_saved enabled=%s revision=%d", saved.enabled, saved.revision)
        return saved.model_dump()

    @app.get("/api/plugin/reminders")
    async def list_plugin_reminders(
        x_wanda_tenant_id: str | None = Header(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        tasks = persistent_reminders.list(tenant_id, limit=limit)
        return {"tasks": tasks, "count": len(tasks)}

    @app.get("/api/settings/conversation-policy")
    async def get_conversation_policy() -> dict[str, object]:
        return persistent_conversation_policy.current().model_dump()

    @app.put("/api/settings/conversation-policy")
    async def save_conversation_policy(body: dict[str, object]) -> dict[str, object]:
        try:
            saved = persistent_conversation_policy.save(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=conversation_policy_saved revision=%d", saved.revision)
        return saved.model_dump()

    @app.get("/api/settings/reply-templates")
    async def get_reply_templates(
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        payload = persistent_reply_templates.current().model_dump()
        tenant_id = str(x_wanda_tenant_id or "").strip()
        for rule in payload.get("keyword_replies", []):
            if isinstance(rule, dict) and rule.get("image_tenant_id") != tenant_id:
                rule["image_asset_id"] = None
                rule["image_filename"] = None
                rule["image_tenant_id"] = None
        return payload

    @app.put("/api/settings/reply-templates")
    async def save_reply_templates(
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        rules = body.get("keyword_replies")
        if isinstance(rules, list):
            normalized_rules: list[object] = []
            for raw_rule in rules:
                if not isinstance(raw_rule, dict):
                    normalized_rules.append(raw_rule)
                    continue
                rule = dict(raw_rule)
                asset_id = str(rule.get("image_asset_id") or "").strip()
                if asset_id:
                    tenant_id = require_panel_tenant(x_wanda_tenant_id)
                    try:
                        metadata = persistent_keyword_images.get(tenant_id, asset_id)
                    except (KeyError, ValueError):
                        raise HTTPException(status_code=422, detail="keyword_image_not_found") from None
                    rule["image_asset_id"] = asset_id
                    rule["image_filename"] = metadata["filename"]
                    rule["image_tenant_id"] = tenant_id
                else:
                    rule["image_asset_id"] = None
                    rule["image_filename"] = None
                    rule["image_tenant_id"] = None
                normalized_rules.append(rule)
            body = {**body, "keyword_replies": normalized_rules}
        try:
            saved = persistent_reply_templates.save(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        LOGGER.info("event=reply_templates_saved revision=%d", saved.revision)
        return saved.model_dump()

    @app.put("/api/settings/operations", response_model=PricingRulesView)
    async def save_operations_settings(update: PricingRulesUpdate) -> PricingRulesView:
        saved = persistent_pricing_rules.save(update)
        LOGGER.info(
            "event=pricing_rules_saved enabled=%s revision=%d rule_version=%s",
            str(saved.enabled).lower(), saved.revision, saved.rule_version,
        )
        diagnostics.add(
            "pricing_rules_saved",
            enabled=saved.enabled,
            revision=saved.revision,
            rule_version=saved.rule_version,
        )
        return saved

    @app.post("/api/settings/vision/models", response_model=ModelCatalogResponse)
    async def list_provider_models(request: ModelCatalogRequest) -> ModelCatalogResponse:
        stored = persistent_settings.current()
        stored_key = stored.chat_api_key if request.provider == "chat" else stored.api_key
        api_key = request.api_key or stored_key
        if not api_key:
            raise RecognitionError(
                "model_catalog_key_required",
                "请先输入或保存中转站 API Key，再获取模型。",
                status_code=503,
            )
        return await catalog_service.list_models(request.base_url, api_key)

    @app.put("/api/settings/vision", response_model=VisionSettingsView)
    async def save_vision_settings(update: VisionSettingsUpdate) -> VisionSettingsView:
        saved = persistent_settings.save(update)
        LOGGER.info(
            "event=vision_settings_saved vision_model=%s chat_model=%s thinking=%s reasoning_effort=%s has_api_key=%s",
            saved.model,
            saved.chat_model,
            str(saved.enable_thinking).lower(),
            saved.reasoning_effort,
            str(saved.has_api_key).lower(),
        )
        return saved

    def enforce_rate_limit(request: Request) -> None:
        client_id = request.client.host if request.client else "unknown"
        if not limiter.allow(client_id):
            raise RecognitionError(
                "rate_limit_exceeded",
                "识图请求过多，请一分钟后再试。",
                status_code=429,
            )

    async def read_image(image: UploadFile) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := await image.read(1024 * 1024):
            size += len(chunk)
            if size > UPLOAD_READ_LIMIT:
                raise ImageValidationError(
                    "image_too_large",
                    "图片不能超过 20 MB。",
                    status_code=413,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def recognize_upload(
        image: UploadFile,
        buyer_message: str = "",
        prior_recognitions: list[MovieImageInfo] | None = None,
    ) -> MovieImageInfo:
        return await recognition_service.recognize(
            await read_image(image),
            image.content_type or "application/octet-stream",
            buyer_message,
            prior_recognitions=list(prior_recognitions or []),
        )

    @app.post("/api/movie-images/recognize", response_model=RecognitionResponse)
    async def recognize_movie_image(request: Request, image: UploadFile = File(...)) -> RecognitionResponse:
        enforce_rate_limit(request)
        return RecognitionResponse(data=await recognize_upload(image))

    @app.post("/api/ticket-images/recognize", response_model=RecognitionResponse)
    async def recognize_ticket_image(request: Request, payload: TicketImageRecognitionRequest) -> RecognitionResponse:
        """Recognize an issuing-system ticket image with the ticket-code contract."""
        enforce_rate_limit(request)
        return RecognitionResponse(data=await recognition_service.recognize_from_url(
            payload.image_url,
            city_name=payload.city_name,
            ticket_image=True,
        ))

    @app.post("/api/chat/image-messages", response_model=ChatMessageResponse)
    async def create_chat_image_message(
        request: Request,
        image: Annotated[UploadFile, File()],
        conversation_id: Annotated[str, Form(min_length=1, max_length=128)],
        message_text: Annotated[str, Form(max_length=2000)] = "",
        simulation: Annotated[bool, Form()] = False,
    ) -> ChatMessageResponse:
        enforce_rate_limit(request)
        normalized = ChatTextRequest(
            conversation_id=conversation_id,
            text=message_text or "[图片]",
        )
        recognition = await recognize_upload(
            image,
            message_text,
            recognition_context.recent(normalized.conversation_id),
        )
        complete_cinema = getattr(authoritative_quote_service, "complete_cinema", None)
        if callable(complete_cinema):
            try:
                recognition = await complete_cinema(recognition)
            except RecognitionError:
                pass
        recognition_context.add(normalized.conversation_id, recognition)
        quote: RealQuote | None = None
        quote_error: str | None = None
        new_harness_turn = bool(
            runtime_settings.new_agent_harness_enabled
            and ai_chat_service is not None
            and runtime_settings.chat_api_key
        )
        if authoritative_quote_service is not None and not new_harness_turn:
            try:
                quote = await authoritative_quote_service.quote(recognition)
            except RecognitionError as error:
                quote_error = error.message
                diagnostics.add("quote_unavailable", code=error.code, message=error.message)
                LOGGER.warning("event=quote_unavailable code=%s", error.code)
        remember_image_context = getattr(ai_chat_service, "remember_image_context", None)
        if callable(remember_image_context):
            remember_image_context(normalized.conversation_id, recognition, quote, quote_error)
        agent_trace: list[dict[str, object]] = []
        reply_text = build_recognition_reply(
            recognition,
            quote=quote,
            quote_error=quote_error,
            templates=persistent_reply_templates.current(),
        )
        message_type = "movie_recognition"
        # Workbench image turns enter the same Agent loop as text turns. The
        # recognition and quote above are authoritative facts supplied as
        # context; simulation writes remain sandboxed by the executor.
        if new_harness_turn and not simulation:
            harness_context = {
                "tenant_id": f"public:{normalized.conversation_id}",
                "shop_id": f"public:{normalized.conversation_id}",
                "buyer_id": f"public:{normalized.conversation_id}",
                "chat_id": normalized.conversation_id,
                "current_event": {
                    "message_id": uuid4().hex,
                    "content": message_text or "请根据这张截图查询价格",
                    "recognition": _public_recognition_payload(recognition),
                },
                "recognition_facts": _public_recognition_payload(recognition),
            }
            reply_text = await _invoke_chat_reply(
                ai_chat_service,
                message_text or "请根据这张截图查询价格",
                normalized.conversation_id,
                runtime_context=harness_context,
            )
            message_type = "ai_reply"
        elif simulation and ai_chat_service is not None and persistent_settings.current().chat_api_key:
            simulation_context = {
                "_simulation_mode": True,
                "_agent_tool_executor": (
                    lambda name, arguments: run_simulation_tool(
                        name, arguments, normalized.conversation_id,
                    )
                ),
                "_agent_trace_recorder": agent_trace.append,
                "pending_image_recognition": _public_recognition_payload(recognition),
                "image_quote_targets": [{
                    "target_id": "workbench-upload-0",
                    "image_indexes": [0],
                    "recognition": _public_recognition_payload(recognition),
                    "quote": quote.model_dump(mode="json") if quote is not None else None,
                    "quote_error": quote_error,
                }],
                "confirmed_facts": {
                    "city": recognition.city, "cinema_name": recognition.cinema_name,
                    "movie_name": recognition.movie_name, "date": recognition.date_text,
                    "showtime_start": recognition.showtime_start, "showtime_end": recognition.showtime_end,
                    "hall_name": recognition.hall_name, "seat_count": len(recognition.selected_seats),
                },
                "current_quote": quote.model_dump(mode="json") if quote is not None else None,
            }
            reply_text = await _invoke_chat_reply(
                ai_chat_service,
                message_text or "请根据这张截图继续处理",
                normalized.conversation_id,
                runtime_context=simulation_context,
            )
            message_type = "ai_reply_simulation"
        return ChatMessageResponse(message=ChatAssistantMessage(
            id=uuid4().hex,
            conversation_id=normalized.conversation_id,
            message_type=message_type,
            text=reply_text,
            recognition=recognition,
            quote=quote,
            agent_trace=agent_trace,
        ))

    @app.post("/api/chat/text-messages", response_model=ChatMessageResponse)
    async def create_chat_text_message(request: Request, message: ChatTextRequest) -> ChatMessageResponse:
        enforce_rate_limit(request)
        has_key = bool(persistent_settings.current().chat_api_key)
        simulation_context_trace: list[dict[str, object]] = []
        if ai_chat_service is not None and has_key:
            simulation_context = {
                "_simulation_mode": True,
                "_agent_tool_executor": (
                    lambda name, arguments: run_simulation_tool(
                        name, arguments, message.conversation_id,
                    )
                ),
                "_agent_trace_recorder": simulation_context_trace.append,
            } if message.simulation else None
            if message.simulation:
                reply_text = await _invoke_chat_reply(
                    ai_chat_service,
                    message.text,
                    message.conversation_id,
                    runtime_context=simulation_context,
                )
            else:
                reply_text = await _invoke_chat_reply(
                    ai_chat_service, message.text, message.conversation_id,
                )
            message_type = "ai_reply_simulation" if message.simulation else "ai_reply"
        else:
            reply_text = build_guidance_reply(
                message.text,
                templates=persistent_reply_templates.current(),
            )
            message_type = "guidance"
        return ChatMessageResponse(message=ChatAssistantMessage(
            id=uuid4().hex,
            conversation_id=message.conversation_id,
            message_type=message_type,
            text=reply_text,
            agent_trace=simulation_context_trace if message.simulation else [],
        ))

    # Serve the canonical plugin UI assets for local development.  The root
    # page and the iframe use the same files; only the request adapter differs.
    app.mount("/ui", StaticFiles(directory=PLUGIN_UI_PATH), name="plugin-ui")
    return app


app = create_app()
