from pathlib import Path

import pytest

from app.cinema_route_v2.models import CinemaRouteResult
from app.conversation_fact_store import ConversationFactStore
from app.recovery.models import GateResult, SafetyClass
from app.recovery.runtime import RecoveryQuoteRuntime
from app.recognition_v2.models import RecognitionResult
from app.seat_facts_v2.models import SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts
from app.wanda_pricing_v2.models import WandaPricingResult


def gate(name, status, facts, success=True):
    return GateResult(gate=name, status=status, success=success,
                      safety_class=SafetyClass.RECOVERABLE, facts=facts)


class FakeRecognition:
    async def recognize_gate(self, *args, **kwargs):
        return gate("RECOGNITION", "RECOGNIZED", RecognitionResult(
            city_text="合肥", cinema_text="万达影城", movie="奥德赛", show_date="2026-09-13",
            start_time="14:30", selected_seats=[], has_selected_seats=False,
        ).model_dump(mode="json"))


class FakeRoute:
    async def resolve_gate(self, recognition):
        return gate("CINEMA_ROUTE", "WANDA_SELF", CinemaRouteResult(
            route="WANDA_SELF", wanda_store_id="store-1", wanda_city_id="city-1",
            wanda_city_name="合肥", wanda_cinema_name="万达影城", resolution_reason="UNIQUE",
        ).model_dump(mode="json"))


class FakeShow:
    async def resolve_gate(self, request):
        return gate("SHOW", "RESOLVED", ShowResolutionResult(
            status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
            movie_name="奥德赛", show_date="2026-09-13", start_time="14:30",
            resolution_reason="UNIQUE",
        ).model_dump(mode="json"))


class FakeSeat:
    async def resolve_gate(self, request):
        return gate("SEAT", "WPLUS_AREA_RESOLVED", SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="EXACT_SEATS",
            wanda_store_id="store-1", wanda_show_id="show-1",
        ).model_dump(mode="json"))


class FakeCost:
    def resolve_cost_gate(self, show, seat):
        return gate("COST", "COST_READY", WandaCostFacts(
            status="COST_READY", request_type="WPLUS_AREA",
        ).model_dump(mode="json"))


class FakePricing:
    def price_gate(self, cost, show, seat, rules, **kwargs):
        return gate("PRICING", "PRICED", WandaPricingResult(
            status="PRICED", request_type="WPLUS_AREA", unit_sell_price_fen=4500,
            total_sell_price_fen=4500, ticket_count=1,
        ).model_dump(mode="json"))


class FakeQuotes:
    def persist_gate(self, *args, **kwargs):
        return gate("QUOTE", "QUOTE_PERSISTED", {"quote_record": {"record_id": "record-1", "total": 4500}})


@pytest.mark.asyncio
async def test_recovery_runtime_runs_full_quote_path(tmp_path: Path):
    runtime = RecoveryQuoteRuntime(
        recognition_service=FakeRecognition(), route_service=FakeRoute(), show_service=FakeShow(),
        seat_service=FakeSeat(), cost_service=FakeCost(), pricing_service=FakePricing(),
        quote_service=FakeQuotes(), rules_provider=lambda: object(),
        fact_store=ConversationFactStore(tmp_path / "facts.sqlite"),
    )
    result = await runtime.process_image_event({
        "envelope": {"id": "event-1", "tenantId": "tenant", "payload": {"imageUrls": ["https://example/image"]}},
        "session": {"accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat"},
    })
    assert result["status"] == "QUOTED"
    assert result["reply_gate"]["status"] == "AMOUNT_REPLY_ALLOWED"


@pytest.mark.asyncio
async def test_recovery_runtime_stops_without_amount_when_route_unresolved():
    class UnresolvedRoute(FakeRoute):
        async def resolve_gate(self, recognition):
            return gate("CINEMA_ROUTE", "CINEMA_REQUIRED", {}, success=False)

    runtime = RecoveryQuoteRuntime(
        recognition_service=FakeRecognition(), route_service=UnresolvedRoute(), show_service=FakeShow(),
        seat_service=FakeSeat(), cost_service=FakeCost(), pricing_service=FakePricing(),
        quote_service=FakeQuotes(), rules_provider=lambda: object(),
    )
    result = await runtime.process_image_event({
        "envelope": {"id": "event-2", "tenantId": "tenant", "payload": {"imageUrls": ["https://example/image"]}},
        "session": {"accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat"},
    })
    assert result["status"] == "CINEMA_REQUIRED"


@pytest.mark.asyncio
async def test_text_followup_uses_same_conversation_facts(tmp_path: Path):
    facts_store = ConversationFactStore(tmp_path / "facts.sqlite")
    facts_store.save(tenant_id="tenant", shop_id="shop", buyer_id="buyer", chat_id="chat",
                     purchase_context_id="item", facts={"city": "合肥", "cinema": "万达影城", "movie": "奥德赛",
                     "quote_date": "2026-09-13", "showtime_start": "14:30"}, source="test", fact_tier="verified")
    runtime = RecoveryQuoteRuntime(
        recognition_service=FakeRecognition(), route_service=FakeRoute(), show_service=FakeShow(),
        seat_service=FakeSeat(), cost_service=FakeCost(), pricing_service=FakePricing(),
        quote_service=FakeQuotes(), rules_provider=lambda: object(), fact_store=facts_store,
    )
    result = await runtime.process_text_event({
        "text": "两张", "envelope": {"id": "event-text", "tenantId": "tenant", "payload": {"itemId": "item", "text": "两张"}},
        "session": {"accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat"},
    })
    assert result["status"] == "QUOTED"
