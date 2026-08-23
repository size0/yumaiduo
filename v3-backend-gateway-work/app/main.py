from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .quote_preview_store import QuotePreviewStore
from .quote_preview_support import (
    _buyer_app_has_lower_price, _preview_failure_code,
    _recognition_needs_seat_or_count_confirmation,
)
from .quote_reply import (
    quote_reply_text as _quote_reply_text,
    validate_quote_reply_template as _validate_quote_reply_template,
)
from .routes.agent_reply import create_agent_reply_router
from .routes.plugin_bridge import create_plugin_bridge_router
from .routes.quote_preview import create_quote_preview_router
from .plugin_bridge_store import PluginBridgeStore
from .knowledge_base_store import KnowledgeBaseStore
from .conversation_agent import ConversationAgentService
from .reply_preview import ReplyPreviewService
from .match_candidate_resolver import MatchCandidateResolver
from .local_catalog import LocalWandaCatalog
from .schemas import ImageUploadResponse, ModelSettingsUpdate, ModelSettingsView, QuoteRealtimeRequest, QuoteRealtimeResponse, StorageSettingsView, VisionRecognizeRequest, VisionRecognizeResponse, WandaQuoteSettingsUpdate, WandaQuoteSettingsView
from .settings_store import ModelSettingsStore
from .storage import CosStorageService
from .storage_store import CosSettingsStore
from .vision import PROMPT_VERSION, VisionService
from .wanda_quote import LocalTicketGateway, RealtimeQuoteService
from .wanda_quote_store import WandaQuoteSettingsStore

COS_CLEANUP_INTERVAL_SECONDS = 30 * 60
PREVIEW_VISION_RETRY_DELAY_SECONDS = 0.35
QUOTE_SHUTDOWN_TIMEOUT_SECONDS = 65
V3_RUNTIME_CONTRACT = "wanda-v3-v16-vision-consistency-gates"
async def _run_storage_cleanup(app: FastAPI) -> None:
    cleanup = getattr(app.state.storage_service, "cleanup_expired_images", None)
    if cleanup is not None:
        await cleanup(app.state.cos_store.read())


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()

    async def cleanup_loop() -> None:
        while not stop_event.is_set():
            await _run_storage_cleanup(app)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=COS_CLEANUP_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        stop_event.set()
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        close_quote_service = getattr(app.state.quote_service, "aclose", None)
        if callable(close_quote_service):
            try:
                await asyncio.wait_for(close_quote_service(), timeout=QUOTE_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                # The original quote is already failed closed; shutdown must
                # not hang indefinitely if an external read never returns.
                pass


def create_app(
    store: ModelSettingsStore | None = None,
    vision_service: VisionService | None = None,
    cos_store: CosSettingsStore | None = None,
    storage_service: CosStorageService | None = None,
    quote_service: RealtimeQuoteService | None = None,
    wanda_quote_store: WandaQuoteSettingsStore | None = None,
    quote_preview_store: QuotePreviewStore | None = None,
    reply_preview_service: ReplyPreviewService | None = None,
    conversation_agent_service: ConversationAgentService | None = None,
    match_candidate_resolver: MatchCandidateResolver | None = None,
    plugin_bridge_store: PluginBridgeStore | None = None,
    local_catalog: LocalWandaCatalog | None = None,
) -> FastAPI:
    app = FastAPI(title="万达 AI 客服", version="0.1.0", lifespan=lifespan)
    configured_path = os.getenv("WANDA_SETTINGS_PATH")
    settings_path = Path(configured_path) if configured_path else Path(__file__).resolve().parents[1] / "data" / "model_config.json"
    app.state.settings_store = store or ModelSettingsStore(settings_path)
    configured_vision_cache_path = os.getenv("WANDA_VISION_RECOGNITION_CACHE_PATH")
    vision_cache_path = Path(configured_vision_cache_path) if configured_vision_cache_path else Path("/var/lib/ticket-system/wanda-ai-v2-data/vision-recognition-cache.json")
    app.state.vision_service = vision_service or VisionService(cache_path=vision_cache_path)
    configured_cos_path = os.getenv("WANDA_COS_SETTINGS_PATH")
    cos_settings_path = Path(configured_cos_path) if configured_cos_path else Path(__file__).resolve().parents[1] / "data" / "cos_config.json"
    app.state.cos_store = cos_store or CosSettingsStore(cos_settings_path)
    app.state.storage_service = storage_service or CosStorageService()
    configured_wanda_quote_path = os.getenv("WANDA_QUOTE_SETTINGS_PATH")
    wanda_quote_settings_path = Path(configured_wanda_quote_path) if configured_wanda_quote_path else Path(__file__).resolve().parents[1] / "data" / "wanda_quote_config.json"
    app.state.wanda_quote_store = wanda_quote_store or WandaQuoteSettingsStore(wanda_quote_settings_path)
    app.state.local_catalog = local_catalog or LocalWandaCatalog()
    quote_settings = app.state.wanda_quote_store.read()
    app.state.quote_service = quote_service or RealtimeQuoteService(
        LocalTicketGateway(account_phone=str(quote_settings["account_phone"])),
        cinema_catalog=app.state.local_catalog,
    )
    configured_preview_path = os.getenv("WANDA_QUOTE_PREVIEW_STORE_PATH")
    preview_store_path = Path(configured_preview_path) if configured_preview_path else Path(__file__).resolve().parents[1] / "data" / "quote_preview_queue.json"
    app.state.quote_preview_store = quote_preview_store or QuotePreviewStore(preview_store_path)
    app.state.reply_preview_service = reply_preview_service or ReplyPreviewService()
    app.state.conversation_agent_service = conversation_agent_service or ConversationAgentService()
    app.state.match_candidate_resolver = match_candidate_resolver or MatchCandidateResolver()
    configured_bridge_path = os.getenv("WANDA_PLUGIN_BRIDGE_SETTINGS_PATH")
    bridge_store_path = Path(configured_bridge_path) if configured_bridge_path else Path(__file__).resolve().parents[1] / "data" / "plugin_bridge_settings.json"
    app.state.plugin_bridge_store = plugin_bridge_store or PluginBridgeStore(bridge_store_path)
    knowledge_path = Path(os.getenv("WANDA_KNOWLEDGE_BASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "knowledge_base.json")))
    app.state.knowledge_base_store = KnowledgeBaseStore(knowledge_path)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type", "X-Wanda-Preview-Key"],
    )
    app.include_router(create_plugin_bridge_router(app))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime_contract": V3_RUNTIME_CONTRACT}

    def reply_model_settings() -> dict[str, object]:
        """Attach editable operator guidance without exposing the API key."""
        runtime = app.state.plugin_bridge_store.runtime()
        return {
            **app.state.settings_store.read(),
            "ai_reply_system_prompt": str(runtime.get("ai_reply_system_prompt", "")).strip(),
            "ai_reply_shop_background": str(runtime.get("ai_reply_shop_background", "")).strip(),
            "ai_reply_precautions": str(runtime.get("ai_reply_precautions", "")).strip(),
            "ai_reply_style": str(runtime.get("ai_reply_style", "")).strip(),
            "ai_reply_memory_hours": runtime.get("ai_reply_memory_hours", 24),
            "ai_reply_memory_depth": runtime.get("ai_reply_memory_depth", 20),
            "ai_reply_delay_seconds": runtime.get("ai_reply_delay_seconds", 3),
            "ai_reply_manual_takeover_seconds": runtime.get("ai_reply_manual_takeover_seconds", 20),
        }

    app.include_router(create_quote_preview_router(app, reply_model_settings))
    app.include_router(create_agent_reply_router(app, reply_model_settings))

    @app.get("/api/settings/model", response_model=ModelSettingsView)
    async def get_model_settings() -> ModelSettingsView:
        return app.state.settings_store.view()

    @app.put("/api/settings/model", response_model=ModelSettingsView)
    async def save_model_settings(update: ModelSettingsUpdate) -> ModelSettingsView:
        return app.state.settings_store.save(update)

    @app.get("/api/settings/storage", response_model=StorageSettingsView)
    async def get_storage_settings() -> StorageSettingsView:
        return app.state.cos_store.view()

    @app.get("/api/settings/wanda-quote", response_model=WandaQuoteSettingsView)
    async def get_wanda_quote_settings() -> WandaQuoteSettingsView:
        return app.state.wanda_quote_store.view()

    @app.put("/api/settings/wanda-quote", response_model=WandaQuoteSettingsView)
    async def save_wanda_quote_settings(update: WandaQuoteSettingsUpdate) -> WandaQuoteSettingsView:
        saved = app.state.wanda_quote_store.save(update)
        app.state.quote_service = RealtimeQuoteService(
            LocalTicketGateway(account_phone=saved.account_phone),
            cinema_catalog=app.state.local_catalog,
        )
        return saved

    @app.post("/api/storage/images", response_model=ImageUploadResponse)
    async def upload_image(image: UploadFile = File(...)) -> ImageUploadResponse:
        return await app.state.storage_service.upload_image(image, app.state.cos_store.read())

    @app.post("/api/storage/reply-images", response_model=ImageUploadResponse)
    async def upload_reply_image(image: UploadFile = File(...)) -> ImageUploadResponse:
        return await app.state.storage_service.upload_image(image, app.state.cos_store.read(), persistent=True)

    @app.post("/api/wanda-ai/vision/recognize", response_model=VisionRecognizeResponse)
    async def recognize_ticket_image(request: VisionRecognizeRequest) -> VisionRecognizeResponse:
        service = app.state.vision_service
        recognition = await (service.recognize(request, app.state.settings_store.read(), app.state.knowledge_base_store.active("vision")) if isinstance(service, VisionService) else service.recognize(request, app.state.settings_store.read()))
        return VisionRecognizeResponse(prompt_version=PROMPT_VERSION, recognition=recognition)

    @app.post(
        "/api/wanda-ai/quote/realtime",
        response_model=QuoteRealtimeResponse,
        response_model_exclude={"pricing_account_ref"},
    )
    async def quote_realtime(request: QuoteRealtimeRequest) -> QuoteRealtimeResponse:
        recognition = app.state.local_catalog.canonicalize(request.recognition)
        return await app.state.quote_service.quote(request.model_copy(update={"recognition": recognition}))

    return app




app = create_app()
