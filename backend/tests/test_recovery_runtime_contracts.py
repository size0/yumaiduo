from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.conversation_fact_store import ConversationFactStore
from app.recovery.models import GateResult, SafetyClass
from app.recovery.runtime import RecoveryQuoteRuntime
from app.recognition_v2.models import RecognitionResult
from app.cinema_route_v2.models import CinemaRouteResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.seat_facts_v2.models import SeatFactsResult
from app.wanda_cost_v2.models import WandaCostFacts
from app.wanda_pricing_v2.models import WandaPricingResult
from app.selected_seat_quote_service import SelectedSeatQuoteResult


def _gate(gate: str, status: str, facts=None, *, success=True, retryable=False):
    return GateResult(
        gate=gate, status=status, success=success,
        safety_class=SafetyClass.RECOVERABLE,
        facts=facts or {}, retryable=retryable,
    )


def _recognition(seats=None, dimension=None):
    return RecognitionResult(
        city_text="合肥", cinema_text="万达影城", movie="奥德赛",
        show_date="2026-09-13", start_time="14:30", dimension=dimension,
        selected_seats=list(seats or []), has_selected_seats=bool(seats),
    ).model_dump(mode="json")


class RecordingRecognition:
    def __init__(self, seats=None, dimension=None):
        self.calls = []
        self.seats, self.dimension = seats, dimension

    async def recognize_gate(self, image, **kwargs):
        self.calls.append((image, kwargs))
        return _gate("RECOGNITION", "RECOGNIZED", _recognition(self.seats, self.dimension))


class RecordingRoute:
    def __init__(self): self.calls = []

    async def resolve_gate(self, recognition):
        self.calls.append(recognition)
        return _gate("CINEMA_ROUTE", "WANDA_SELF", CinemaRouteResult(
            route="WANDA_SELF", wanda_store_id="store-1", wanda_city_id="city-1",
            wanda_city_name="合肥", wanda_cinema_name="万达影城", resolution_reason="UNIQUE",
        ).model_dump(mode="json"))


class RecordingShow:
    def __init__(self, status="RESOLVED"):
        self.calls, self.status = [], status

    async def resolve_gate(self, request):
        self.calls.append(request)
        if self.status != "RESOLVED":
            return _gate("SHOW", self.status, {}, success=False)
        return _gate("SHOW", "RESOLVED", ShowResolutionResult(
            status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
            movie_name=request.get("movie"), show_date=request.get("show_date"),
            start_time=request.get("start_time"), resolution_reason="UNIQUE",
        ).model_dump(mode="json"))


class RecordingSeat:
    def __init__(self, status="WPLUS_AREA_RESOLVED"):
        self.calls, self.status = [], status

    async def resolve_gate(self, request):
        self.calls.append(request)
        if self.status != "WPLUS_AREA_RESOLVED":
            return _gate("SEAT", self.status, {}, success=False)
        return _gate("SEAT", self.status, SeatFactsResult(
            status=self.status, seat_request_type="EXACT_SEATS" if request["selected_seats"] else "WPLUS_AREA",
            wanda_store_id="store-1", wanda_show_id="show-1",
        ).model_dump(mode="json"))


class RecordingCost:
    def __init__(self, status="COST_READY"):
        self.calls, self.status = [], status

    def resolve_cost_gate(self, show, seat):
        self.calls.append((show, seat))
        if self.status != "COST_READY":
            return _gate("COST", self.status, {}, success=False)
        return _gate("COST", "COST_READY", WandaCostFacts(status="COST_READY", request_type="WPLUS_AREA").model_dump(mode="json"))


class RecordingPricing:
    def __init__(self): self.calls = []

    def price_gate(self, cost, show, seat, rules, **kwargs):
        self.calls.append((cost, show, seat, kwargs))
        return _gate("PRICING", "PRICED", WandaPricingResult(
            status="PRICED", request_type="WPLUS_AREA", unit_sell_price_fen=4500,
            total_sell_price_fen=4500 * (kwargs.get("ticket_count") or 1),
            ticket_count=kwargs.get("ticket_count") or 1,
        ).model_dump(mode="json"))


class RecordingQuotes:
    def __init__(self): self.calls = []

    def persist_gate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _gate("QUOTE", "QUOTE_PERSISTED", {"quote_record": {
            "record_id": "r1", "total": 4500,
            "tenant_id": kwargs["tenant_id"], "shop_id": kwargs["shop_id"],
            "buyer_id": kwargs["buyer_id"], "chat_id": kwargs["chat_id"],
            "purchase_context_id": kwargs["purchase_context_id"], "wanda_show_id": "show-1",
        }})


def _runtime(tmp_path, *, seat_status="WPLUS_AREA_RESOLVED", cost_status="COST_READY", recognition=None, show_status="RESOLVED"):
    services = {
        "recognition": recognition or RecordingRecognition(), "route": RecordingRoute(),
        "show": RecordingShow(show_status), "seat": RecordingSeat(seat_status),
        "cost": RecordingCost(cost_status), "pricing": RecordingPricing(), "quotes": RecordingQuotes(),
    }
    runtime = RecoveryQuoteRuntime(
        recognition_service=services["recognition"], route_service=services["route"], show_service=services["show"],
        seat_service=services["seat"], cost_service=services["cost"], pricing_service=services["pricing"],
        quote_service=services["quotes"], rules_provider=lambda: object(),
        fact_store=ConversationFactStore(tmp_path / "facts.sqlite"),
    )
    return runtime, services


def _event(event_id="e1", text=None):
    payload = {"imageUrls": ["https://example/image"]}
    if text is not None:
        payload = {"itemId": "item", "text": text}
    return {"envelope": {"id": event_id, "tenantId": "tenant", "payload": payload},
            "session": {"accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat"}}


@pytest.mark.asyncio
async def test_wplus_without_selected_seats_reaches_cost_and_pricing(tmp_path: Path):
    runtime, services = _runtime(tmp_path)
    result = await runtime.process_image_event(_event())
    assert result["status"] == "QUOTED"
    assert len(services["cost"].calls) == 1
    assert len(services["pricing"].calls) == 1
    assert services["seat"].calls[0]["selected_seats"] == []


@pytest.mark.asyncio
async def test_exact_seat_failure_stops_before_cost(tmp_path: Path):
    runtime, services = _runtime(tmp_path, seat_status="SELECTED_SEATS_REQUIRED",
                                 recognition=RecordingRecognition(seats=[]))
    result = await runtime.process_image_event(_event())
    assert result["status"] == "SELECTED_SEATS_REQUIRED"
    assert services["cost"].calls == []
    assert services["pricing"].calls == []
    assert services["quotes"].calls == []


@pytest.mark.asyncio
async def test_cost_failure_never_prices_or_persists(tmp_path: Path):
    runtime, services = _runtime(tmp_path, cost_status="PROVIDER_UNAVAILABLE")
    result = await runtime.process_image_event(_event())
    assert result["status"] == "PROVIDER_UNAVAILABLE"
    assert services["pricing"].calls == []
    assert services["quotes"].calls == []


@pytest.mark.asyncio
async def test_text_patch_forwards_ordinal_and_dimension(tmp_path: Path):
    runtime, services = _runtime(tmp_path)
    services["recognition"] = RecordingRecognition()
    runtime.recognition = services["recognition"]
    store = runtime.fact_store
    store.save(tenant_id="tenant", shop_id="shop", buyer_id="buyer", chat_id="chat", purchase_context_id="item",
               facts={"city": "合肥", "cinema": "万达影城", "movie": "奥德赛", "quote_date": "2026-09-13",
                      "showtime_start": "14:30", "candidate_shows": [{"start_time": "15:50"}]}, source="test", fact_tier="verified")
    result = await runtime.process_text_event({**_event("e2", "第二场IMAX，两张"), "text": "第二场IMAX，两张"})
    assert result["status"] == "QUOTED"
    assert services["show"].calls[0]["showtime_ordinal"] == 2
    assert services["show"].calls[0]["dimension"] == "IMAX"
    assert services["pricing"].calls[0][3]["ticket_count"] == 2


@pytest.mark.asyncio
async def test_fact_identity_isolation_does_not_mix_buyers(tmp_path: Path):
    runtime, services = _runtime(tmp_path)
    runtime.fact_store.save(tenant_id="tenant", shop_id="shop", buyer_id="other", chat_id="chat", purchase_context_id="item",
                            facts={"city": "厦门", "movie": "别的电影"}, source="test", fact_tier="verified")
    result = await runtime.process_text_event({**_event("e3", "两张"), "text": "两张"})
    assert result["status"] == "NEED_CLARIFICATION"
    assert services["show"].calls == []


@pytest.mark.asyncio
async def test_quote_persist_is_the_only_amount_authority(tmp_path: Path):
    runtime, services = _runtime(tmp_path)
    result = await runtime.process_image_event(_event())
    assert services["quotes"].calls
    assert result["reply_gate"]["status"] == "AMOUNT_REPLY_ALLOWED"
    assert result["quote"]["record_id"] == "r1"


class LiangpiaoRoute:
    async def resolve_gate(self, recognition):
        return _gate("CINEMA_ROUTE", "LIANGPIAO", CinemaRouteResult(
            route="LIANGPIAO", liangpiao_cinema_id="9001", resolution_reason="CROSSWALK",
        ).model_dump(mode="json"))


class LiangpiaoQuote:
    async def quote(self, request):
        return SelectedSeatQuoteResult(
            quote_id="lp-q", quote_hash="a" * 64, show_id="lp-show", seats=request.seats,
            provider_amount_fen=3000, buyer_amount_fen=3500, pricing_rule_version="test",
            expires_at=datetime.now(timezone.utc), snapshot={"preflight_response": {"ok": True}},
            generation=1, trace_id=request.trace_id,
        )


class LiangpiaoFacts:
    def from_preflight(self, payload, *, request):
        return SimpleNamespace(provider="LIANGPIAO", payload=payload)


class LiangpiaoEngine:
    def quote(self, facts, rules):
        return SimpleNamespace(total_quote_cents=3500, provider="LIANGPIAO", quote_route="LIANGPIAO",
                               price_mode="FIXED", quote_scope="exact_seats", seat_zone_type="REGULAR",
                               provider_quote_id="lp-q", provider_quote_hash="a" * 64)


class LiangpiaoQuotes(RecordingQuotes):
    def persist_liangpiao(self, pricing, **kwargs):
        return {"record_id": "lp-r", "tenant_id": kwargs["tenant_id"], "shop_id": kwargs["shop_id"],
                "buyer_id": kwargs["buyer_id"], "chat_id": kwargs["chat_id"],
                "purchase_context_id": kwargs["purchase_context_id"], "liangpiao_show_id": kwargs["show_id"]}


@pytest.mark.asyncio
async def test_liangpiao_runtime_branch_uses_provider_and_persists(tmp_path: Path):
    runtime, services = _runtime(tmp_path, recognition=RecordingRecognition(seats=["9排11座"]))
    runtime.route = LiangpiaoRoute()
    runtime.quotes = LiangpiaoQuotes()
    runtime.liangpiao_quote = LiangpiaoQuote()
    runtime.liangpiao_facts = LiangpiaoFacts()
    runtime.pricing_engine = LiangpiaoEngine()
    result = await runtime.process_image_event(_event("lp-1"))
    assert result["status"] == "QUOTED"
    assert result["quote"]["liangpiao_show_id"] == "lp-show"
    assert services["show"].calls == []
    assert services["cost"].calls == []


@pytest.mark.asyncio
async def test_liangpiao_runtime_branch_requests_exact_seats(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    runtime.route = LiangpiaoRoute()
    runtime.quotes = LiangpiaoQuotes()
    runtime.liangpiao_quote = LiangpiaoQuote()
    runtime.liangpiao_facts = LiangpiaoFacts()
    runtime.pricing_engine = LiangpiaoEngine()
    result = await runtime.process_image_event(_event("lp-2"))
    assert result["status"] == "NEED_CLARIFICATION"
    assert result["reason"] == "SELECTED_SEATS_REQUIRED"
