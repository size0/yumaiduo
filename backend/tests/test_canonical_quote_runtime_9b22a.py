from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.pricing.engine import V4PricingEngine
from app.pricing.models import PricingFacts, PricingRulesSnapshot, PricingSeatFact
from app.quote_record_store import QuoteRecordStore
from fastapi.testclient import TestClient

from app.main import create_app
from app.quote_v2.service import (
    CanonicalQuoteRequest, CanonicalQuoteRuntime, ManualQuoteInput, QuoteV2Service,
)
from app.recognition_v2.models import RecognitionResult
from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.rules_first_store import RulesFirstStore
from app.shop_automation_store import ShopAutomationStore
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem
from app.wanda_pricing_v2.service import WandaPricingV2Service


class Route:
    async def resolve(self, recognition):
        from app.cinema_route_v2.models import CinemaRouteResult
        return CinemaRouteResult(
            route="WANDA_SELF", wanda_city_id="city-1", wanda_store_id="store-1",
            wanda_city_name="广州", wanda_cinema_name="广州测试万达",
            resolution_reason="test",
        )


class Show:
    async def resolve(self, request):
        return ShowResolutionResult(
            status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
            wanda_film_id="film-1", movie_name="测试电影", show_date="2026-09-05",
            start_time="13:35", hall_name="IMAX厅", sales_price_fen=6200,
            wplus_activity_price_fen=4490,
        )


class Seats:
    def __init__(self, *, detector_result=None):
        self.requests = []
        self.detector_result = detector_result
        self.detector_calls = 0

    async def resolve(self, request, *, manual_mark_detector=None):
        self.requests.append(dict(request))
        mark = request.get("has_manual_mark")
        if request.get("selected_seats") and mark is None and manual_mark_detector is not None:
            self.detector_calls += 1
            mark = await manual_mark_detector.detect(request.get("image_url"))
        if request.get("selected_seats") and mark is None:
            return SeatFactsResult(
                status="MANUAL_MARK_REQUIRED", seat_request_type="MANUAL_MARK_REQUIRED",
                wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=None,
            )
        if not request.get("selected_seats") or mark is True:
            return SeatFactsResult(
                status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
                wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=mark,
            )
        return SeatFactsResult(
            status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
            wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=False,
            exact_seats=[ExactSeatFact(
                label="8排9座", seat_label="8排9座", seat_id="seat-1", status="AVAILABLE",
                area_code="36", zone_type="W+", area_original_price_fen=6200,
            )],
        )


class Cost:
    def resolve(self, show, seats):
        if seats.seat_request_type == "WPLUS_AREA":
            return WandaCostFacts(
                status="COST_READY", request_type="WPLUS_AREA",
                cost_items=[WandaCostItem(zone_type="W+", cost_fen=4490, cost_source="SHOWTIME_WPLUS")],
            )
        return WandaCostFacts(
            status="COST_READY", request_type="EXACT_SEATS",
            cost_items=[WandaCostItem(
                seat_label="8排9座", area_code="36", zone_type="W+", cost_fen=4500,
                cost_source="REALTIME_AREA_WPLUS",
            )],
        )


class NoPricing:
    def __init__(self):
        self.calls = 0

    def price(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("pricing must not run when cost is not ready")


class Detector:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def detect(self, image_url):
        self.calls += 1
        return self.value


def recognition(*, selected_seats=None, has_manual_mark=None):
    return RecognitionResult(
        city_text="广州", cinema_text="广州测试万达", movie="测试电影",
        show_date="2026-09-05", start_time="13:35", hall="IMAX厅",
        selected_seats=selected_seats or [], has_manual_mark=has_manual_mark,
    )


def runtime(tmp_path: Path, *, seats=None, cost=None, pricing=None, detector=None):
    store = QuoteRecordStore(tmp_path / "quotes.json")
    return CanonicalQuoteRuntime(
        recognition_service=None, cinema_route_service=Route(), show_resolve_service=Show(),
        seat_facts_service=seats or Seats(), cost_resolution_service=cost or Cost(),
        wanda_pricing_service=pricing or WandaPricingV2Service(V4PricingEngine()),
        selected_seat_quote_service=None,
        pricing_rules_provider=PricingRulesSnapshot(enabled=False, rule_version="r1"),
        quote_service=QuoteV2Service(store, ttl_seconds=1800),
        liangpiao_facts_adapter=None, manual_mark_detector=detector,
    )


IDENTITY = {
    "event_id": "event-1", "tenant_id": "tenant-1", "shop_id": "shop-1",
    "buyer_id": "buyer-1", "chat_id": "chat-1", "purchase_context_id": "purchase-1",
    "message_id": "message-1",
}


@pytest.mark.asyncio
async def test_structured_wplus_request_with_known_count_persists_transaction_ready_quote(tmp_path: Path):
    result = await runtime(tmp_path).quote_structured(CanonicalQuoteRequest(
        tenant_id=IDENTITY["tenant_id"], shop_id=IDENTITY["shop_id"],
        buyer_id=IDENTITY["buyer_id"], chat_id=IDENTITY["chat_id"],
        purchase_context_id=IDENTITY["purchase_context_id"], request_id=IDENTITY["event_id"],
        city="广州", cinema="广州测试万达", movie="测试电影",
        quote_date="2026-09-05", showtime_start="13:35", hall="IMAX厅",
        seat_request_type="WPLUS_AREA", ticket_count=2,
    ))
    assert result["status"] == "QUOTED"
    assert result["quote"]["request_type"] == "WPLUS_AREA"
    assert result["quote"]["quote_state"] == "TRANSACTION_READY"
    assert result["quote"]["total_sell_price_fen"] == 8980


@pytest.mark.asyncio
async def test_wplus_unknown_count_is_preview(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(), identity=IDENTITY,
    )
    assert result["status"] == "QUOTED"
    assert result["quote"]["quote_state"] == "PREVIEW"
    assert result["quote"]["unit_sell_price_fen"] == 4490
    assert result["quote"]["total_sell_price_fen"] is None
    assert result["quote"]["transaction_authorized"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", [True])
async def test_selected_seats_with_manual_mark_uses_wplus_area(tmp_path: Path, mark: bool):
    result = await runtime(tmp_path).quote_recognition(
        recognition(selected_seats=["8排9座"], has_manual_mark=mark), identity=IDENTITY,
    )
    assert result["quote"]["request_type"] == "WPLUS_AREA"
    assert result["quote"]["selected_seats"] == []


@pytest.mark.asyncio
async def test_unmarked_selected_seats_uses_exact_seats(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(selected_seats=["8排9座"], has_manual_mark=False), identity=IDENTITY,
    )
    assert result["quote"]["request_type"] == "EXACT_SEATS"
    assert result["quote"]["selected_seats"][0]["seat_label"] == "8排9座"


@pytest.mark.asyncio
async def test_unknown_manual_mark_is_detected_once_and_not_assumed_false(tmp_path: Path):
    detector = Detector(True)
    seats = Seats()
    result = await runtime(tmp_path, seats=seats, detector=detector).quote_recognition(
        recognition(selected_seats=["8排9座"]), identity=IDENTITY, image_url="https://img.test/x",
    )
    assert result["quote"]["request_type"] == "WPLUS_AREA"
    assert detector.calls == 1
    assert seats.detector_calls == 1


@pytest.mark.asyncio
async def test_unknown_manual_mark_without_detector_fails_closed(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(selected_seats=["8排9座"]), identity=IDENTITY, image_url="https://img.test/x",
    )
    assert result["status"] == "MANUAL_MARK_REQUIRED"


def test_create_app_gate_on_terminates_image_event_at_canonical_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.setenv("CANONICAL_QUOTE_RUNTIME_ENABLED", "true")

    class Runtime:
        def __init__(self):
            self.calls = 0

        async def process_image_event(self, body):
            self.calls += 1
            return {"status": "QUOTED", "route": "WANDA_SELF"}

        async def aclose(self):
            return None

    runtime_instance = Runtime()
    shop_store = ShopAutomationStore(tmp_path / "shops.json")
    shop_store.sync("tenant-1", [{"accountUnb": "shop-1", "shopName": "测试店铺"}])
    shop_store.set_canonical_enabled("tenant-1", "shop-1", True)
    client = TestClient(create_app(
        service=object(), canonical_quote_runtime=runtime_instance,
        shop_automation_store=shop_store,
        rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    ))
    event = {
        "envelope": {
            "id": "image-event-1", "tenantId": "tenant-1", "event": "im.message.received",
            "payload": {"imageUrls": ["https://img.test/seat.webp"]},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [], "order": None,
    }
    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process", json=event,
        headers={"x-wanda-ai-v2-bridge-key": "bridge-test"},
    )
    assert response.status_code == 202
    assert response.json()["canonical_quote_status"] == "QUOTED"
    assert runtime_instance.calls == 1


def test_create_app_gate_off_keeps_event_out_of_canonical_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test")
    monkeypatch.delenv("CANONICAL_QUOTE_RUNTIME_ENABLED", raising=False)

    class Runtime:
        calls = 0

        async def process_image_event(self, body):
            self.calls += 1
            return {"status": "QUOTED"}

    runtime_instance = Runtime()
    client = TestClient(create_app(
        service=object(), canonical_quote_runtime=runtime_instance,
        rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    ))
    event = {
        "envelope": {
            "id": "image-event-off", "tenantId": "tenant-1", "event": "im.message.received",
            "payload": {"imageUrls": ["https://img.test/seat.webp"]},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [], "order": None,
    }
    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process", json=event,
        headers={"x-wanda-ai-v2-bridge-key": "bridge-test"},
    )
    assert response.status_code == 202
    assert "canonical_quote_status" not in response.json()
    assert runtime_instance.calls == 0


def test_expired_provider_preflight_cannot_be_transaction_candidate(tmp_path: Path):
    svc = QuoteV2Service(QuoteRecordStore(tmp_path / "expiry.json"), ttl_seconds=1800)
    pricing = V4PricingEngine().quote(PricingFacts(
        provider="LIANGPIAO", show_id="lp-show", quote_route="LIANGPIAO_FIXED",
        quantity=1, quote_scope="exact_seats", provider_total_amount_cents=1000,
        provider_buyer_amount_cents=1000, provider_amount_cents=1000,
        preflight_verified=True,
        seats=(PricingSeatFact(seat_id="s1", seat_label="1排1座", cost_source="liangpiao_preflight"),),
    ), PricingRulesSnapshot(enabled=False, rule_version="r1"))
    record = svc.persist_liangpiao(
        pricing, tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        purchase_context_id="purchase-1", request_id="expiry-request", city="广州",
        cinema_id="1", cinema_name="测试影院", movie="测试电影", quote_date="2026-09-05",
        showtime_start="13:35", hall="IMAX厅", show_id="lp-show",
        selected_seats=[{"rowNo": 1, "colNo": 1, "seatNo": "1排1座"}],
        provider_preflight_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert record["seller_quote_expires_at"] > record["provider_preflight_expires_at"]
    assert record["provider_preflight_verified"] is False
    assert record["quote_state"] == "PREVIEW"
    assert record["transaction_authorized"] is False


@pytest.mark.parametrize(
    ("provider_route", "provider"),
    [("WANDA_SELF", "WANDA"), ("LIANGPIAO", "LIANGPIAO")],
)
def test_manual_quote_keeps_source_and_provider_route_separate(
    tmp_path: Path, provider_route: str, provider: str,
):
    svc = QuoteV2Service(QuoteRecordStore(tmp_path / "manual.json"), ttl_seconds=7200)
    record = svc.persist_manual(ManualQuoteInput(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        purchase_context_id="purchase-1", request_id=f"manual-{provider_route}",
        city="广州", cinema="测试影院", movie="测试电影", quote_date="2026-09-05",
        showtime_start="13:35", hall="IMAX厅", seat_display="W+区域",
        quote_scope="area_preview", seat_zone_type="W+", price_basis="UNIT",
        provider_route=provider_route, unit_sell_price_fen=4490,
    ))
    assert record["source"] == "MANUAL_OPERATOR"
    assert record["provider_route"] == provider_route
    assert record["provider"] == provider


@pytest.mark.asyncio
async def test_missing_wplus_cost_does_not_call_pricing_or_create_quote(tmp_path: Path):
    class MissingCost:
        def resolve(self, show, seats):
            return WandaCostFacts(status="PROBE_REQUIRED", request_type="WPLUS_AREA", probe_required=True)

    pricing = NoPricing()
    result = await runtime(tmp_path, cost=MissingCost(), pricing=pricing).quote_recognition(
        recognition(), identity=IDENTITY,
    )
    assert result["status"] == "PROBE_REQUIRED"
    assert result["probe_executed"] is False
    assert result["pricing_called"] is False
    assert pricing.calls == 0
