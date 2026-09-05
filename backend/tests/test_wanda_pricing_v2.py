from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.pricing.models import PricingRulesSnapshot
from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.models import WandaCostFacts, WandaCostItem, WandaProbeTarget
from app.wanda_pricing_v2.service import WandaPricingV2Service, price_wanda_cost


def show(*, cost: int | None = 4490, original: int | None = 6200) -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED",
        wanda_store_id="store-1",
        wanda_show_id="show-1",
        sales_price_fen=original,
        wplus_activity_price_fen=cost,
    )


def seat_fact(
    seat_id: str,
    label: str,
    *,
    area: str = "36",
    zone: str = "W+",
    original: int | None = 6200,
    wplus: bool = True,
) -> ExactSeatFact:
    return ExactSeatFact(
        label=label,
        seat_label=label,
        wanda_seat_id=seat_id,
        seat_id=seat_id,
        area_code=area,
        zone_type=zone,
        status="AVAILABLE",
        is_wplus_exclusive=wplus,
        area_original_price_fen=original,
        area_member_price_fen=None,
        has_valid_area_member_price=False,
    )


def exact_cost(items: list[WandaCostItem]) -> WandaCostFacts:
    return WandaCostFacts(
        status="COST_READY",
        request_type="EXACT_SEATS",
        cost_items=items,
    )


def area_cost(cost: int = 4490) -> WandaCostFacts:
    return WandaCostFacts(
        status="COST_READY",
        request_type="WPLUS_AREA",
        cost_items=[WandaCostItem(
            seat_label=None, area_code=None, zone_type="W+",
            cost_fen=cost, cost_source="SHOWTIME_WPLUS",
        )],
    )


def rules(**updates: object) -> PricingRulesSnapshot:
    values = {"enabled": True, "revision": 7, "rule_version": "pricing-r7-test", **updates}
    return PricingRulesSnapshot(**values)


def test_wplus_area_cost_goes_through_existing_engine() -> None:
    result = price_wanda_cost(area_cost(), show(), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules(), ticket_count=1)

    assert result.status == "PRICED"
    assert result.unit_sell_price_fen == 5910
    assert result.total_sell_price_fen == 5910
    assert result.unit_sell_price_fen != 4490
    assert result.pricing_rule_revision == 7
    assert result.pricing_rule_version == "pricing-r7-test"


def test_wplus_area_known_quantity_calculates_total_from_engine_result() -> None:
    result = price_wanda_cost(area_cost(), show(), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules(), ticket_count=2)

    assert (result.unit_sell_price_fen, result.total_sell_price_fen) == (5910, 11820)
    assert result.ticket_count == 2
    assert result.needs_ticket_count is False


def test_wplus_area_unknown_quantity_keeps_unit_price() -> None:
    result = price_wanda_cost(area_cost(), show(), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules())

    assert result.unit_sell_price_fen == 5910
    assert result.total_sell_price_fen is None
    assert result.ticket_count is None
    assert result.needs_ticket_count is True


def test_exact_one_seat_is_priced_by_existing_engine() -> None:
    cost = exact_cost([WandaCostItem(
        seat_label="8排9座", area_code="10", zone_type="普通",
        cost_fen=4800, cost_source="REALTIME_AREA_WPLUS",
    )])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[seat_fact("s1", "8排9座", area="10", zone="普通", wplus=False)],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.status == "PRICED"
    assert result.ticket_count == 1
    assert result.needs_ticket_count is False
    assert result.seat_quotes[0].cost_fen == 4800
    assert result.seat_quotes[0].sell_price_fen == 4900
    assert result.total_sell_price_fen == 4900


def test_exact_same_cost_seats_are_priced_independently() -> None:
    costs = exact_cost([
        WandaCostItem(seat_label="8排9座", area_code="10", zone_type="普通", cost_fen=4800, cost_source="REALTIME_AREA_WPLUS"),
        WandaCostItem(seat_label="8排10座", area_code="10", zone_type="普通", cost_fen=4800, cost_source="REALTIME_AREA_WPLUS"),
    ])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[
            seat_fact("s1", "8排9座", area="10", zone="普通", wplus=False),
            seat_fact("s2", "8排10座", area="10", zone="普通", wplus=False),
        ],
    )

    result = price_wanda_cost(costs, show(cost=None), facts, rules())

    assert [item.sell_price_fen for item in result.seat_quotes] == [4900, 4900]
    assert result.ticket_count == 2
    assert result.needs_ticket_count is False
    assert result.total_sell_price_fen == 9800


def test_exact_different_cost_seats_are_priced_and_summed_independently() -> None:
    costs = exact_cost([
        WandaCostItem(seat_label="8排9座", area_code="10", zone_type="普通", cost_fen=4800, cost_source="REALTIME_AREA_WPLUS"),
        WandaCostItem(seat_label="8排10座", area_code="36", zone_type="W+", cost_fen=5200, cost_source="REALTIME_AREA_WPLUS"),
    ])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[
            seat_fact("s1", "8排9座", area="10", zone="普通", wplus=False),
            seat_fact("s2", "8排10座", area="36", zone="W+", original=6500, wplus=True),
        ],
    )

    result = price_wanda_cost(costs, show(cost=None), facts, rules())

    assert [item.cost_fen for item in result.seat_quotes] == [4800, 5200]
    assert [item.sell_price_fen for item in result.seat_quotes] == [4900, 6210]
    assert result.ticket_count == 2
    assert result.needs_ticket_count is False
    assert result.total_sell_price_fen == 11110
    assert result.unit_sell_price_fen is None


def test_normal_seat_member_cost_source_is_supported() -> None:
    cost = exact_cost([WandaCostItem(
        seat_label="8排9座", area_code="10", zone_type="普通",
        cost_fen=4800, cost_source="REALTIME_AREA_WPLUS",
    )])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[seat_fact("s1", "8排9座", area="10", zone="普通", wplus=False)],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.seat_quotes[0].cost_source == "REALTIME_AREA_WPLUS"
    assert result.status == "PRICED"


def test_wplus_exact_seat_member_cost_source_is_supported() -> None:
    cost = exact_cost([WandaCostItem(
        seat_label="8排9座", area_code="36", zone_type="W+",
        cost_fen=4500, cost_source="REALTIME_AREA_WPLUS",
    )])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[seat_fact("s1", "8排9座", area="36", zone="W+", wplus=True)],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.status == "PRICED"
    assert result.seat_quotes[0].cost_fen == 4500


def test_probe_required_never_calls_pricing_engine() -> None:
    class FailingEngine:
        def quote(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("pricing engine must not be called")

    cost = WandaCostFacts(
        status="PROBE_REQUIRED", request_type="WPLUS_AREA", probe_required=True,
        probe_targets=[WandaProbeTarget(area_code="40", zone_type="W+", seat_id="s1")],
    )
    result = WandaPricingV2Service(engine=FailingEngine()).price(cost, show(cost=None), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules())

    assert result.status == "PRICING_REQUIRES_COST"
    assert result.probe_targets == [{"area_code": "40", "zone_type": "W+", "seat_id": "s1"}]


def test_missing_cost_is_not_replaced_by_original_price() -> None:
    cost = WandaCostFacts(
        status="PROBE_REQUIRED", request_type="EXACT_SEATS", probe_required=True,
        cost_items=[WandaCostItem(
            seat_label="8排9座", area_code="40", zone_type="普通",
            cost_fen=None, cost_source=None,
        )],
    )
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[seat_fact("s1", "8排9座", area="40", zone="普通", original=6200, wplus=False)],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.status == "PRICING_REQUIRES_COST"
    assert result.unit_sell_price_fen is None


def test_invalid_showtime_original_is_input_incomplete_not_cost_fallback() -> None:
    result = price_wanda_cost(area_cost(), show(original=None), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules())

    assert result.status == "INPUT_INCOMPLETE"
    assert result.unit_sell_price_fen is None


def test_pricing_rule_revision_and_version_are_preserved() -> None:
    result = price_wanda_cost(area_cost(), show(), SeatFactsResult(
        status="WPLUS_AREA_RESOLVED", seat_request_type="WPLUS_AREA",
        wanda_show_id="show-1", has_manual_mark=False,
    ), rules(revision=19, rule_version="pricing-r19-snapshot"))

    assert result.pricing_rule_revision == 19
    assert result.pricing_rule_version == "pricing-r19-snapshot"


def test_locked_allot_seat_is_reserved_as_a_future_cost_source() -> None:
    cost = exact_cost([WandaCostItem(
        seat_label="8排9座", area_code="36", zone_type="W+",
        cost_fen=4500, cost_source="LOCKED_ALLOT_SEAT",
    )])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[seat_fact("s1", "8排9座")],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.status == "PRICED"
    assert result.cost_sources == ["LOCKED_ALLOT_SEAT"]


def test_cost_sources_are_preserved_for_each_exact_seat() -> None:
    cost = exact_cost([
        WandaCostItem(seat_label="8排9座", area_code="10", zone_type="普通", cost_fen=4800, cost_source="REALTIME_AREA_WPLUS"),
        WandaCostItem(seat_label="8排10座", area_code="36", zone_type="W+", cost_fen=5200, cost_source="LOCKED_ALLOT_SEAT"),
    ])
    facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        wanda_show_id="show-1", has_manual_mark=False,
        exact_seats=[
            seat_fact("s1", "8排9座", area="10", zone="普通", wplus=False),
            seat_fact("s2", "8排10座", area="36", zone="W+", original=6500),
        ],
    )

    result = price_wanda_cost(cost, show(cost=None), facts, rules())

    assert result.cost_sources == ["REALTIME_AREA_WPLUS", "LOCKED_ALLOT_SEAT"]
    assert [item.cost_source for item in result.seat_quotes] == result.cost_sources


def test_image_total_price_is_not_accepted_as_cost_input() -> None:
    with pytest.raises(ValidationError):
        WandaCostItem.model_validate({
            "seat_label": None,
            "cost_fen": 4490,
            "cost_source": "SHOWTIME_WPLUS",
            "image_total_price_fen": 4490,
        })


def test_liangpiao_price_source_is_not_accepted() -> None:
    with pytest.raises(ValidationError):
        WandaCostItem.model_validate({
            "seat_label": "8排9座",
            "cost_fen": 4490,
            "cost_source": "LIANGPIAO_FINAL_PRICE",
        })
