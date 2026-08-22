from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware

from .quote_preview_store import QuotePreviewStore, empty_pending_record
from .plugin_bridge_store import DEFAULT_REPLY_TEMPLATE_IMAGES, DEFAULT_REPLY_TEMPLATES, PluginBridgeStore
from .knowledge_base_store import KnowledgeBaseStore
from .conversation_agent import ConversationAgentService, classify_agent_scene
from .reply_preview import ReplyPreviewService
from .match_candidate_resolver import MatchCandidateResolver
from .local_catalog import LocalWandaCatalog
from .schemas import AgentTurnRequest, AgentTurnResponse, AvailableWplusSeatsRequest, AvailableWplusSeatsResponse, ConversationExperienceIngestRequest, ImageType, ImageUploadResponse, Recognition, ModelSettingsUpdate, ModelSettingsView, PendingQuoteRecord, PendingQuotesResponse, QuoteMatchCandidateRequest, QuoteMatchCandidateResponse, QuotePreviewIngestRequest, QuotePreviewIngestResponse, QuotePreviewQuoteRequest, QuoteRealtimeRequest, QuoteRealtimeResponse, QuoteShowtimeResolveRequest, QuoteShowtimeResolveResponse, ReplyPreviewIngestRequest, ReplyPreviewIngestResponse, StorageSettingsView, VisionRecognizeRequest, VisionRecognizeResponse, WandaQuoteSettingsUpdate, WandaQuoteSettingsView
from .settings_store import ModelSettingsStore
from .storage import CosStorageService
from .storage_store import CosSettingsStore
from .vision import PROMPT_VERSION, VisionFailure, VisionService
from .wanda_quote import LocalTicketGateway, RealtimeQuoteService
from .wanda_quote_store import WandaQuoteSettingsStore

COS_CLEANUP_INTERVAL_SECONDS = 30 * 60
PREVIEW_VISION_RETRY_DELAY_SECONDS = 0.35
QUOTE_SHUTDOWN_TIMEOUT_SECONDS = 65
V3_RUNTIME_CONTRACT = "wanda-v3-v11-pricing-account-evidence"
SCREENSHOT_PRICE_CONFIDENCE_THRESHOLD = 0.85
logger = logging.getLogger(__name__)


def _explicit_yuan_cents(value: object) -> int | None:
    try:
        cents = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        return None
    if cents != cents.to_integral_value():
        return None
    result = int(cents)
    return result if result > 0 else None


def _buyer_app_has_lower_price(quote: QuoteRealtimeResponse, recognition: Recognition) -> bool:
    """Use screenshot prices only to suppress a worse offer, never to price ours."""
    if recognition.confidence.price < SCREENSHOT_PRICE_CONFIDENCE_THRESHOLD:
        return False
    count = quote.ticket_count
    quoted_total = quote.total_quote_cents
    if not count or not quoted_total:
        return False

    comparable_totals: list[int] = []
    selected = recognition.official_selection
    if selected.is_selected and selected.selected_count == count:
        explicit_total = _explicit_yuan_cents(selected.total_price)
        if explicit_total:
            comparable_totals.append(explicit_total)
        elif len(selected.seats) == count:
            seat_prices = [_explicit_yuan_cents(seat.price) for seat in selected.seats]
            if all(price is not None for price in seat_prices):
                comparable_totals.append(sum(price for price in seat_prices if price is not None))

    # Without an official selected-seat total, compare only the same explicit
    # seat zone. A cheaper unrelated zone must not suppress the verified quote.
    if not comparable_totals:
        quote_zones = {seat.seat_zone_type for seat in quote.seat_quotes}
        if not quote_zones:
            quote_zones = {quote.seat_zone_type}
        if len(quote_zones) == 1:
            quote_zone = next(iter(quote_zones))
            unit_prices = [
                cents
                for item in recognition.visible_prices
                if item.zone_type == quote_zone
                if (cents := _explicit_yuan_cents(item.price_yuan)) is not None
            ]
            if unit_prices:
                comparable_totals.append(min(unit_prices) * count)

    return bool(comparable_totals) and quoted_total > min(comparable_totals)


def _recognition_needs_seat_or_count_confirmation(recognition: Recognition, requested_ticket_count: int | None) -> bool:
    # A hand-drawn circle is an artificial-delivery preference, never evidence
    # of quantity. Only an official selected-seat card or explicit buyer text
    # can satisfy the ticket-count requirement.
    return recognition.official_selection.selected_count <= 0 and not requested_ticket_count


def _preview_failure_code(error: Exception) -> str:
    """Return a stable, non-sensitive code for preview diagnostics."""
    if isinstance(error, VisionFailure):
        return error.code
    if isinstance(error, HTTPException):
        detail = str(error.detail)
        if re.fullmatch(r"(?:ai_vision|image)_[a-z0-9_]+", detail):
            return detail
        match = re.fullmatch(r"模型服务返回 HTTP (\d{3})", str(error.detail))
        if match:
            return f"model_http_{match.group(1)}"
        return f"http_{error.status_code}"
    exception_name = re.sub(r"[^a-z0-9]+", "_", type(error).__name__.lower()).strip("_")
    return f"unexpected_{exception_name or 'error'}"


async def _recognize_preview_image(app: FastAPI, request: VisionRecognizeRequest):
    """Retry one transient image-read validation failure before marking a quote preview failed."""
    for attempt in range(2):
        try:
            service = app.state.vision_service
            if isinstance(service, VisionService):
                return await service.recognize(request, app.state.settings_store.read(), app.state.knowledge_base_store.active("vision"))
            return await service.recognize(request, app.state.settings_store.read())
        except HTTPException as error:
            # Older FastAPI/Starlette releases used by the production service
            # do not expose HTTP_422_UNPROCESSABLE_CONTENT. Compare the HTTP
            # status value directly so a valid image-validation failure is
            # retried instead of being turned into an AttributeError/HTTP 500.
            if error.status_code != 422 or attempt == 1:
                raise
            await asyncio.sleep(PREVIEW_VISION_RETRY_DELAY_SECONDS)
    raise AssertionError("preview vision retry loop must return or raise")


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
    app.state.vision_service = vision_service or VisionService()
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "runtime_contract": V3_RUNTIME_CONTRACT}

    def require_plugin_bridge_key(
        x_plugin_bridge_key: str | None = Header(default=None, alias="X-Plugin-Bridge-Key"),
    ) -> None:
        configured_key = os.getenv("WANDA_PLUGIN_BRIDGE_KEY", "")
        if not configured_key or not x_plugin_bridge_key or not hmac.compare_digest(x_plugin_bridge_key, configured_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def bridge_runtime_settings(account_unb: str | None = None) -> dict[str, object]:
        runtime = app.state.plugin_bridge_store.runtime()
        model = app.state.settings_store.read()
        api_key = str(model.get("api_key", ""))
        settings: dict[str, object] = {
            **runtime,
            # Active mode remains fail-closed until the durable Agent worker
            # owns the turn, tools and reply outbox end to end. This also
            # neutralizes a stale persisted "active" value after rollback.
            "conversation_agent_mode": "shadow" if runtime.get("conversation_agent_mode") == "active" else runtime.get("conversation_agent_mode", "shadow"),
            "conversation_agent_active_ready": False,
            "execution_owner": "deterministic",
            "ai_reply_base_url": str(model.get("base_url", "")),
            "ai_reply_model": str(model.get("model", "")),
            "ai_reply_key_configured": bool(api_key),
            "ai_reply_api_key_masked": f"***{api_key[-4:]}" if api_key else "",
        }
        if account_unb:
            settings["shop_enabled"] = app.state.plugin_bridge_store.shop_enabled(account_unb)
        return settings

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

    def bridge_account_unb(value: str | None) -> str:
        account_unb = str(value or "").strip()
        if not account_unb or len(account_unb) > 128:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid account_unb")
        return account_unb

    def patch_runtime_settings(payload: dict[str, object]) -> dict[str, object]:
        bridge_fields = {
            "automation_enabled",
            "recognition_enabled",
            "quote_enabled",
            "auto_price_change",
            "ai_reply_enabled",
            "conversation_agent_mode",
            "ai_reply_system_prompt",
            "ai_reply_shop_background",
            "ai_reply_precautions",
            "ai_reply_style",
            "ai_reply_memory_hours",
            "ai_reply_memory_depth",
            "ai_reply_delay_seconds",
            "ai_reply_manual_takeover_seconds",
            "low_confidence_threshold",
            "reply_templates",
            "reply_template_images",
        }
        patch: dict[str, object] = {}
        for field in bridge_fields:
            if field not in payload:
                continue
            value = payload[field]
            if field == "conversation_agent_mode":
                if value not in {"off", "shadow", "active"}:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid conversation_agent_mode")
                if value == "active":
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="conversation_agent_active_not_ready")
                patch[field] = value
            elif field == "low_confidence_threshold":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
                patch[field] = float(value)
            elif field == "reply_templates":
                if not isinstance(value, dict) or set(value) != set(DEFAULT_REPLY_TEMPLATES):
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid reply_templates")
                patch[field] = {key: _validate_quote_reply_template(template) for key, template in value.items()}
            elif field == "reply_template_images":
                if not isinstance(value, dict) or set(value) != set(DEFAULT_REPLY_TEMPLATE_IMAGES):
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid reply_template_images")
                normalized_images: dict[str, str] = {}
                for key, image_url in value.items():
                    if not isinstance(image_url, str) or len(image_url.strip()) > 2_000:
                        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid reply template image URL")
                    image_url = image_url.strip()
                    if image_url:
                        parsed = urlsplit(image_url)
                        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid reply template image URL")
                    normalized_images[key] = image_url
                patch[field] = normalized_images
            elif field == "ai_reply_system_prompt":
                if not isinstance(value, str) or not (1 <= len(value.strip()) <= 8_000):
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid ai_reply_system_prompt")
                patch[field] = value.strip()
            elif field in {"ai_reply_shop_background", "ai_reply_precautions", "ai_reply_style"}:
                if not isinstance(value, str) or len(value.strip()) > 4_000:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
                patch[field] = value.strip()
            elif field == "ai_reply_memory_hours":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 24:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid ai_reply_memory_hours")
                patch[field] = value
            elif field == "ai_reply_memory_depth":
                if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 50:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid ai_reply_memory_depth")
                patch[field] = value
            elif field == "ai_reply_delay_seconds":
                if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 60:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
                patch[field] = value
            elif field == "ai_reply_manual_takeover_seconds":
                if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 60:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
                patch[field] = value
            elif not isinstance(value, bool):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
            else:
                patch[field] = value
        if patch:
            app.state.plugin_bridge_store.update_runtime(patch)

        model_patch_requested = any(field in payload for field in ("ai_reply_base_url", "ai_reply_model", "ai_reply_api_key", "ai_reply_clear_api_key"))
        if model_patch_requested:
            current = app.state.settings_store.read()
            base_url = str(payload.get("ai_reply_base_url", current["base_url"])).strip()
            model_name = str(payload.get("ai_reply_model", current["model"])).strip()
            if not base_url or not model_name:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="ai_reply_base_url and ai_reply_model are required")
            clear_api_key = payload.get("ai_reply_clear_api_key") is True
            api_key: str | None = None
            if "ai_reply_api_key" in payload and not clear_api_key:
                candidate = payload["ai_reply_api_key"]
                if not isinstance(candidate, str) or not candidate.strip():
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid ai_reply_api_key")
                api_key = candidate.strip()
            update = ModelSettingsUpdate.model_validate({
                "base_url": base_url,
                "model": model_name,
                "api_key": api_key,
                "temperature": current["temperature"],
                "max_tokens": current["max_tokens"],
            })
            app.state.settings_store.save(update)
            if clear_api_key:
                app.state.settings_store.clear_api_key()
        return bridge_runtime_settings()

    def bridge_tenant_id(tenant_id: str | None) -> str:
        normalized = str(tenant_id or "").strip()
        if not normalized or len(normalized) > 128:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid tenant_id")
        return normalized

    @app.get("/api/xianyu-plugin/bridge/runtime-settings")
    async def get_plugin_runtime_settings(_: None = Depends(require_plugin_bridge_key)) -> dict[str, object]:
        return {"settings": bridge_runtime_settings()}

    @app.get("/api/xianyu-plugin/bridge/settings")
    async def get_plugin_runtime_settings_for_account(
        account_unb: str | None = None,
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"settings": bridge_runtime_settings(bridge_account_unb(account_unb))}

    @app.put("/api/xianyu-plugin/bridge/shop-settings")
    async def update_plugin_shop_settings(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        account_unb = bridge_account_unb(payload.get("account_unb") if isinstance(payload.get("account_unb"), str) else None)
        enabled = payload.get("automation_enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid automation_enabled")
        app.state.plugin_bridge_store.update_shop_enabled(account_unb, enabled)
        return {"settings": bridge_runtime_settings(account_unb)}

    @app.put("/api/xianyu-plugin/bridge/runtime-settings")
    async def update_plugin_runtime_settings(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"settings": patch_runtime_settings(payload)}

    @app.put("/api/xianyu-plugin/bridge/agent-canary-approval")
    async def update_agent_canary_approval(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        action = payload.get("action")
        if action == "revoke":
            app.state.plugin_bridge_store.update_runtime({
                "agent_canary_enabled": False,
                "agent_canary_kill_switch": True,
                "agent_canary_percentage": 0,
                "agent_canary_approved": False,
                "agent_canary_runtime_version": "",
                "agent_canary_approved_at": None,
            })
            return {"settings": bridge_runtime_settings()}
        if action != "approve":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid agent canary approval action")
        runtime_version = payload.get("runtime_version")
        percentage = payload.get("percentage")
        if not isinstance(runtime_version, str) or not 8 <= len(runtime_version.strip()) <= 100:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid agent canary runtime version")
        if isinstance(percentage, bool) or not isinstance(percentage, int) or not 1 <= percentage <= 5:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid agent canary percentage")
        evidence_fields = (
            "canary_readiness_ready", "image_offline_evaluation_ready",
            "execution_owner_proven", "rollback_verified",
        )
        if any(payload.get(field) is not True for field in evidence_fields):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="agent_canary_approval_evidence_incomplete")
        app.state.plugin_bridge_store.update_runtime({
            "agent_canary_enabled": True,
            "agent_canary_kill_switch": False,
            "agent_canary_percentage": percentage,
            "agent_canary_approved": True,
            "agent_canary_runtime_version": runtime_version.strip(),
            "agent_canary_approved_at": datetime.now(UTC).isoformat(),
        })
        # Approval is intentionally independent from Active mode. The latter
        # remains hard-disabled until its separate execution-owner release.
        return {"settings": bridge_runtime_settings()}

    @app.get("/api/xianyu-plugin/bridge/quote-policy")
    async def get_plugin_quote_policy(
        tenant_id: str | None = None,
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"policy": app.state.plugin_bridge_store.quote_policy(bridge_tenant_id(tenant_id))}

    @app.put("/api/xianyu-plugin/bridge/quote-policy")
    async def update_plugin_quote_policy(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        tenant_id = bridge_tenant_id(payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None)
        patch: dict[str, int] = {}
        for field in ("wplus_adjustment_cents", "wplus_member_price_threshold_cents", "regular_adjustment_cents", "max_auto_order_amount_cents"): 
            if field not in payload:
                continue
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
            patch[field] = value
        if not patch:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="quote policy patch is empty")
        return {"policy": app.state.plugin_bridge_store.update_quote_policy(tenant_id, patch)}

    @app.post("/api/xianyu-plugin/bridge/shops/sync")
    async def sync_plugin_shops(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, int]:
        bridge_tenant_id(payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None)
        shops = payload.get("shops")
        if not isinstance(shops, list):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid shops")
        return {"synced": len(shops)}

    @app.get("/api/xianyu-plugin/bridge/knowledge-base")
    async def list_knowledge_base(
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        tenant_id = bridge_tenant_id(x_yumaiduo_tenant_id)
        return {"entries": app.state.knowledge_base_store.list(tenant_id)}

    @app.post("/api/xianyu-plugin/bridge/knowledge-base")
    async def create_knowledge_base(
        payload: dict[str, object] = Body(...),
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        try:
            return {"entry": app.state.knowledge_base_store.create(payload, bridge_tenant_id(x_yumaiduo_tenant_id))}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/xianyu-plugin/bridge/knowledge-base/{entry_id}")
    async def update_knowledge_base(
        entry_id: str,
        payload: dict[str, object] = Body(...),
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        try:
            return {"entry": app.state.knowledge_base_store.update(entry_id, payload, bridge_tenant_id(x_yumaiduo_tenant_id))}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="knowledge entry not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/xianyu-plugin/bridge/conversation-experiences", status_code=201)
    async def record_conversation_experience(
        request: ConversationExperienceIngestRequest,
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        tenant_id = bridge_tenant_id(x_yumaiduo_tenant_id)
        if tenant_id != request.tenant_id:
            raise HTTPException(status_code=403, detail="tenant mismatch")
        try:
            entry = app.state.knowledge_base_store.record_experience(tenant_id, request.candidate.model_dump(mode="json"))
            return {"status": "draft_updated" if int(entry.get("evidence_count", 1)) > 1 else "draft_created", "entry": entry}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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

    @app.post("/api/quotes/preview-recognize", response_model=VisionRecognizeResponse)
    async def preview_recognize(
        request: QuotePreviewIngestRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> VisionRecognizeResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        if request.image_url is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="image_required")
        try:
            recognition = await _recognize_preview_image(app, VisionRecognizeRequest(image_url=request.image_url, message_text=request.message_text))
        except VisionFailure as error:
            raise HTTPException(status_code=error.status_code, detail={"code": error.code, "diagnostics": error.diagnostics}) from error
        return VisionRecognizeResponse(prompt_version=PROMPT_VERSION, recognition=recognition)

    @app.post("/api/quotes/preview-quote", response_model=QuoteRealtimeResponse)
    async def preview_quote(
        request: QuotePreviewQuoteRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuoteRealtimeResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        policy = app.state.plugin_bridge_store.quote_policy(request.tenant_id)
        request = request.model_copy(update={"recognition": app.state.local_catalog.canonicalize(request.recognition)})
        try:
            quote = await app.state.quote_service.quote(request, wplus_adjustment_cents=int(policy["wplus_adjustment_cents"]), wplus_member_price_threshold_cents=int(policy["wplus_member_price_threshold_cents"]), regular_adjustment_cents=int(policy["regular_adjustment_cents"]))
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {}
            code = str(detail.get("code", "quote_verification_failed"))
            templates = app.state.plugin_bridge_store.runtime().get("reply_templates", {})
            fallback_template = DEFAULT_REPLY_TEMPLATES.get(code, DEFAULT_REPLY_TEMPLATES["quote_verification_failed"])
            template = templates.get(code, fallback_template) if isinstance(templates, dict) else fallback_template
            raise HTTPException(status_code=error.status_code, detail={**detail, "code": code, "reply_text": _static_reply_text(str(template))}) from error
        recommend_buyer_app = _buyer_app_has_lower_price(quote, request.recognition)
        policy_version = "quote-policy-" + hashlib.sha256(
            json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        quote = quote.model_copy(update={
            "buyer_app_purchase_recommended": recommend_buyer_app,
            "pricing_rule_version": policy_version,
        })
        return quote.model_copy(update={"reply_text": _quote_reply_text(quote, request.recognition, _quote_reply_template(app.state.plugin_bridge_store.runtime(), quote))})

    @app.post("/api/quotes/preview-resolve-showtime", response_model=QuoteShowtimeResolveResponse)
    async def preview_resolve_showtime(
        request: QuoteShowtimeResolveRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuoteShowtimeResolveResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        recognition = app.state.local_catalog.canonicalize(request.recognition)
        return await app.state.quote_service.resolve_showtime(recognition)

    @app.post("/api/quotes/preview-available-seats", response_model=AvailableWplusSeatsResponse)
    async def preview_available_wplus_seats(
        request: AvailableWplusSeatsRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> AvailableWplusSeatsResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        recognition = app.state.local_catalog.canonicalize(request.recognition)
        return await app.state.quote_service.available_wplus_seats(recognition, request.row)

    @app.post("/api/quotes/preview-resolve-candidates", response_model=QuoteMatchCandidateResponse)
    async def preview_resolve_candidates(
        request: QuoteMatchCandidateRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuoteMatchCandidateResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        resolution = app.state.local_catalog.resolve(request.recognition)
        if not resolution.matched:
            return QuoteMatchCandidateResponse()
        return await app.state.match_candidate_resolver.resolve(resolution.recognition, app.state.settings_store.read())

    @app.get("/api/quotes/pending", response_model=PendingQuotesResponse)
    async def get_pending_quotes(tenant_id: str | None = None) -> PendingQuotesResponse:
        return PendingQuotesResponse(records=app.state.quote_preview_store.pending(tenant_id))

    @app.post("/api/quotes/preview-ingest", response_model=QuotePreviewIngestResponse)
    async def ingest_quote_preview(
        request: QuotePreviewIngestRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuotePreviewIngestResponse:
        configured_key = os.getenv("WANDA_PREVIEW_INGEST_KEY", "")
        if not configured_key or not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, configured_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

        record = empty_pending_record(
            event_id=request.event_id,
            buyer_label=QuotePreviewStore.masked_buyer_label(request.buyer_label),
            message_summary=QuotePreviewStore.message_summary(request.message_text),
        )
        claimed, created = app.state.quote_preview_store.claim(request.event_id, request.tenant_id, record)
        if not created:
            return QuotePreviewIngestResponse(status=str(claimed.get("ingest_status", "failed")), duplicate=True)

        event_digest = str(claimed["event_digest"])
        if request.image_url is None:
            app.state.quote_preview_store.complete(event_digest, ingest_status="needs_image", record=record)
            return QuotePreviewIngestResponse(status="needs_image")

        try:
            recognition = await _recognize_preview_image(
                app,
                VisionRecognizeRequest(image_url=request.image_url, message_text=request.message_text),
            )
        except Exception as error:
            failure_code = _preview_failure_code(error)
            logger.warning(
                "quote preview vision failed event_id=%s failure_stage=vision failure_code=%s image_mime=%s image_bytes=%s provider_status=%s provider_content_type=%s",
                request.event_id,
                failure_code,
                getattr(error, "image_mime", None),
                getattr(error, "image_bytes", None),
                getattr(error, "provider_status", None),
                getattr(error, "provider_content_type", None),
            )
            app.state.quote_preview_store.complete(
                event_digest,
                ingest_status="failed",
                record=record,
                failure_stage="vision",
                failure_code=failure_code,
            )
            return QuotePreviewIngestResponse(status="failed", failure_code=failure_code)

        if recognition.image_type is not ImageType.SEAT_MAP:
            app.state.quote_preview_store.complete(
                event_digest,
                ingest_status="failed",
                record=record,
                recognition=recognition.model_dump(mode="json"),
                failure_stage="intent",
                failure_code="image_not_seat_map",
            )
            ignored_reply = {
                ImageType.CHAT_IMAGE: "截图中的价格仅供参考，实际价格以万达实时核价结果为准。",
                ImageType.ORDER_CONFIRM: "已收到订单或付款相关截图，订单支付和出票请以订单状态为准；不会重新核价。",
            }.get(recognition.image_type)
            return QuotePreviewIngestResponse(
                status="ignored",
                reply_text=ignored_reply,
                failure_code="image_not_seat_map",
            )

        try:
            policy = app.state.plugin_bridge_store.quote_policy(request.tenant_id)
            quote = await app.state.quote_service.quote(
                QuoteRealtimeRequest(recognition=recognition, ticket_count=request.ticket_count),
                wplus_adjustment_cents=int(policy["wplus_adjustment_cents"]),
                wplus_member_price_threshold_cents=int(policy["wplus_member_price_threshold_cents"]),
                regular_adjustment_cents=int(policy["regular_adjustment_cents"]),
            )
            quote = quote.model_copy(update={
                "buyer_app_purchase_recommended": _buyer_app_has_lower_price(quote, recognition),
            })
            preview_record = PendingQuoteRecord(
                id=record.id,
                buyer_label=record.buyer_label,
                message_summary=record.message_summary,
                cinema=quote.matched_cinema_name or recognition.cinema,
                movie=recognition.movie,
                date=recognition.date.isoformat() if recognition.date else None,
                showtime=recognition.showtime,
                hall=recognition.hall,
                unit_quote_cents=quote.unit_quote_cents,
                total_quote_cents=quote.total_quote_cents,
                ticket_count=quote.ticket_count,
                seat_quotes=quote.seat_quotes,
                status="UNSENT_PREVIEW",
                created_at=record.created_at,
            )
            app.state.quote_preview_store.complete(
                event_digest,
                ingest_status="preview_ready",
                record=preview_record,
                recognition=recognition.model_dump(mode="json"),
                quote=quote.model_dump(mode="json"),
            )
            return QuotePreviewIngestResponse(
                status="preview_ready",
                reply_text=_quote_reply_text(quote, recognition, _quote_reply_template(app.state.plugin_bridge_store.runtime(), quote)),
                quote_unit_cents=quote.unit_quote_cents,
                quote_total_cents=quote.total_quote_cents,
                quote_ticket_count=quote.ticket_count,
            )
        except Exception as error:
            app.state.quote_preview_store.complete(
                event_digest,
                ingest_status="failed",
                record=record,
                recognition=recognition.model_dump(mode="json"),
                failure_stage="quote",
                failure_code=_preview_failure_code(error),
            )
            failure_code = _preview_failure_code(error)
            if _recognition_needs_seat_or_count_confirmation(recognition, request.ticket_count):
                return QuotePreviewIngestResponse(
                    status="needs_confirmation",
                    reply_text="已记录：出票时按您原图圈选的位置操作，无需提供具体座位号。请告诉我需要几张，并发送清晰完整的场次选座页截图，我再按实时优惠核价；若圈选位置届时不可选，会先联系您确认，不会擅自换座。",
                    failure_code=failure_code,
                )
            if failure_code == "http_422":
                return QuotePreviewIngestResponse(
                    status="ignored",
                    reply_text="暂未匹配到该场的实时座位信息，请补充影片名称、日期和具体场次时间后我再核价。",
                    failure_code=failure_code,
                )
            return QuotePreviewIngestResponse(status="failed", failure_code=failure_code)

    @app.post("/api/agents/turn", response_model=AgentTurnResponse)
    async def plan_agent_turn(
        request: AgentTurnRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> AgentTurnResponse:
        configured_key = os.getenv("WANDA_PREVIEW_INGEST_KEY", "")
        if not configured_key or not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, configured_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        try:
            service = app.state.conversation_agent_service
            plan = await (
                service.plan(
                    request,
                    reply_model_settings(),
                    app.state.knowledge_base_store.active_for_agent(classify_agent_scene(request), request.tenant_id),
                )
                if isinstance(service, ConversationAgentService)
                else service.plan(request, reply_model_settings())
            )
            return AgentTurnResponse(status="planned", plan=plan)
        except Exception as error:
            failure_code = _preview_failure_code(error)
            logger.warning("agent planning failed event_id=%s failure_code=%s", request.event_id, failure_code)
            return AgentTurnResponse(status="failed", failure_code=failure_code)

    @app.post("/api/replies/preview-ingest", response_model=ReplyPreviewIngestResponse)
    async def ingest_reply_preview(
        request: ReplyPreviewIngestRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> ReplyPreviewIngestResponse:
        configured_key = os.getenv("WANDA_PREVIEW_INGEST_KEY", "")
        if not configured_key or not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, configured_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

        try:
            service = app.state.reply_preview_service
            draft = await (service.draft(request, reply_model_settings(), app.state.knowledge_base_store.active("reply", request.tenant_id)) if isinstance(service, ReplyPreviewService) else service.draft(request, reply_model_settings()))
            return ReplyPreviewIngestResponse(status="preview_ready", draft=draft)
        except Exception as error:
            failure_code = _preview_failure_code(error)
            logger.warning("reply generation failed event_id=%s failure_code=%s", request.event_id, failure_code)
            return ReplyPreviewIngestResponse(status="failed", failure_code=failure_code)

    return app


QUOTE_REPLY_VARIABLES = frozenset({"城市", "城市标记", "影院", "影片", "日期", "场次", "影厅", "座位", "张数", "单价", "合计", "座位类型", "价格来源", "缺失信息", "排数", "可选座位"})


def _validate_quote_reply_template(value: object) -> str:
    template = value.strip() if isinstance(value, str) else ""
    if not template or len(template) > 500:
        raise HTTPException(status_code=422, detail="invalid quote reply template")
    variables = re.findall(r"\{([^{}]+)\}", template)
    remaining = re.sub(r"\{[^{}]+\}", "", template)
    if "{" in remaining or "}" in remaining or any(variable not in QUOTE_REPLY_VARIABLES for variable in variables):
        raise HTTPException(status_code=422, detail="invalid quote reply template")
    # Reject concrete fabricated facts and affirmative inventory promises, but
    # permit safe wording such as “不为买家保留座位” and “余票以出票时为准”.
    prohibited = r"(?:[¥￥]\s*\d|\d+(?:\.\d{1,2})?\s*(?:元|块)|\d+\s*张|\d+排\s*\d+座|库存充足|余票充足|保证有票|已锁座|已保留座位|承诺出票|出票成功)"
    if re.search(prohibited, remaining):
        raise HTTPException(status_code=422, detail="unsafe quote reply template")
    return template


def _quote_reply_template(runtime: dict[str, object], quote: QuoteRealtimeResponse) -> str:
    templates = runtime.get("reply_templates")
    templates = templates if isinstance(templates, dict) else {}
    if quote.buyer_app_purchase_recommended:
        key = "quote_buyer_app_better_price"
    elif quote.needs_ticket_count:
        key = "quote_need_count"
    elif quote.quote_scope.value == "exact_seats":
        key = "quote_exact"
    else:
        key = "quote_area"
    return str(templates.get(key, DEFAULT_REPLY_TEMPLATES[key]))


def _static_reply_text(template: str) -> str:
    """Buyer-facing failure copy is fully managed by backend reply templates."""
    return template.strip()


def _quote_reply_text(quote: QuoteRealtimeResponse, recognition: Recognition, template: str) -> str:
    """Render merchant text from verified facts without hidden buyer-facing suffixes."""
    if quote.buyer_app_purchase_recommended:
        return _static_reply_text(template)
    if quote.seat_quotes and quote.unit_quote_cents is None:
        parts = [f"{item.seat_number[:24]} {item.unit_quote_cents / 100:.2f}元" for item in quote.seat_quotes]
        detail = "、".join(parts)
        # Standard Wanda labels fit comfortably. For pathological upstream
        # labels, keep the authoritative per-seat facts in seat_quotes and use
        # a bounded grouped summary so response validation cannot drop a quote.
        if len(detail) > 360:
            counts: dict[int, int] = {}
            for item in quote.seat_quotes:
                counts[item.unit_quote_cents] = counts.get(item.unit_quote_cents, 0) + 1
            detail = "、".join(f"{price / 100:.2f}元×{count}" for price, count in sorted(counts.items()))
        total = quote.total_quote_cents or sum(item.unit_quote_cents for item in quote.seat_quotes)
        seats = "、".join(item.seat_number[:24] for item in quote.seat_quotes)
        city = recognition.city or ""
        city_label = f"【{city}的】" if city else ""
        header = (
            f"※{city_label}| {quote.matched_cinema_name or recognition.cinema or ''}\n"
            f"电影：{recognition.movie or ''}\n影厅：{recognition.hall or ''}\n"
            f"场次：{recognition.date.isoformat() if recognition.date else ''} {recognition.showtime or ''}\n座位：{seats}"
        )
        return f"{header}\n\n按官方选座逐座实时核验：{detail}；{len(quote.seat_quotes)}张合计{total / 100:.2f}元。"
    values = {
        "城市": recognition.city or "", "城市标记": f"【{recognition.city}的】" if recognition.city else "",
        "影院": quote.matched_cinema_name or recognition.cinema or "", "影片": recognition.movie or "",
        "日期": recognition.date.isoformat() if recognition.date else "", "场次": recognition.showtime or "",
        "影厅": recognition.hall or "",
        "座位": "、".join(item.seat_number[:24] for item in quote.seat_quotes),
        "张数": str(quote.ticket_count or recognition.official_selection.selected_count or ""),
        # Quotes are executable order amounts. Preserve cents so an operator
        # can enter exactly the same amount during a manual price change.
        "单价": f"{quote.unit_quote_cents / 100:.2f}" if quote.unit_quote_cents is not None else "",
        "合计": f"{quote.total_quote_cents / 100:.2f}" if quote.total_quote_cents is not None else "",
        "座位类型": quote.seat_zone_type.value, "价格来源": quote.pricing_source,
    }
    return re.sub(r"\{([^{}]+)\}", lambda match: values.get(match.group(1), ""), template).strip()


app = create_app()
