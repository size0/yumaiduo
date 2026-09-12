from pathlib import Path

import pytest

from app.cinema_route_v2.service import CinemaRouteV2Service
from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.quote_v2.service import QuoteV2Service
from app.recovery.runtime import RecoveryQuoteRuntime
from app.recognition_v2.liangpiao import LiangpiaoRecognitionResponse
from app.recognition_v2.service import RecognitionV2Service
from app.seat_facts_v2.service import SeatFactsV2Service
from app.show_resolve_v2.service import ShowResolveV2Service
from app.wanda_cost_v2.service import WandaCostResolutionService
from app.wanda_pricing_v2.service import WandaPricingV2Service
from app.selected_seat_quote_service import SelectedSeatQuoteResult
from datetime import datetime, timezone
from types import SimpleNamespace


class _RecognitionTransport:
    def __init__(self, *, seats=True):
        self.seats = seats

    async def recognize(self, image_url, **kwargs):
        return LiangpiaoRecognitionResponse(
            data={
                "recognizeId": "recognition-1",
                "rawResults": {
                    "city": "\u5408\u80a5", "cinema": "\u5408\u80a5\u4e07\u8fbe\u5f71\u57ce",
                    "film": "\u5965\u5fb7\u8d5b", "showtime": "2026-09-13 14:30",
                    "hall": "1\u53f7\u5385", "dimension": "2D",
                    "seat": [{"seatName": "9\u639211\u5ea7"}] if self.seats else [],
                },
            },
            raw_provider_result={},
        )

    async def aclose(self):
        return None


class _WandaReadSource:
    def __init__(self, *, show_ok=True, priced=True):
        self.show_ok = show_ok
        self.priced = priced

    async def get_city_list(self):
        return {"code": 0, "data": {"cityList": [{"cityId": "city-1", "cityName": "\u5408\u80a5"}]}}

    async def get_cinema_list(self, *args):
        return {"code": 0, "data": {"cinemaList": [{
            "storeId": "store-1", "cinemaName": "\u5408\u80a5\u4e07\u8fbe\u5f71\u57ce", "cityId": "city-1",
        }]}}

    async def get_showtimes(self, *args):
        if not self.show_ok:
            return {"code": 500, "data": {}}
        member_price = "4200" if self.priced else None
        return {"code": 0, "data": {"showtimeFilmInf": [{
            "filmName": "\u5965\u5fb7\u8d5b", "showtimeFilmDateInf": [{
                "date": "20260913", "showtimesInf": {"showtimeList": [{
                    "showtimeId": "show-1", "filmName": "\u5965\u5fb7\u8d5b",
                    "showDate": "2026-09-13", "realtime": "14:30",
                    "hallName": "1\u53f7\u5385", "dimension": "2D",
                    "salesPrice": "4500", "wPlusActivityPrice": member_price,
                }]},
            }],
        }]}}

    async def get_realtime_seats(self, show_id):
        member_price = "4200" if self.priced else None
        return {"code": 0, "data": {"realtimeSeats": {"area": [{
            "areaName": "W+", "areaCode": "w1",
            "areaPrice": {"originalPrice": "4500", "memberPrice": member_price},
            "seat": [{"seatName": "9\u639211\u5ea7", "seatId": "seat-1",
                       "payMemberSeatStatus": 1, "status": "AVAILABLE"}],
        }]}}}


class _LiangpiaoQuote:
    async def quote(self, request):
        return SelectedSeatQuoteResult(
            quote_id="provider-q", quote_hash="a" * 64, show_id="provider-show", seats=request.seats,
            provider_amount_fen=3000, buyer_amount_fen=3500, pricing_rule_version="test",
            expires_at=datetime.now(timezone.utc), snapshot={"preflight_response": {"ok": True}},
            generation=1, trace_id=request.trace_id,
        )


class _LiangpiaoFacts:
    def from_preflight(self, payload, *, request):
        return SimpleNamespace(provider="LIANGPIAO", payload=payload)


class _LiangpiaoEngine:
    def quote(self, facts, rules):
        return SimpleNamespace(total_quote_cents=3500, unit_quote_cents=3500,
                               provider="LIANGPIAO", quote_route="LIANGPIAO",
                               price_mode="FIXED", quote_scope="exact_seats", seat_zone_type="REGULAR",
                               provider_quote_id="provider-q", provider_quote_hash="a" * 64,
                               provider_amount_cents=3000, base_total_cents=3000,
                               max_price_cents=3500, ticket_count=1, needs_ticket_count=False,
                               pricing_rule_version="test", calculation_evidence={}, semantic_flags=())


class _LiangpiaoRouteSource(_WandaReadSource):
    async def get_city_list(self):
        return {"code": 0, "data": {"cityList": [{"cityId": "city-qz", "cityName": "\u6cc9\u5dde"}]}}

    async def get_cinema_list(self, *args):
        return {"code": 0, "data": {"cinemaList": []}}


def _event(event_id="real-e1"):
    return {"envelope": {"id": event_id, "tenantId": "tenant-1", "payload": {
        "imageUrls": ["https://example.test/image"], "itemId": "purchase-1",
    }}, "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"}}


def _wanda_runtime(tmp_path: Path, source, transport=None, *, pricing_engine=None):
    return RecoveryQuoteRuntime(
        recognition_service=RecognitionV2Service(transport or _RecognitionTransport()),
        route_service=CinemaRouteV2Service(source), show_service=ShowResolveV2Service(source),
        seat_service=SeatFactsV2Service(source), cost_service=WandaCostResolutionService(),
        pricing_service=WandaPricingV2Service(pricing_engine),
        quote_service=QuoteV2Service(QuoteRecordStore(tmp_path / "quotes.json")),
        rules_provider=lambda: PricingRulesSnapshot(),
    )


@pytest.mark.asyncio
async def test_real_services_and_orchestrator_quote_with_fake_provider(tmp_path: Path):
    source = _WandaReadSource()
    runtime = _wanda_runtime(tmp_path, source)

    result = await runtime.process_image_event(_event())

    assert result["status"] == "QUOTED"
    assert result["reply_gate"]["status"] == "AMOUNT_REPLY_ALLOWED"
    assert result["quote"]["wanda_show_id"] == "show-1"
    assert result["quote"]["buyer_id"] == "buyer-1"


@pytest.mark.asyncio
async def test_real_services_context_image_then_seat_image(tmp_path: Path):
    class TwoImages(_RecognitionTransport):
        def __init__(self):
            super().__init__(seats=False)
            self.calls = 0

        async def recognize(self, image_url, **kwargs):
            self.calls += 1
            response = await super().recognize(image_url, **kwargs)
            if self.calls == 2:
                raw = dict(response.data["rawResults"])
                raw.update({"city": None, "cinema": None, "film": None, "showtime": None,
                            "seat": [{"seatName": "9\u639211\u5ea7"}]})
                response.data["rawResults"] = raw
            return response

    from app.conversation_fact_store import ConversationFactStore
    source = _WandaReadSource()
    transport = TwoImages()
    runtime = _wanda_runtime(tmp_path, source, transport)
    runtime.fact_store = ConversationFactStore(tmp_path / "facts.sqlite")
    first = await runtime.process_image_event(_event("context-image"))
    second = await runtime.process_image_event(_event("seat-image"))
    assert first["status"] == "QUOTED"
    assert second["status"] == "QUOTED"
    assert second["quote"]["wanda_show_id"] == "show-1"
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_real_show_provider_failure_is_safe(tmp_path: Path):
    result = await _wanda_runtime(tmp_path, _WandaReadSource(show_ok=False)).process_image_event(_event("show-down"))
    assert result["status"] == "PROVIDER_UNAVAILABLE"
    assert result["reply_gate"]["status"] != "AMOUNT_REPLY_ALLOWED"


@pytest.mark.asyncio
async def test_real_cost_probe_required_is_safe(tmp_path: Path):
    result = await _wanda_runtime(tmp_path, _WandaReadSource(priced=False)).process_image_event(_event("probe-needed"))
    assert result["status"] in {"PROBE_REQUIRED", "COST_UNAVAILABLE"}
    assert result["reply_gate"]["status"] != "AMOUNT_REPLY_ALLOWED"


@pytest.mark.asyncio
async def test_real_show_service_uses_candidate_time_but_provider_verifies_id():
    source = _WandaReadSource()
    service = ShowResolveV2Service(source)
    gate = await service.resolve_gate({
        "wanda_store_id": "store-1", "movie": "\u5965\u5fb7\u8d5b", "show_date": "2026-09-13",
        "candidate_shows": [{"show_id": "untrusted-candidate", "start_time": "14:30"}],
    })
    assert gate.success is True
    assert gate.facts["wanda_show_id"] == "show-1"


@pytest.mark.asyncio
async def test_real_show_service_keeps_multiple_candidates_as_clarification():
    class MultiShowSource(_WandaReadSource):
        async def get_showtimes(self, *args):
            response = await super().get_showtimes(*args)
            show = response["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"][0]
            response["data"]["showtimeFilmInf"][0]["showtimeFilmDateInf"][0]["showtimesInf"]["showtimeList"] = [
                show, {**show, "showtimeId": "show-2", "realtime": "16:30"},
            ]
            return response

    service = ShowResolveV2Service(MultiShowSource())
    gate = await service.resolve_gate({
        "wanda_store_id": "store-1", "movie": "\u5965\u5fb7\u8d5b", "show_date": "2026-09-13",
        "candidate_shows": [{"start_time": "14:30"}, {"start_time": "16:30"}],
    })
    assert gate.success is False
    assert gate.status == "CANDIDATE_REQUIRED"


@pytest.mark.asyncio
async def test_real_route_and_orchestrator_enter_liangpiao_exact_seat_path(tmp_path: Path):
    class Transport(_RecognitionTransport):
        async def recognize(self, image_url, **kwargs):
            response = await super().recognize(image_url, **kwargs)
            raw = dict(response.data["rawResults"])
            raw["city"] = "\u6cc9\u5dde"
            raw["cinema"] = "\u6cc9\u5dde\u6d66\u897f\u4e07\u8fbe"
            response.data["rawResults"] = raw
            return LiangpiaoRecognitionResponse(data=response.data, raw_provider_result={"cinemaId": "9001"})

    source = _LiangpiaoRouteSource()
    runtime = RecoveryQuoteRuntime(
        recognition_service=RecognitionV2Service(Transport()),
        route_service=CinemaRouteV2Service(source), show_service=ShowResolveV2Service(source),
        seat_service=SeatFactsV2Service(source), cost_service=WandaCostResolutionService(),
        pricing_service=WandaPricingV2Service(),
        quote_service=QuoteV2Service(QuoteRecordStore(tmp_path / "quotes.json")),
        rules_provider=lambda: PricingRulesSnapshot(),
        liangpiao_quote_service=_LiangpiaoQuote(), liangpiao_facts_adapter=_LiangpiaoFacts(),
        pricing_engine=_LiangpiaoEngine(),
    )
    result = await runtime.process_image_event(_event("liangpiao-real"))
    assert result["status"] == "QUOTED"
    assert result["quote"]["liangpiao_show_id"] == "provider-show"
