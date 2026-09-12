from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from datetime import datetime, timedelta
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
from .liangpiao_stages import LiangpiaoStageService
from ..selected_seat_quote_service import QuoteServiceError
from ..pricing.errors import PricingError


class RecoveryQuoteRuntime:
    """Gate-native image quote runtime.

    It owns stage progression and keeps the legacy runtime out of the new path.
    Provider/domain authorities remain the injected services and stores.
    """

    def __init__(self, *, recognition_service: Any, route_service: Any, show_service: Any,
                 seat_service: Any, cost_service: Any, pricing_service: Any,
                 quote_service: Any, rules_provider: Callable[[], Any], fact_store: Any | None = None,
                 reply_renderer: Any | None = None, liangpiao_quote_service: Any | None = None,
                 liangpiao_facts_adapter: Any | None = None, pricing_engine: Any | None = None) -> None:
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
        self.liangpiao_quote = liangpiao_quote_service
        self.liangpiao_facts = liangpiao_facts_adapter
        self.pricing_engine = pricing_engine
        self.liangpiao_stages = (
            LiangpiaoStageService(liangpiao_quote_service, liangpiao_facts_adapter, pricing_engine,
                                  quote_service, rules_provider)
            if liangpiao_quote_service is not None and liangpiao_facts_adapter is not None and pricing_engine is not None
            else None
        )

    async def process_image_event(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._process_image_event(body)

    async def _process_image_event(self, body: dict[str, Any], *, recognition_override: GateResult | None = None,
                                   ticket_count: int | None = None, showtime_ordinal: int | None = None,
                                   candidate_shows: list[Any] | None = None) -> dict[str, Any]:
        envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
        urls = payload.get("imageUrls", payload.get("image_urls"))
        if (not isinstance(urls, list) or not urls or not isinstance(urls[0], str)) and recognition_override is None:
            return {"status": "NO_IMAGE"}
        identity = self._identity(body)
        if not identity["event_id"] or not identity["shop_id"] or not identity["buyer_id"]:
            return {"status": "IDENTITY_INCOMPLETE"}
        context = QuotePipelineContext(identity=identity)
        fact_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
        fact_identity["purchase_context_id"] = identity["purchase_context_id"] or f"chat:{identity['chat_id']}"
        try:
            stored = self.fact_store.load_context(**fact_identity) if self.fact_store is not None else None
        except Exception:
            stored = None
        stored_facts = stored.get("facts", {}) if isinstance(stored, Mapping) and stored.get("available") else {}
        state: dict[str, Any] = {"identity": identity, "url": (urls[0].strip() if isinstance(urls, list) and urls else ""), "stored": stored_facts,
                                 "recognition_gate": recognition_override,
                                 "ticket_count": ticket_count,
                                 "showtime_ordinal": showtime_ordinal,
                                 "candidate_shows": candidate_shows or stored_facts.get("candidate_shows") or [],
                                 "liangpiao_request": None, "liangpiao_preflight": None,
                                 "liangpiao_cost": None, "liangpiao_pricing": None}

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
            stored_compare = {"cinema": stored_facts.get("cinema"), "movie": stored_facts.get("movie"),
                              "quote_date": stored_facts.get("quote_date", stored_facts.get("date")),
                              "showtime_start": stored_facts.get("showtime_start", stored_facts.get("show"))}
            changed_selection = any(current_facts.get(key) not in (None, "", []) and
                                    stored_compare.get(key) not in (None, "", []) and
                                    current_facts.get(key) != stored_compare.get(key)
                                    for key in stored_compare)
            for key in ("city_text", "cinema_text", "movie", "show_date", "start_time", "hall", "dimension", "selected_seats"):
                if changed_selection and key in {"hall", "selected_seats"}:
                    continue
                source_key = mapping.get(key, key)
                relative_selection = bool(state.get("showtime_ordinal")) or (
                    bool(getattr(recognition, "dimension", None))
                    and getattr(recognition, "dimension", None) != stored_facts.get("dimension")
                )
                if source_key == "showtime_start" and relative_selection:
                    continue
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
            if route.route == "LIANGPIAO":
                if self.liangpiao_quote is not None and self.liangpiao_facts is not None and self.pricing_engine is not None:
                    self.liangpiao_stages = LiangpiaoStageService(
                        self.liangpiao_quote, self.liangpiao_facts, self.pricing_engine,
                        self.quotes, self.rules_provider,
                    )
                stages = self.liangpiao_stages
                if stages is None:
                    return GateResult(gate="SHOW", status="PROVIDER_UNAVAILABLE", success=False,
                                      safety_class="RECOVERABLE", reason_code="LIANGPIAO_STAGE_UNAVAILABLE")
                try:
                    state["liangpiao_request"] = stages.prepare_request(recognition, route, identity)
                except ValueError as error:
                    return GateResult(gate="SHOW", status="NEED_CLARIFICATION", success=False,
                                      safety_class="RECOVERABLE", reason_code=str(error), missing_fields=["selected_seats"])
                except QuoteServiceError as error:
                    return GateResult(gate="SHOW", status="PROVIDER_UNAVAILABLE", success=False,
                                      safety_class="RECOVERABLE", reason_code=error.code)
                try:
                    preflight = await stages.preflight(state["liangpiao_request"])
                    if not str(getattr(preflight, "show_id", "") or "").strip():
                        return GateResult(gate="SHOW", status="SHOW_UNRESOLVED", success=False,
                                          safety_class="RECOVERABLE", reason_code="LIANGPIAO_SHOW_ID_REQUIRED")
                    state["liangpiao_preflight"] = preflight
                except (QuoteServiceError, PricingError, ValueError) as error:
                    return GateResult(gate="SHOW", status="PROVIDER_UNAVAILABLE", success=False,
                                      safety_class="RECOVERABLE", reason_code=getattr(error, "code", type(error).__name__))
                return GateResult(gate="SHOW", status="RESOLVED", success=True, safety_class="RECOVERABLE",
                                  facts={"provider_route": "LIANGPIAO", "show_id": preflight.show_id},
                                  provider_verified=True)
            gate = await self.show.resolve_gate({"route": route.route, "wanda_store_id": route.wanda_store_id,
                "movie": recognition.movie, "show_date": recognition.show_date, "start_time": recognition.start_time,
                "hall": recognition.hall, "language": recognition.language, "dimension": recognition.dimension,
                "showtime_ordinal": state.get("showtime_ordinal"), "candidate_shows": state.get("candidate_shows", [])})
            if gate.candidates:
                state["candidate_shows"] = list(gate.candidates)
            if gate.success:
                state["show"] = ShowResolutionResult.model_validate(gate.facts)
            return gate

        async def seat_stage(context: QuotePipelineContext) -> GateResult:
            if state["route"].route == "LIANGPIAO":
                preflight = state.get("liangpiao_preflight")
                seats = getattr(preflight, "seats", []) if preflight is not None else []
                return GateResult(gate="SEAT", status="EXACT_SEATS_RESOLVED", success=bool(seats),
                                  safety_class="RECOVERABLE", facts={"selected_seats": [seat.model_dump(mode="json") for seat in seats]},
                                  reason_code=None if seats else "SELECTED_SEATS_REQUIRED")
            route, show, recognition = state["route"], state["show"], state["recognition"]
            gate = await self.seat.resolve_gate({"route": route.route, "wanda_store_id": route.wanda_store_id,
                "wanda_show_id": show.wanda_show_id, "selected_seats": recognition.selected_seats,
                "has_manual_mark": False, "image_url": state["url"]})
            if gate.success:
                state["seat"] = SeatFactsResult.model_validate(gate.facts)
            return gate

        def cost_stage(context: QuotePipelineContext) -> GateResult:
            if state["route"].route == "LIANGPIAO":
                try:
                    state["liangpiao_cost"] = self.liangpiao_stages.cost_facts(
                        state["liangpiao_preflight"], state["liangpiao_request"],
                    )
                    return GateResult(gate="COST", status="COST_READY", success=True,
                                      safety_class="RECOVERABLE", facts={"provider": "LIANGPIAO"}, provider_verified=True)
                except (QuoteServiceError, PricingError, ValueError) as error:
                    return GateResult(gate="COST", status="COST_UNAVAILABLE", success=False,
                                      safety_class="RECOVERABLE", reason_code=getattr(error, "code", type(error).__name__))
            gate = self.cost.resolve_cost_gate(state["show"], state["seat"])
            if gate.success:
                state["cost"] = WandaCostFacts.model_validate(gate.facts)
            return gate

        def pricing_stage(context: QuotePipelineContext) -> GateResult:
            if state["route"].route == "LIANGPIAO":
                try:
                    state["liangpiao_pricing"] = self.liangpiao_stages.price(state["liangpiao_cost"])
                    return GateResult(gate="PRICING", status="PRICED", success=True,
                                      safety_class="RECOVERABLE", facts={"provider": "LIANGPIAO"}, provider_verified=True)
                except (QuoteServiceError, PricingError, ValueError) as error:
                    return GateResult(gate="PRICING", status="PRICING_FAILED", success=False,
                                      safety_class="RECOVERABLE", reason_code=getattr(error, "code", type(error).__name__))
            gate = self.pricing.price_gate(state["cost"], state["show"], state["seat"], self.rules_provider(), ticket_count=state.get("ticket_count"))
            if gate.success:
                state["pricing"] = WandaPricingResult.model_validate(gate.facts)
            return gate

        def quote_stage(context: QuotePipelineContext) -> GateResult:
            if state["route"].route == "LIANGPIAO":
                try:
                    record = self.liangpiao_stages.persist(
                        state["liangpiao_pricing"], state["liangpiao_preflight"], state["liangpiao_request"],
                        state["recognition"], identity,
                    )
                except Exception as error:
                    state["quote_persist_status"] = "QUOTE_PERSIST_FAILED"
                    return GateResult(gate="QUOTE", status="QUOTE_PERSIST_FAILED", success=False,
                                      safety_class="RECOVERABLE", reason_code=type(error).__name__)
                if not record:
                    state["quote_persist_status"] = "QUOTE_PERSIST_FAILED"
                    return GateResult(gate="QUOTE", status="QUOTE_PERSIST_FAILED", success=False,
                                      safety_class="RECOVERABLE", reason_code="LIANGPIAO_QUOTE_NOT_PERSISTED")
                state["quote"] = record
                state["quote_persist_status"] = "QUOTE_PERSISTED"
                context.quote_generation = record.get("generation") if isinstance(record, dict) else None
                return GateResult(gate="QUOTE", status="QUOTE_PERSISTED", success=True, safety_class="RECOVERABLE",
                                  facts={"quote_record": record}, provider_verified=True)
            route, recognition = state["route"], state["recognition"]
            gate = self.quotes.persist_gate(state["pricing"], state["show"], tenant_id=identity["tenant_id"],
                shop_id=identity["shop_id"], buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                wanda_city_id=route.wanda_city_id or "", cinema_name=route.wanda_cinema_name or recognition.cinema_text or "",
                purchase_context_id=identity["purchase_context_id"], request_id=f'{identity["event_id"]}:recovery-quote',
                event_id=identity["event_id"], recognition_id=recognition.provider_recognize_id, message_id=identity.get("message_id"))
            if gate.success:
                state["quote"] = gate.facts.get("quote_record")
                record_generation = (state["quote"] or {}).get("generation") if isinstance(state.get("quote"), dict) else None
                if isinstance(record_generation, int):
                    context.quote_generation = record_generation
            state["quote_persist_status"] = gate.status
            return gate

        def reply_stage(context: QuotePipelineContext) -> GateResult:
            if state["route"].route == "LIANGPIAO":
                preflight = state.get("liangpiao_preflight")
                return reply_eligibility_gate({"status": "QUOTED", "quote": state.get("quote"),
                    "quote_persist_status": state.get("quote_persist_status"), "identity": identity,
                    "verified_show_id": getattr(preflight, "show_id", ""),
                    "pipeline_generation": context.quote_generation})
            show = state.get("show")
            return reply_eligibility_gate({
                "status": "QUOTED", "quote": state.get("quote"),
                "quote_persist_status": state.get("quote_persist_status"),
                "identity": identity,
                "verified_show_id": getattr(show, "wanda_show_id", ""),
                "pipeline_generation": context.quote_generation,
            })

        context, final_gate, decision = await QuoteRecoveryOrchestrator(
            [recognition_stage, route_stage, show_stage, seat_stage, cost_stage, pricing_stage, quote_stage, reply_stage],
            max_steps=8,
            max_recovery_attempts=1,
            retry_handlers={"COST": lambda ctx, result: cost_stage(ctx), "PRICING": lambda ctx, result: pricing_stage(ctx)},
        ).run(context)
        self._persist_facts(identity, context, state, final_gate)
        if final_gate.gate == "REPLY" and final_gate.success:
            result = {"status": "QUOTED", "quote": state.get("quote"), "reply_gate": final_gate.model_dump(mode="json"),
                      "recognition": state.get("recognition").model_dump(mode="json"),
                      "conversation_facts": dict(context.conversation_facts),
                      "invalidated_fields": sorted(context.stale_fields), "generation": context.generation,
                      "quote_generation": context.quote_generation}
            if self.reply_renderer is not None:
                rendered = self.reply_renderer.render(result)
                result.update({"current_runtime_reply": rendered.get("text"), "canonical_reply_kind": rendered.get("kind")})
            return result
        if final_gate.gate == "REPLY":
            return self._safe(final_gate, context)
        return self._safe(final_gate, context)

    def _persist_facts(self, identity: Mapping[str, str], context: QuotePipelineContext,
                       state: Mapping[str, Any], final_gate: GateResult) -> None:
        """Best-effort durable write of the current conversation facts.

        Facts are input/context evidence only.  QuoteRecord remains the amount
        authority; this write exists so a follow-up image can reuse identity
        and invalidate stale downstream facts across process boundaries.
        """
        if self.fact_store is None:
            return
        try:
            facts = dict(context.conversation_facts)
            candidates = state.get("candidate_shows")
            if candidates:
                facts["candidate_shows"] = list(candidates)
            if state.get("ticket_count") is not None:
                facts["ticket_count"] = state["ticket_count"]
            show = state.get("show")
            show_id = getattr(show, "wanda_show_id", None)
            if show_id:
                facts["show_id"] = show_id
            quote = state.get("quote")
            if isinstance(quote, Mapping) and quote.get("record_id"):
                facts["quote_record"] = {"record_id": quote["record_id"], "generation": quote.get("generation")}
            invalidated: set[str] = set(context.stale_fields)
            dependency_to_fact = {
                "show": "show_id", "show_id": "show_id", "seat_facts": "selected_seats",
                "seat": "selected_seats", "cost": "cost", "pricing": "pricing",
                "quote_record": "quote_record",
            }
            from .invalidation import invalidated_fields
            for changed in context.invalidated_fields:
                invalidated.update(
                    dependency_to_fact.get(dependent, dependent)
                    for dependent in invalidated_fields(changed)
                )
            # A dependency is stale only when this run did not replace it
            # with a newly verified fact.  Never delete the current show or
            # quote while clearing the predecessor's lineage.
            invalidated.difference_update(key for key in facts if key in invalidated)
            fact_purchase_context_id = identity["purchase_context_id"] or f"chat:{identity['chat_id']}"
            self.fact_store.save(
                tenant_id=identity["tenant_id"], shop_id=identity["shop_id"],
                buyer_id=identity["buyer_id"], chat_id=identity["chat_id"],
                purchase_context_id=fact_purchase_context_id, facts=facts,
                invalidated_fields=invalidated, source="recovery_runtime",
                fact_tier="verified" if final_gate.success else "candidate",
                event_id=identity["event_id"], message_id=identity.get("message_id"),
            )
        except Exception:
            # Fact persistence must never change quote decisions or reply safety.
            return

    async def process_text_event(self, body: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(body)
        fact_identity = {key: identity[key] for key in ("tenant_id", "shop_id", "buyer_id", "chat_id")}
        fact_identity["purchase_context_id"] = identity["purchase_context_id"] or f"chat:{identity['chat_id']}"
        try:
            stored = self.fact_store.load_context(**fact_identity) if self.fact_store is not None else None
        except Exception:
            stored = None
        facts = stored.get("facts", {}) if isinstance(stored, Mapping) and stored.get("available") else {}
        payload = body.get("envelope", {}).get("payload", {}) if isinstance(body.get("envelope"), Mapping) else {}
        text = str(payload.get("text") or payload.get("content") or body.get("text") or "").strip()
        if any(token in text for token in ("不要了", "不用了", "取消", "算了")):
            return {"status": "NON_AMOUNT_REPLY_ALLOWED", "reason": "BUYER_CANCELLED"}
        if not facts:
            return {"status": "NEED_CLARIFICATION", "missing_fields": ["cinema", "movie", "date", "showtime"]}
        time_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:点|时|:)[ ]*(\d{1,2})?", text)
        if time_match is None:
            chinese_time = re.search(r"([一二两三四五六七八九十\d]+)点(半|[一二三四五六七八九十\d]+)?", text)
            if chinese_time:
                digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                hour = digits.get(chinese_time.group(1), int(chinese_time.group(1)) if chinese_time.group(1).isdigit() else 0)
                minute = 30 if chinese_time.group(2) == "半" else digits.get(chinese_time.group(2), 0) if chinese_time.group(2) else 0
                time_match = (hour, minute)
        count_match = re.search(r"([一二两三四五六七八九十]|\d+)\s*(?:张|票|人)", text)
        count_words = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if isinstance(time_match, tuple):
            start_time = f"{time_match[0]:02d}:{time_match[1]:02d}"
        else:
            start_time = f"{int(time_match.group(1)):02d}:{int(time_match.group(2) or 0):02d}" if time_match else facts.get("showtime_start")
        quote_date = facts.get("quote_date") or facts.get("date")
        ordinal_match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*场", text)
        ordinal_words = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        showtime_ordinal = (ordinal_words.get(ordinal_match.group(1), int(ordinal_match.group(1)) if ordinal_match.group(1).isdigit() else None)
                            if ordinal_match else facts.get("showtime_ordinal"))
        if ordinal_match or "imax" in text.lower():
            # Relative/dimension references select from the provider show list;
            # an old absolute time must not override that selection.
            start_time = None
        if "明天" in text:
            quote_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        elif "后天" in text:
            quote_date = (datetime.now().date() + timedelta(days=2)).isoformat()
        patch_signals = time_match or count_match or any(token in text for token in ("第二场", "那场", "IMAX", "场次", "多少钱", "价格", "明天", "后天"))
        if not patch_signals:
            return {"status": "NON_AMOUNT_REPLY_ALLOWED", "reason": "TEXT_NOT_QUOTE_PATCH"}
        dimension = "IMAX" if "imax" in text.lower() else facts.get("dimension")
        recognition = RecognitionResult(city_text=facts.get("city"), cinema_text=facts.get("cinema"), movie=facts.get("movie"),
            show_date=quote_date, start_time=start_time,
            hall=facts.get("hall"), dimension=dimension, selected_seats=list(facts.get("selected_seats") or []),
            has_selected_seats=bool(facts.get("selected_seats")))
        # Re-enter the same orchestration directly with a recognition patch;
        # text follow-ups are not fabricated as image events.
        followup = {"envelope": {"id": identity["event_id"], "tenantId": identity["tenant_id"], "payload": {"itemId": identity["purchase_context_id"]}},
                     "session": {"accountUnb": identity["shop_id"], "peerUnb": identity["buyer_id"], "chatId": identity["chat_id"]},
                     "_recovery_ticket_count": (count_words.get(count_match.group(1)) if count_match else facts.get("ticket_count")),
                     "_recovery_showtime_ordinal": showtime_ordinal,
                     "_recovery_candidate_shows": facts.get("candidate_shows") or []}
        return await self._process_image_event(followup, recognition_override=GateResult(
            gate="RECOGNITION", status="PARTIAL", success=True,
            safety_class="RECOVERABLE", facts=recognition.model_dump(mode="json"),
        ), ticket_count=(count_words.get(count_match.group(1)) if count_match else facts.get("ticket_count")),
            showtime_ordinal=showtime_ordinal, candidate_shows=facts.get("candidate_shows") or [])

    def _safe(self, gate: GateResult, context: QuotePipelineContext | None = None) -> dict[str, Any]:
        result = {"status": gate.status, "reason": gate.reason_code, "gate": gate.gate,
                  "missing_fields": gate.missing_fields, "candidates": gate.candidates}
        reply_gate = reply_eligibility_gate(result)
        result["reply_gate"] = reply_gate.model_dump(mode="json")
        if context is not None:
            result.update({"conversation_facts": dict(context.conversation_facts),
                           "invalidated_fields": sorted(context.stale_fields),
                           "generation": context.generation, "quote_generation": context.quote_generation})
        if self.reply_renderer is not None:
            rendered = self.reply_renderer.render(result)
            result.update({"current_runtime_reply": rendered.get("text"), "canonical_reply_kind": rendered.get("kind")})
        return result

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
