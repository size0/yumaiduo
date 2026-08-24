from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, status

from ..plugin_bridge_store import DEFAULT_REPLY_TEMPLATE_IMAGES, DEFAULT_REPLY_TEMPLATES
from ..quote_reply import validate_quote_reply_template
from ..schemas import ConversationExperienceIngestRequest, ModelSettingsUpdate


FULL_AGENT_RUNTIME_VERSION = "wanda-agent-runtime-v34-full-active"


def create_plugin_bridge_router(app: FastAPI) -> APIRouter:
    router = APIRouter()

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
        agent_mode = runtime.get("conversation_agent_mode", "shadow")
        settings: dict[str, object] = {
            **runtime,
            "conversation_agent_mode": agent_mode,
            "conversation_agent_active_ready": True,
            "execution_owner": "agent" if agent_mode == "active" else "deterministic",
            "ai_reply_base_url": str(model.get("base_url", "")),
            "ai_reply_model": str(model.get("model", "")),
            "ai_reply_key_configured": bool(api_key),
            "ai_reply_api_key_masked": f"***{api_key[-4:]}" if api_key else "",
        }
        if account_unb:
            settings["shop_enabled"] = app.state.plugin_bridge_store.shop_enabled(account_unb)
        return settings

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
            "shadow_evaluation_enabled",
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
                patch[field] = value
                if value == "active":
                    # This authenticated operator write is the explicit full
                    # rollout command. Durable Agent owns all buyer IM turns;
                    # platform order lifecycle events remain deterministic.
                    patch.update({
                        "agent_canary_enabled": True,
                        "agent_canary_kill_switch": False,
                        "agent_canary_percentage": 100,
                        "agent_canary_approved": True,
                        "agent_canary_runtime_version": FULL_AGENT_RUNTIME_VERSION,
                        "agent_canary_approved_at": datetime.now(UTC).isoformat(),
                    })
                else:
                    patch.update({
                        "agent_canary_enabled": False,
                        "agent_canary_kill_switch": True,
                        "agent_canary_percentage": 0,
                        "agent_canary_approved": False,
                        "agent_canary_runtime_version": "",
                        "agent_canary_approved_at": None,
                    })
            elif field == "low_confidence_threshold":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"invalid {field}")
                patch[field] = float(value)
            elif field == "reply_templates":
                if not isinstance(value, dict) or set(value) != set(DEFAULT_REPLY_TEMPLATES):
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid reply_templates")
                patch[field] = {key: validate_quote_reply_template(template) for key, template in value.items()}
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

    @router.get("/api/xianyu-plugin/bridge/runtime-settings")
    async def get_plugin_runtime_settings(_: None = Depends(require_plugin_bridge_key)) -> dict[str, object]:
        return {"settings": bridge_runtime_settings()}

    @router.get("/api/xianyu-plugin/bridge/settings")
    async def get_plugin_runtime_settings_for_account(
        account_unb: str | None = None,
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"settings": bridge_runtime_settings(bridge_account_unb(account_unb))}

    @router.put("/api/xianyu-plugin/bridge/shop-settings")
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

    @router.put("/api/xianyu-plugin/bridge/runtime-settings")
    async def update_plugin_runtime_settings(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"settings": patch_runtime_settings(payload)}

    @router.put("/api/xianyu-plugin/bridge/agent-canary-approval")
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
        if isinstance(percentage, bool) or not isinstance(percentage, int) or not 1 <= percentage <= 100:
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

    @router.get("/api/xianyu-plugin/bridge/quote-policy")
    async def get_plugin_quote_policy(
        tenant_id: str | None = None,
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        return {"policy": app.state.plugin_bridge_store.quote_policy(bridge_tenant_id(tenant_id))}

    @router.put("/api/xianyu-plugin/bridge/quote-policy")
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

    @router.post("/api/xianyu-plugin/bridge/shops/sync")
    async def sync_plugin_shops(
        payload: dict[str, object] = Body(...),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, int]:
        bridge_tenant_id(payload.get("tenant_id") if isinstance(payload.get("tenant_id"), str) else None)
        shops = payload.get("shops")
        if not isinstance(shops, list):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid shops")
        return {"synced": len(shops)}

    @router.get("/api/xianyu-plugin/bridge/knowledge-base")
    async def list_knowledge_base(
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        tenant_id = bridge_tenant_id(x_yumaiduo_tenant_id)
        return {"entries": app.state.knowledge_base_store.list(tenant_id)}

    @router.post("/api/xianyu-plugin/bridge/knowledge-base")
    async def create_knowledge_base(
        payload: dict[str, object] = Body(...),
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        _: None = Depends(require_plugin_bridge_key),
    ) -> dict[str, object]:
        try:
            return {"entry": app.state.knowledge_base_store.create(payload, bridge_tenant_id(x_yumaiduo_tenant_id))}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.put("/api/xianyu-plugin/bridge/knowledge-base/{entry_id}")
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

    @router.post("/api/xianyu-plugin/bridge/conversation-experiences", status_code=201)
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

    return router
