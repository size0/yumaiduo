from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException, status

from ..conversation_agent import ConversationAgentService, classify_agent_scene
from ..quote_preview_support import _preview_failure_code
from ..reply_preview import ReplyPreviewService
from ..schemas import AgentTurnRequest, AgentTurnResponse, ReplyPreviewIngestRequest, ReplyPreviewIngestResponse

logger = logging.getLogger(__name__)


def create_agent_reply_router(app: FastAPI, reply_model_settings: Callable[[], dict[str, object]]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/agents/turn", response_model=AgentTurnResponse)
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

    @router.post("/api/replies/preview-ingest", response_model=ReplyPreviewIngestResponse)
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

    return router
