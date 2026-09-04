from __future__ import annotations

from pathlib import Path

import pytest

from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer
from app.pricing.engine import V4PricingEngine
from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.quote_v2.service import CanonicalQuoteRuntime
from app.recognition_v2.models import RecognitionResult
from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult
from app.seat_facts_v2.service import SeatFactsV2Service, select_same_type_available_reference
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem
from app.wanda_pricing_v2.service import WandaPricingV2Service


class Route:
    async def resolve(self, recognition):
        from app.cinema_route_v2.models import CinemaRouteResult
        return CinemaRouteResult(
            route="WANDA_SELF", wanda_city_id="city-1", wanda_store_id="store-1",
            wanda_city_name="牡丹江", wanda_cinema_name="万达影城",
            resolution_reason="test",
        )


class Show:
    async def resolve(self, request):
        return ShowResolutionResult(
            status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
            wanda_film_id="film-1", movie_name="坠落2：死点", show_date="2026-09-05",
            start_time="19:55", hall_name="3号激光厅", sales_price_fen=6200,
        )


class Seats:
    async def resolve(self, request, *, manual_mark_detector=None):
        return SeatFactsResult(
            status="SEAT_UNAVAILABLE", seat_request_type="EXACT_SEATS",
            wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=False,
            resolution_reason="TARGET_SEAT_NOT_AVAILABLE",
            exact_seats=[
                ExactSeatFact(
                    label="8排7座", seat_label="8排7座", seat_id="sold-7", wanda_seat_id="sold-7",
                    row=8, col=7, area_code="36", zone_type="W+", seat_type="WPLUS",
                    member_price_group="wplus-6200-3910", status="OCCUPIED",
                    area_original_price_fen=6200, area_member_price_fen=3910,
                    has_valid_area_member_price=True,
                ),
                ExactSeatFact(
                    label="8排8座", seat_label="8排8座", seat_id="sold-8", wanda_seat_id="sold-8",
                    row=8, col=8, area_code="36", zone_type="W+", seat_type="WPLUS",
                    member_price_group="wplus-6200-3910", status="OCCUPIED",
                    area_original_price_fen=6200, area_member_price_fen=3910,
                    has_valid_area_member_price=True,
                ),
            ],
            same_type_reference=ExactSeatFact(
                label="8排6座", seat_label="8排6座", seat_id="available-6", wanda_seat_id="available-6",
                row=8, col=6, area_code="36", zone_type="W+", seat_type="WPLUS",
                member_price_group="wplus-6200-3910", status="AVAILABLE",
                area_original_price_fen=6200, area_member_price_fen=3910,
                has_valid_area_member_price=True,
            ),
        )


class Cost:
    def resolve(self, show, seats):
        assert seats.same_type_reference is not None
        return WandaCostFacts(
            status="COST_READY", request_type="EXACT_SEATS",
            cost_items=[WandaCostItem(
                seat_label="8排6座", area_code="36", zone_type="W+",
                cost_fen=3910, cost_source="REALTIME_AREA_WPLUS",
            )],
        )


def recognition(*, price_mismatch=False):
    return RecognitionResult(
        city_text="牡丹江", cinema_text="万达影城（牡丹江万达广场IMAX店）", movie="坠落2：死点",
        show_date="2026-09-05", start_time="19:55", hall="3号激光厅",
        selected_seats=["8排7座", "8排8座"], has_manual_mark=False,
        seat_matched=True, seat_confirm_required=price_mismatch,
        price_mismatch=price_mismatch,
        seat_confirm_reasons=["PRICE_MISMATCH"] if price_mismatch else [],
    )


def runtime(tmp_path: Path):
    return CanonicalQuoteRuntime(
        recognition_service=None, cinema_route_service=Route(), show_resolve_service=Show(),
        seat_facts_service=Seats(), cost_resolution_service=Cost(),
        wanda_pricing_service=WandaPricingV2Service(V4PricingEngine()),
        selected_seat_quote_service=None,
        pricing_rules_provider=PricingRulesSnapshot(enabled=False, rule_version="r1"),
        quote_service=__import__("app.quote_v2.service", fromlist=["QuoteV2Service"]).QuoteV2Service(
            QuoteRecordStore(tmp_path / "quotes.json"), ttl_seconds=1800,
        ), liangpiao_facts_adapter=None, reply_renderer=CanonicalBuyerReplyRenderer(),
    )


@pytest.mark.parametrize("unavailable_status", ["OCCUPIED", "SOLD", "LOCKED"])
def test_same_type_reference_matches_all_type_fields_and_never_crosses_area(unavailable_status):
    selected = {
        "seat_id": "sold", "area_code": "36", "zone_type": "W+", "seat_type": "WPLUS",
        "member_price_group": "group-a", "status": unavailable_status, "row": 8, "col": 7,
    }
    candidate = {
        "seat_id": "same", "area_code": "36", "zone_type": "W+", "seat_type": "WPLUS",
        "member_price_group": "group-a", "status": "AVAILABLE", "row": 8, "col": 6,
    }
    wrong_area = {**candidate, "seat_id": "wrong-area", "area_code": "37"}
    assert select_same_type_available_reference([selected], [wrong_area, candidate]) == candidate


@pytest.mark.asyncio
async def test_seat_facts_reference_requires_same_member_group_and_returns_no_reference_when_missing():
    class Source:
        async def get_realtime_seats(self, show_id):
            return {"code": 0, "data": {"realtimeSeats": {"area": [
                {"areaId": "36", "areaName": "W+", "zoneType": "W+", "areaPrice": {"salesPrice": 3910}, "seat": [
                    {"seatId": "sold", "name": "8排7座", "status": 0, "seatType": "WPLUS", "payMemberSeatStatus": 1},
                    {"seatId": "other", "name": "8排6座", "status": 1, "seatType": "WPLUS", "payMemberSeatStatus": 1},
                ]},
                {"areaId": "37", "areaName": "W+", "zoneType": "W+", "areaPrice": {"salesPrice": 3990}, "seat": [
                    {"seatId": "wrong", "name": "8排8座", "status": 1, "seatType": "WPLUS", "payMemberSeatStatus": 1},
                ]},
            ]}}}

    result = await SeatFactsV2Service(Source()).resolve({
        "route": "WANDA_SELF", "wanda_store_id": "store", "wanda_show_id": "show",
        "selected_seats": ["8排7座"], "has_selected_seats": True, "has_manual_mark": False,
    })
    assert result.status == "SEAT_UNAVAILABLE"
    assert result.same_type_reference is not None
    assert result.same_type_reference.seat_label == "8排6座"

    class NoReference(Source):
        async def get_realtime_seats(self, show_id):
            response = await super().get_realtime_seats(show_id)
            response["data"]["realtimeSeats"]["area"][0]["seat"][1]["status"] = 0
            return response

    no_reference = await SeatFactsV2Service(NoReference()).resolve({
        "route": "WANDA_SELF", "wanda_store_id": "store", "wanda_show_id": "show",
        "selected_seats": ["8排7座"], "has_selected_seats": True, "has_manual_mark": False,
    })
    assert no_reference.same_type_reference is None
    assert no_reference.resolution_reason == "TARGET_SEAT_NOT_AVAILABLE"


def test_same_type_reference_rejects_mixed_selected_types():
    left = {"area_code": "36", "zone_type": "W+", "seat_type": "WPLUS", "member_price_group": "a"}
    right = {**left, "member_price_group": "b"}
    assert select_same_type_available_reference([left, right], [right]) is None


@pytest.mark.asyncio
async def test_unavailable_exact_quote_uses_single_same_type_reference_preview(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(), identity={
            "event_id": "event-reference", "tenant_id": "107", "shop_id": "2313315754",
            "buyer_id": "2464035965", "chat_id": "66050332180",
            "purchase_context_id": "ctx-1", "message_id": "msg-1",
        },
    )
    assert result["status"] == "QUOTED"
    quote = result["quote"]
    assert quote["quote_state"] == "PREVIEW"
    assert quote["transaction_authorized"] is False
    assert quote["unit_sell_price_fen"] == 3910
    assert quote["total_sell_price_fen"] is None
    assert quote["ticket_count"] is None
    assert quote["same_type_reference_only"] is True
    assert [item["status"] for item in quote["selected_seats"]] == ["OCCUPIED", "OCCUPIED"]
    assert quote["same_type_reference"]["seat_label"] == "8排6座"
    assert result["current_runtime_reply"] == (
        "你刚选的这几个座位现在没了，同类型座位39.1一张，可以重新选一下座位发我哈"
    )


@pytest.mark.asyncio
async def test_price_mismatch_unavailable_seats_only_returns_single_reference_price(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(price_mismatch=True), identity={
            "event_id": "event-reference-mismatch", "tenant_id": "107", "shop_id": "2313315754",
            "buyer_id": "2464035965", "chat_id": "66050332180",
            "purchase_context_id": "ctx-2", "message_id": "msg-2",
        },
    )
    assert result["status"] == "QUOTED"
    assert result["quote"]["ticket_count"] is None
    assert result["quote"]["total_sell_price_fen"] is None
    assert result["quote"]["transaction_authorized"] is False


@pytest.mark.asyncio
async def test_reference_quote_does_not_use_screenshot_total_or_create_transaction_quantity(tmp_path: Path):
    result = await runtime(tmp_path).quote_recognition(
        recognition(), identity={
            "event_id": "event-reference-safe", "tenant_id": "107", "shop_id": "2313315754",
            "buyer_id": "2464035965", "chat_id": "66050332180",
            "purchase_context_id": "ctx-3", "message_id": "msg-3",
        }, ticket_count=2,
    )
    assert result["quote"]["total_sell_price_fen"] is None
    assert result["quote"]["ticket_count"] is None
    assert result["quote"].get("screenshot_displayed_total") is None
