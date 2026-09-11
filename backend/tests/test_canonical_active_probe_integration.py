from __future__ import annotations

from pathlib import Path

import pytest

from app.probe.models import ProbeResult, ProbeSeatTypePrice
from app.probe.seat_selector import LiveSeat
from app.probe.wanda_provider import WandaDirectProbeProvider, WandaProbeAccountPool
from app.pricing.engine import V4PricingEngine
from app.pricing.models import PricingRulesSnapshot
from app.quote_record_store import QuoteRecordStore
from app.quote_v2.service import CanonicalQuoteRuntime, QuoteV2Service
from app.recognition_v2.models import RecognitionResult
from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem, WandaProbeTarget
from app.wanda_cost_v2.probe_resolver import CanonicalWandaProbeCostResolver
from app.wanda_pricing_v2.service import WandaPricingV2Service


class FakeWandaHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._settings_provider = lambda: object()

    async def _official_app_request(self, account, origin, path, *, method, pairs, **kwargs):
        self.calls.append((method, path, str(dict(pairs))))
        if path == "/order/create_order.api":
            return {"code": 0, "data": {"bizCode": 0, "orderId": "temporary-1"}}
        if path == "/order/order_status.api":
            return {"code": 0, "data": {"orderStatus": 40, "lockSeatTime": 1}}
        if path == "/order/cancel.api":
            return {"code": 0, "data": {"bizCode": 0}}
        return {"code": 0, "data": {}}

    async def _official_get(self, account, origin, path, pairs, **kwargs):
        self.calls.append(("GET", path, str(dict(pairs))))
        return {"code": 0, "data": {}}

    @staticmethod
    def _wplus_offer_price(payload):
        return 3590

    @staticmethod
    def _seat_id_available(payload, seat_id):
        return seat_id == "seat-1"


class FakeAccountPool:
    def payload(self, account_ref):
        return {"token": "token-in-memory", "phone": "13800000000"}


class FakeWandaAccountSource:
    def _fixed_account(self, settings):
        return {
            "status": "online", "token": "token-in-memory", "phone": "13800000000",
            "user_info": {"isPayMember": True, "userIdentifier": "user-1"},
        }



def test_wanda_probe_account_pool_keeps_credentials_out_of_public_state() -> None:
    pool = WandaProbeAccountPool(FakeWandaAccountSource(), lambda: object())
    account = pool.select()

    assert account.token_present is True
    assert account.phone_present is True
    assert account.public_view() == {
        "account_ref": account.account_ref, "online": True, "is_wplus": True,
        "risk_status": "normal", "remaining": 1, "cooldown_until": None,
        "failure_count": 0,
    }
    assert "token-in-memory" not in str(account.public_view())
    assert "13800000000" not in str(account.public_view())


@pytest.mark.asyncio
async def test_live_provider_uses_only_probe_order_endpoints_and_redacts_account_view() -> None:
    from app.probe.account_pool import ProbeAccount

    wanda = FakeWandaHttp()
    provider = WandaDirectProbeProvider(wanda, FakeAccountPool())
    provider.bind_live_seats([
        LiveSeat(
            seat_id="seat-1", label="8排9座", area_code="36", zone_type="W+",
            available=True, wplus=True, original_price_cents=6200,
        ),
        LiveSeat(
            seat_id="seat-2", label="8排10座", area_code="36", zone_type="W+",
            available=True, wplus=True, original_price_cents=6200,
        ),
    ])
    account = ProbeAccount(
        account_ref="account-hash", online=True, is_wplus=True,
        token_present=True, phone_present=True, risk_status="normal", remaining=1,
        token="token-in-memory", phone="13800000000",
    )

    created = await provider.create_probe_order(account=account, show_id="show-1", seat_ids=["seat-1"])
    status = await provider.get_order_status(temporary_order_reference=created.temporary_order_id or "")
    activity = await provider.get_activity_offers(temporary_order_reference=created.temporary_order_id or "")
    cancelled = await provider.cancel_probe_order(temporary_order_reference=created.temporary_order_id or "")
    available = await provider.get_available_seats(show_id="show-1", seat_ids=["seat-1"])

    assert created.temporary_order_id == "temporary-1"
    assert status.order_status == 40
    assert activity.total_pay_price_cents == 3590
    assert cancelled.accepted is True
    assert available.available_seat_ids == {"seat-1"}
    assert "token" not in account.public_view()
    assert "phone" not in account.public_view()
    assert [path for _, path, _ in wanda.calls] == [
        "/order/create_order.api", "/order/order_status.api", 
        "/mkt/activity/secret/list.api", "/order/cancel.api", "/order/real_time_seat.api",
    ]


class FakeSource:
    def __init__(self, seats: list[LiveSeat]) -> None:
        self.seats = seats
        self.calls = 0

    async def get_probe_live_seats(self, show: ShowResolutionResult) -> list[LiveSeat]:
        self.calls += 1
        return self.seats


class FakeProvider:
    def __init__(self) -> None:
        self.bound: list[LiveSeat] = []

    def bind_live_seats(self, seats: list[LiveSeat]) -> None:
        self.bound = list(seats)


class FakeCoordinator:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.requests = []

    async def run(self, request, live_seats):
        self.requests.append(request)
        seat = live_seats[0]
        return ProbeResult(
            probe_id="probe-live-1", show_id=request.show_id, status="SUCCESS",
            release_verified=True, cancel_confirmed=True,
            seat_type_prices=[ProbeSeatTypePrice(
                area_code=seat.area_code, zone_type=seat.zone_type,
                representative_seat_id=seat.seat_id,
                original_price_cents=seat.original_price_cents or 6200,
                member_price_cents=4500,
            )],
        )


def show() -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED", wanda_store_id="store-1", wanda_show_id="show-1",
        movie_name="测试电影", show_date="2026-09-05", start_time="13:35",
        hall_name="IMAX厅", sales_price_fen=6200,
    )


def target_cost(*, request_type: str, target: WandaProbeTarget) -> WandaCostFacts:
    return WandaCostFacts(
        status="PROBE_REQUIRED", request_type=request_type,
        cost_items=([] if request_type == "WPLUS_AREA" else [WandaCostItem(
            seat_label="8排9座", area_code=target.area_code,
            zone_type=target.zone_type, cost_fen=None, cost_source=None,
        )]),
        probe_required=True, probe_targets=[target],
    )


@pytest.mark.asyncio
async def test_probe_cost_resolver_turns_verified_probe_into_canonical_cost() -> None:
    provider = FakeProvider()
    source = FakeSource([LiveSeat(
        seat_id="seat-1", label="8排9座", area_code="36", zone_type="W+",
        available=True, wplus=True, original_price_cents=6200,
    )])
    coordinator = FakeCoordinator(provider)
    resolver = CanonicalWandaProbeCostResolver(coordinator, source, provider=provider)
    facts = target_cost(
        request_type="EXACT_SEATS",
        target=WandaProbeTarget(area_code="36", zone_type="W+", seat_id="seat-1"),
    )

    result = await resolver.resolve(
        show(),
        SeatFactsResult(
            status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
            exact_seats=[ExactSeatFact(
                label="8排9座", seat_label="8排9座", seat_id="seat-1",
                wanda_seat_id="seat-1", area_code="36", zone_type="W+",
                status="AVAILABLE", is_wplus_exclusive=True,
                area_original_price_fen=6200,
            )],
        ),
        facts,
        {"tenant_id": "tenant-1", "shop_id": "shop-1"},
    )

    assert result.status == "COST_READY"
    assert result.probe_executed is True
    assert result.probe_required is False
    assert result.cost_items[0].cost_fen == 4500
    assert result.cost_items[0].cost_source == "LOCKED_ALLOT_SEAT"
    assert coordinator.requests[0].requested_seat_labels == ["8排9座"]
    assert source.calls == 1


@pytest.mark.asyncio
async def test_probe_resolver_rejects_non_wplus_targets_without_locking() -> None:
    provider = FakeProvider()
    source = FakeSource([LiveSeat(
        seat_id="seat-1", label="8排9座", area_code="10", zone_type="普通",
        available=True, wplus=False, original_price_cents=6200,
    )])
    coordinator = FakeCoordinator(provider)
    resolver = CanonicalWandaProbeCostResolver(coordinator, source, provider=provider)
    result = await resolver.resolve(
        show(), SeatFactsResult(status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS"),
        target_cost(
            request_type="EXACT_SEATS",
            target=WandaProbeTarget(area_code="10", zone_type="普通", seat_id="seat-1"),
        ),
        {"tenant_id": "tenant-1", "shop_id": "shop-1"},
    )

    assert result.status == "COST_UNAVAILABLE"
    assert result.probe_executed is False
    assert coordinator.requests == []
    assert source.calls == 0


class ProbeRequiredCost:
    def resolve(self, show_facts, seat_facts):
        return target_cost(
            request_type="WPLUS_AREA",
            target=WandaProbeTarget(area_code="36", zone_type="W+", seat_id="seat-1"),
        )


class ProbeCostResolver:
    async def resolve(self, show_facts, seat_facts, cost_facts, identity):
        return WandaCostFacts(
            status="COST_READY", request_type="WPLUS_AREA",
            cost_items=[WandaCostItem(
                area_code="36", zone_type="W+", cost_fen=4500,
                cost_source="LOCKED_ALLOT_SEAT",
            )], probe_executed=True,
        )


class Route:
    async def resolve(self, recognition):
        from app.cinema_route_v2.models import CinemaRouteResult
        return CinemaRouteResult(
            route="WANDA_SELF", wanda_city_id="city-1", wanda_store_id="store-1",
            wanda_city_name="广州", wanda_cinema_name="广州测试万达", resolution_reason="test",
        )


class ShowService:
    async def resolve(self, request):
        return show()


class Seats:
    async def resolve(self, request, *, manual_mark_detector=None):
        return SeatFactsResult(
            status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
            wanda_store_id="store-1", wanda_show_id="show-1", has_manual_mark=False,
        )




@pytest.mark.asyncio
async def test_canonical_runtime_invokes_probe_cost_resolver_before_pricing(tmp_path: Path) -> None:
    runtime = CanonicalQuoteRuntime(
        recognition_service=None, cinema_route_service=Route(), show_resolve_service=ShowService(),
        seat_facts_service=Seats(), cost_resolution_service=ProbeRequiredCost(),
        wanda_pricing_service=WandaPricingV2Service(V4PricingEngine()), selected_seat_quote_service=None,
        pricing_rules_provider=PricingRulesSnapshot(enabled=False),
        quote_service=QuoteV2Service(QuoteRecordStore(tmp_path / "quotes.json")),
        liangpiao_facts_adapter=None, probe_cost_resolver=ProbeCostResolver(),
    )
    result = await runtime.quote_recognition(
        RecognitionResult(city_text="广州", cinema_text="广州测试万达", movie="测试电影"),
        identity={
            "event_id": "event-1", "tenant_id": "tenant-1", "shop_id": "shop-1",
            "buyer_id": "buyer-1", "chat_id": "chat-1", "purchase_context_id": "purchase-1",
        },
    )

    assert result["status"] == "QUOTED"
    assert result["quote"]["cost_source"] == "LOCKED_ALLOT_SEAT"
    assert result["quote"]["quote_state"] == "PREVIEW"
