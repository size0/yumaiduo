from __future__ import annotations

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

from .chat import build_guidance_reply, build_recognition_reply
from .chat_service import CustomerServiceChatService
from .conversation_policy_store import ConversationPolicyStore
from .diagnostics import DiagnosticsStore
from .errors import ImageValidationError, ProviderError, RecognitionError
from .keyword_image_store import MAX_KEYWORD_IMAGE_BYTES, KeywordImageStore
from .knowledge_store import KnowledgeStore
from .liangpiao_callbacks import CallbackError, CallbackVerifier, LiangpiaoCallbackHandler
from .liangpiao_client import LiangpiaoClient
from .liangpiao_order_service import LiangpiaoOrderRequest, LiangpiaoOrderService, OrderServiceError
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
    VisionSettingsUpdate,
    VisionSettingsView,
)
from .observability import LOGGER, REQUEST_ID
from .pricing_store import PricingRulesStore
from .plugin_automation import RulesFirstDecisionEngine
from .quote_record_store import QuoteRecordStore
from .reminder_service import plan_shipped_order_reminders
from .reminder_store import ReminderStore
from .reply_template_store import ReplyTemplateStore
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
INDEX_PATH = Path(__file__).resolve().parents[2] / "frontend" / "v4" / "index.html"


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
    async def reply(self, text: str, conversation_id: str) -> str: ...


class QuoteService(Protocol):
    async def quote(self, recognition: MovieImageInfo) -> RealQuote: ...


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
    rules_database_path = Path(os.getenv("WANDA_RULES_DB_PATH", "data/rules-first.sqlite3"))
    persistent_settings = settings_store or PersistentSettingsStore(settings_path)
    persistent_pricing_rules = pricing_rules_store or PricingRulesStore(pricing_path)
    persistent_shop_automation = shop_automation_store or ShopAutomationStore(shops_path)
    persistent_reply_templates = reply_template_store or ReplyTemplateStore(templates_path)
    persistent_conversation_policy = conversation_policy_store or ConversationPolicyStore(conversation_policy_path)
    persistent_quote_records = quote_record_store or QuoteRecordStore(quote_records_path)
    persistent_reminders = reminder_store or ReminderStore(reminders_path)
    persistent_knowledge = knowledge_store or KnowledgeStore(knowledge_path)
    persistent_keyword_images = keyword_image_store or KeywordImageStore(keyword_images_path)
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
        )
    configured_order_service = liangpiao_order_service
    if configured_order_service is None and configured_liangpiao_client is not None:
        configured_order_service = LiangpiaoOrderService(
            configured_liangpiao_client, quote_store=persistent_rules_store, order_store=persistent_rules_store,
            order_create_enabled=runtime_settings.liangpiao_order_create_enabled,
            external_writes_enabled=runtime_settings.external_writes_enabled,
        )
    configured_callback_handler = liangpiao_callback_handler
    if configured_callback_handler is None and runtime_settings.liangpiao_callback_enabled and runtime_settings.liangpiao_app_secret:
        configured_callback_handler = LiangpiaoCallbackHandler(
            CallbackVerifier(runtime_settings.liangpiao_app_secret),
            state_store=persistent_transaction_states, mapping_store=persistent_rules_store,
            client=configured_liangpiao_client, enabled=True,
        )
    rule_state_coordinator = RuleStateCoordinator(
        persistent_transaction_states, quote_store=persistent_quote_records,
        reply_template_store=persistent_reply_templates,
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
    ai_chat_service = chat_reply_service or (
        CustomerServiceChatService(
            persistent_settings.current,
            diagnostics=diagnostics,
            conversation_policy_provider=persistent_conversation_policy.current,
            knowledge_provider=persistent_knowledge.active_for_prompt,
        )
        if service is None and ai_assist_enabled
        else None
    )
    authoritative_quote_service = quote_service or (
        WandaDirectQuoteService(
            persistent_settings.current,
            diagnostics=diagnostics,
            pricing_rules=persistent_pricing_rules.current,
        )
        if service is None
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
            ai_assist_enabled=ai_assist_enabled,
        )
        if service is None and authoritative_quote_service is not None
        else None
    )
    durable_runtime = rules_first_runtime or (
        RulesFirstRuntime(
            persistent_rules_store, automated_plugin, rule_state_coordinator,
            persistent_transaction_states,
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
            result = await configured_quote_service.quote(SelectedSeatQuoteRequest.model_validate(body))
        except QuoteServiceError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return result.model_dump(mode="json")

    @app.post("/api/liangpiao/order/create")
    async def liangpiao_order_create(body: dict[str, object]) -> dict[str, object]:
        if (
            not runtime_settings.liangpiao_order_create_enabled
            or not runtime_settings.external_writes_enabled
            or configured_order_service is None
        ):
            raise HTTPException(status_code=503, detail="liangpiao_order_create_disabled")
        try:
            result = await configured_order_service.create(LiangpiaoOrderRequest.model_validate(body))
        except OrderServiceError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return result.model_dump(mode="json")

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
        try:
            return await configured_callback_handler.handle(
                raw, signature=x_liangpiao_sign or "", timestamp=x_liangpiao_timestamp or "", nonce=x_liangpiao_nonce or "",
            )
        except CallbackError as error:
            raise HTTPException(status_code=409, detail=error.code) from error

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
        return {"records": records, "count": len(records)}

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
        return {"shops": persistent_shop_automation.list_shops(tenant_id)}

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
                persistent_quote_records.mark_delivered(
                    tenant_id=command["tenant_id"], record_id=command["event_id"],
                    delivered_at=datetime.now(timezone.utc), message_id=message_id,
                )
        return recorded

    @app.get("/api/rules-first/manual-tasks")
    async def list_rules_manual_tasks(
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
        return {"tasks": persistent_rules_store.list_manual_tasks(tenant_id)}

    @app.post("/api/rules-first/manual-tasks/{task_id}/claim")
    async def claim_rules_manual_task(
        task_id: str, body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None),
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
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
                operator_id=str(body.get("operator_id") or ""),
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
    ) -> dict[str, object]:
        tenant_id = require_panel_tenant(x_wanda_tenant_id)
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
                existing.get("status") != "claimed" or existing.get("lease_token") != lease_token
                or not existing.get("lease_until")
                or str(existing["lease_until"]) <= datetime.now(timezone.utc).isoformat()
            ):
                raise ValueError("manual_task_lease_conflict")
            if resolution == "resume":
                current = persistent_transaction_states.get(
                    tenant_id=tenant_id, shop_id=existing["shop_id"],
                    buyer_id=existing["buyer_id"], chat_id=existing["chat_id"],
                )
                if current is None or current.revision != expected_revision or current.flow_state != "MANUAL_HOLD":
                    raise ValueError("manual_resume_revision_conflict")
                persistent_transaction_states.transition(
                    tenant_id=tenant_id, shop_id=existing["shop_id"],
                    buyer_id=existing["buyer_id"], chat_id=existing["chat_id"],
                    expected_revision=expected_revision, event_id=f"manual-resume:{task_id}",
                    transition_code="manual_revision_resume", flow_state="ORDER_UNVERIFIED",
                    updates={"automation_control": "active", "order_status": "unverified"},
                )
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

    @app.post("/api/chat/image-messages", response_model=ChatMessageResponse)
    async def create_chat_image_message(
        request: Request,
        image: Annotated[UploadFile, File()],
        conversation_id: Annotated[str, Form(min_length=1, max_length=128)],
        message_text: Annotated[str, Form(max_length=2000)] = "",
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
        if authoritative_quote_service is not None:
            try:
                quote = await authoritative_quote_service.quote(recognition)
            except RecognitionError as error:
                quote_error = error.message
                diagnostics.add("quote_unavailable", code=error.code, message=error.message)
                LOGGER.warning("event=quote_unavailable code=%s", error.code)
        remember_image_context = getattr(ai_chat_service, "remember_image_context", None)
        if callable(remember_image_context):
            remember_image_context(normalized.conversation_id, recognition, quote, quote_error)
        return ChatMessageResponse(message=ChatAssistantMessage(
            id=uuid4().hex,
            conversation_id=normalized.conversation_id,
            message_type="movie_recognition",
            text=build_recognition_reply(
                recognition,
                quote=quote,
                quote_error=quote_error,
                templates=persistent_reply_templates.current(),
            ),
            recognition=recognition,
            quote=quote,
        ))

    @app.post("/api/chat/text-messages", response_model=ChatMessageResponse)
    async def create_chat_text_message(request: Request, message: ChatTextRequest) -> ChatMessageResponse:
        enforce_rate_limit(request)
        has_key = bool(persistent_settings.current().chat_api_key)
        if ai_chat_service is not None and has_key:
            reply_text = await ai_chat_service.reply(message.text, message.conversation_id)
            message_type = "ai_reply"
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
        ))

    return app


app = create_app()
