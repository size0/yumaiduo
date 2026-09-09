from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathlib import Path

from app.pricing.engine import V4PricingEngine

from app.pricing.models import PricingFacts, PricingRulesSnapshot, PricingSeatFact

from app.quote_record_store import QuoteRecordStore

from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer

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

def recognition(**kwargs):
    return RecognitionResult(city_text="广州", cinema_text="广州测试万达", movie="测试电影",
        show_date="2026-09-05", start_time="13:35", hall="IMAX厅", selected_seats=[], has_manual_mark=None)


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
        reply_renderer=CanonicalBuyerReplyRenderer(),
    )