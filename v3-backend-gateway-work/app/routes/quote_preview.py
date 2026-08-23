from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException, status

from ..conversation_agent import ConversationAgentService, QUOTE_FACT_PROMPT_VERSION
from ..plugin_bridge_store import DEFAULT_REPLY_TEMPLATES
from ..quote_preview_store import QuotePreviewStore, empty_pending_record
from ..quote_preview_support import (
    _buyer_app_has_lower_price,
    _preview_failure_code,
    _recognition_needs_seat_or_count_confirmation,
    _recognize_preview_image,
)
from ..quote_reply import quote_reply_template, quote_reply_text, static_reply_text
from ..schemas import (
    AvailableWplusSeatsRequest, AvailableWplusSeatsResponse, ImageType, PendingQuoteRecord,
    PendingQuotesResponse, QuoteMatchCandidateRequest, QuoteMatchCandidateResponse,
    QuotePreviewIngestRequest, QuotePreviewIngestResponse, QuotePreviewQuoteRequest,
    QuoteRealtimeRequest, QuoteRealtimeResponse, QuoteShowtimeResolveRequest,
    QuoteShowtimeResolveResponse, QuoteTextFactExtractRequest, QuoteTextFactExtractResponse,
    VisionRecognizeRequest, VisionRecognizeResponse,
)
from ..vision import PROMPT_VERSION, VisionFailure

logger = logging.getLogger(__name__)


def create_quote_preview_router(app: FastAPI, reply_model_settings: Callable[[], dict[str, object]]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/quotes/preview-extract-text", response_model=QuoteTextFactExtractResponse)
    async def preview_extract_text_facts(
        request: QuoteTextFactExtractRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuoteTextFactExtractResponse:
        configured_key = os.getenv("WANDA_PREVIEW_INGEST_KEY", "")
        if not configured_key or not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, configured_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        try:
            service = app.state.conversation_agent_service
            if not isinstance(service, ConversationAgentService):
                return QuoteTextFactExtractResponse(status="failed", extractor_version=QUOTE_FACT_PROMPT_VERSION, failure_code="semantic_extractor_unavailable")
            facts = await service.extract_quote_facts(request, reply_model_settings())
            return QuoteTextFactExtractResponse(status="extracted", extractor_version=QUOTE_FACT_PROMPT_VERSION, facts=facts)
        except Exception as error:
            failure_code = _preview_failure_code(error)
            logger.warning("quote text fact extraction failed event_id=%s failure_code=%s", request.event_id, failure_code)
            return QuoteTextFactExtractResponse(status="failed", extractor_version=QUOTE_FACT_PROMPT_VERSION, failure_code=failure_code)

    @router.post("/api/quotes/preview-recognize", response_model=VisionRecognizeResponse)
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

    @router.post("/api/quotes/preview-quote", response_model=QuoteRealtimeResponse)
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
            raise HTTPException(status_code=error.status_code, detail={**detail, "code": code, "reply_text": static_reply_text(str(template))}) from error
        recommend_buyer_app = _buyer_app_has_lower_price(quote, request.recognition)
        policy_version = "quote-policy-" + hashlib.sha256(
            json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        quote = quote.model_copy(update={
            "buyer_app_purchase_recommended": recommend_buyer_app,
            "pricing_rule_version": policy_version,
        })
        return quote.model_copy(update={"reply_text": quote_reply_text(quote, request.recognition, quote_reply_template(app.state.plugin_bridge_store.runtime(), quote))})

    @router.post("/api/quotes/preview-resolve-showtime", response_model=QuoteShowtimeResolveResponse)
    async def preview_resolve_showtime(
        request: QuoteShowtimeResolveRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> QuoteShowtimeResolveResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        recognition = app.state.local_catalog.canonicalize(request.recognition)
        return await app.state.quote_service.resolve_showtime(recognition)

    @router.post("/api/quotes/preview-available-seats", response_model=AvailableWplusSeatsResponse)
    async def preview_available_wplus_seats(
        request: AvailableWplusSeatsRequest,
        x_wanda_preview_key: str | None = Header(default=None, alias="X-Wanda-Preview-Key"),
    ) -> AvailableWplusSeatsResponse:
        if not x_wanda_preview_key or not hmac.compare_digest(x_wanda_preview_key, os.getenv("WANDA_PREVIEW_INGEST_KEY", "")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        recognition = app.state.local_catalog.canonicalize(request.recognition)
        return await app.state.quote_service.available_wplus_seats(recognition, request.row)

    @router.post("/api/quotes/preview-resolve-candidates", response_model=QuoteMatchCandidateResponse)
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

    @router.get("/api/quotes/pending", response_model=PendingQuotesResponse)
    async def get_pending_quotes(tenant_id: str | None = None) -> PendingQuotesResponse:
        return PendingQuotesResponse(records=app.state.quote_preview_store.pending(tenant_id))

    @router.post("/api/quotes/preview-ingest", response_model=QuotePreviewIngestResponse)
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
                reply_text=quote_reply_text(quote, recognition, quote_reply_template(app.state.plugin_bridge_store.runtime(), quote)),
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

    return router
