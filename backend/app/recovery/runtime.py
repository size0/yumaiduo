from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..recognition_v2.models import RecognitionResult
from ..cinema_route_v2.models import CinemaRouteResult
from ..show_resolve_v2.models import ShowResolutionResult
from ..seat_facts_v2.models import SeatFactsResult
from ..wanda_cost_v2.models import WandaCostFacts
from ..wanda_pricing_v2.models import WandaPricingResult
from .context import QuotePipelineContext
from .models import GateResult
from .orchestrator import QuoteRecoveryOrchestrator
from .reply_gate import reply_eligibility_gate


class RecoveryQuoteRuntime:
    """Gate-native image quote runtime.

    It owns stage progression and keeps the legacy runtime out of the new path.
    Provider/domain authorities remain the injected services and stores.
    """

    def __init__(self, *, recognition_service: Any, route_service: Any, show_service: Any,
                 seat_service: Any, cost_service: Any, pricing_service: Any,
                 quote_service: Any, rules_provider: Callable[[], Any], fact_store: Any | None = None,
                 reply_renderer: Any | None = None) -> None:
        self.recognition = recognition_service
        self.route = route_service
        self.show = show_service
        self.seat = seat_service
        self.cost = cost_service
        self.pricing = pricing_service
        self.quotes = quote_service
        self.rules_provider = rules_provider
        self.fact_store = fact_store
        self.reply_renderer = reply_renderer

    async def process_image_event(self, body: dict[str, Any]) -> dict[str, Any]:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        urls = payload.get("imageUrls", payload.get("image_urls"))
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            return {"status": "NO_IMAGE"}
        identity = self._identity(body)
        if not identity["event_id"] or not identity["shop_id"] or not identity["buyer_id"]:
            return {"status": "IDENTITY_INCOMPLETE"}
        context = QuotePipelineContext(identity=identity)
        fact_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")}
        stored = self.fact_store.load_context(**fact_identity) if self.fact_store is not None else None
        stored_facts = stored.get("facts", {}) if isinstance(stored, Mapping) and stored.get("available") else {}
        state: dict[str, Any] = {"identity": identity, "url": urls[0].strip(), "stored": stored_facts}

        async def recognition_stage(context: QuotePipelineContext) -> GateResult:
            gate = await self.recognition.recognize_gate(state["url"], trace_id=identity["event_id"], idempotency_key=identity["event_id"])
            recognition = RecognitionResult.model_validate(gate.facts)
            context.merge_facts(recognition.model_dump(mode="json"), stored=stored_facts)
            mapping = {"city_text": "city", "cinema_text": "cinema", "show_date": "quote_date", "start_time": "showtime_start"}
            for key in ("city_text", "cinema_text", "movie", "show_date", "start_time", "hall", "dimension", "selected_seats"):
                source_key = mapping.get(key, key)
                if source_key in stored_facts and not getattr(recognition, key, None):
                    recognition = recognition.model_copy(update={key: stored_facts[source_key]})
            state["recognition"] = recognition
            return gate

        async def route_stage(context: QuotePipelineContext) -> GateResult:
            gate = await self.route.resolve_gate(state["recognition"])
            if gate.success:
                state["route"] = CinemaRouteResult.model_validate(gate.facts)
            return gate

        async def show_stage(context: QuotePipelineContext) -> GateResult:
            recognition, route = state["recognition"], state["route"]
            gate = await self.show.resolve_gate({"route": route.route, "wanda_store_id": route.wanda_store_id,
                "movie": recognition.movie, "show_date": recognition.show_date, "start_time": recognition.start_time,
                "hall": recognition.hall, "language": recognition.language, "dimension": recognition.dimension})
            if gate.success:
                state["show"] = ShowResolutionResult.model_validate(gate.facts)
            return gate

        async def seat_stage(context: QuotePipelineContext) -> GateResult:
            route, show, recognition = state["route"], state["show"], state["recognition"]
            gate = await self.seat.resolve_gate({"route": route.route, "wanda_store_id": route.wanda_store_id,
                "wanda_show_id": show.wanda_show_id, "selected_seats": recognition.selected_seats,
                "has_manual_mark": False, "image_url": state["url"]})
            if gate.success:
                state["seat"] = SeatFactsResult.model_validate(gate.facts)
            return gate

        def cost_stage(context: QuotePipelineContext) -> GateResult:
            gate = self.cost.resolve_cost_gate(state["show"], state["seat"])
            if gate.success:
                state["cost"] = WandaCostFacts.model_validate(gate.facts)
            return gate

        def pricing_stage(context: QuotePipelineContext) -> GateResult:
            gate = self.pricing.price_gate(state["cost"], state["show"], state["seat"], self.rules_provider())
            if gate.success:
                state["pricing"] = WandaPricingResult.model_validate(gate.facts)
            return gate

        def quote_stage(context: QuotePipelineContext) -> GateResult:
            route, recognition = state["route"], state["recognition"]
            gate = self.quotes.persist_gate(state["pricing"], state["show"], tenant_id=identity["tenant_id"],
                shop_id=identity["shop_id"], buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                wanda_city_id=route.wanda_city_id or "", cinema_name=route.wanda_cinema_name or recognition.cinema_text or "",
                purchase_context_id=identity["purchase_context_id"], request_id=f'{identity["event_id"]}:recovery-quote',
                event_id=identity["event_id"], recognition_id=recognition.provider_recognize_id, message_id=identity.get("message_id"))
            if gate.success:
                state["quote"] = gate.facts.get("quote_record")
            return gate

        def reply_stage(context: QuotePipelineContext) -> GateResult:
            return reply_eligibility_gate({"status": "QUOTED", "quote": state.get("quote")})

        context, final_gate, decision = await QuoteRecoveryOrchestrator(
            [recognition_stage, route_stage, show_stage, seat_stage, cost_stage, pricing_stage, quote_stage, reply_stage],
            max_steps=8,
        ).run(context)
        if final_gate.gate == "REPLY":
            return {"status": "QUOTED", "quote": state.get("quote"), "reply_gate": final_gate.model_dump(mode="json")}
        return self._safe(final_gate)

    @staticmethod
    def _safe(gate: GateResult) -> dict[str, Any]:
        return {"status": gate.status, "reason": gate.reason_code, "gate": gate.gate,
                "missing_fields": gate.missing_fields, "candidates": gate.candidates}

    @staticmethod
    def _identity(body: Mapping[str, Any]) -> dict[str, str]:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
        def pick(*values: Any) -> str:
            return next((str(v).strip() for v in values if str(v or "").strip()), "")
        return {"event_id": pick(envelope.get("id")), "tenant_id": pick(envelope.get("tenantId")),
                "shop_id": pick(session.get("accountUnb"), payload.get("accountUnb")),
                "buyer_id": pick(session.get("peerUnb"), payload.get("peerUnb")),
                "chat_id": pick(session.get("chatId"), payload.get("chatId")),
                "purchase_context_id": pick(payload.get("itemId")), "message_id": pick(payload.get("messageId"))}
