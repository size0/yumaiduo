from __future__ import annotations

from collections.abc import Callable, Mapping
import re
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
        try:
            stored = self.fact_store.load_context(**fact_identity) if self.fact_store is not None else None
        except Exception:
            stored = None
        stored_facts = stored.get("facts", {}) if isinstance(stored, Mapping) and stored.get("available") else {}
        state: dict[str, Any] = {"identity": identity, "url": urls[0].strip(), "stored": stored_facts,
                                 "recognition_gate": body.get("_recovery_recognition_gate"),
                                 "ticket_count": body.get("_recovery_ticket_count")}

        async def recognition_stage(context: QuotePipelineContext) -> GateResult:
            gate = state["recognition_gate"] or await self.recognition.recognize_gate(
                state["url"], trace_id=identity["event_id"], idempotency_key=identity["event_id"],
            )
            recognition = RecognitionResult.model_validate(gate.facts)
            current_facts = {"city": recognition.city_text, "cinema": recognition.cinema_text,
                             "movie": recognition.movie, "quote_date": recognition.show_date,
                             "showtime_start": recognition.start_time, "hall": recognition.hall,
                             "dimension": recognition.dimension, "selected_seats": recognition.selected_seats}
            context.merge_facts({key: value for key, value in current_facts.items() if value not in (None, "", [])}, stored=stored_facts)
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
            gate = self.pricing.price_gate(state["cost"], state["show"], state["seat"], self.rules_provider(), ticket_count=state.get("ticket_count"))
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
            max_recovery_attempts=1,
            retry_handlers={"COST": lambda ctx, result: cost_stage(ctx), "PRICING": lambda ctx, result: pricing_stage(ctx)},
        ).run(context)
        if final_gate.gate == "REPLY":
            result = {"status": "QUOTED", "quote": state.get("quote"), "reply_gate": final_gate.model_dump(mode="json")}
            if self.reply_renderer is not None:
                rendered = self.reply_renderer.render(result)
                result.update({"current_runtime_reply": rendered.get("text"), "canonical_reply_kind": rendered.get("kind")})
            return result
        return self._safe(final_gate)

    async def process_text_event(self, body: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(body)
        fact_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id", "purchase_context_id")}
        try:
            stored = self.fact_store.load_context(**fact_identity) if self.fact_store is not None else None
        except Exception:
            stored = None
        facts = stored.get("facts", {}) if isinstance(stored, Mapping) and stored.get("available") else {}
        payload = body.get("envelope", {}).get("payload", {}) if isinstance(body.get("envelope"), Mapping) else {}
        text = str(payload.get("text") or payload.get("content") or body.get("text") or "").strip()
        if not facts:
            return {"status": "NEED_CLARIFICATION", "missing_fields": ["cinema", "movie", "date", "showtime"]}
        time_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:点|时|:)[ ]*(\d{1,2})?", text)
        count_match = re.search(r"([一二两三四五六七八九十]|\d+)\s*(?:张|票|人)", text)
        count_words = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        recognition = RecognitionResult(city_text=facts.get("city"), cinema_text=facts.get("cinema"), movie=facts.get("movie"),
            show_date=facts.get("quote_date") or facts.get("date"), start_time=(f"{int(time_match.group(1)):02d}:{int(time_match.group(2) or 0):02d}" if time_match else facts.get("showtime_start")),
            hall=facts.get("hall"), dimension=facts.get("dimension"), selected_seats=list(facts.get("selected_seats") or []),
            has_selected_seats=bool(facts.get("selected_seats")))
        synthetic = {"envelope": {"id": identity["event_id"], "tenantId": identity["tenant_id"], "payload": {"imageUrls": ["about:blank"]}},
                     "session": {"accountUnb": identity["shop_id"], "peerUnb": identity["buyer_id"], "chatId": identity["chat_id"]},
                     "_recovery_recognition_gate": GateResult(gate="RECOGNITION", status="PARTIAL", success=True,
                         safety_class="RECOVERABLE", facts=recognition.model_dump(mode="json")),
                     "_recovery_ticket_count": (count_words.get(count_match.group(1)) if count_match else facts.get("ticket_count"))}
        return await self.process_image_event(synthetic)

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
