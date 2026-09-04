from __future__ import annotations

from app.seat_facts_v2.models import ExactSeatFact, SeatFactsResult, WplusAreaFact
from app.show_resolve_v2.models import ShowResolutionResult
from app.wanda_cost_v2.service import resolve_wanda_cost


def show(price: int | None = 4490) -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED",
        wanda_store_id="store-1",
        wanda_show_id="show-1",
        wplus_activity_price_fen=price,
    )


def exact(
    seat_id: str,
    *,
    label: str = "8排9座",
    area: str | None = "36",
    zone: str | None = "W+",
    member: int | None = 4800,
    valid: bool = True,
    wplus: bool = True,
    status: str = "AVAILABLE",
) -> ExactSeatFact:
    return ExactSeatFact(
        label=label,
        seat_label=label,
        wanda_seat_id=seat_id,
        seat_id=seat_id,
        area_code=area,
        zone_type=zone,
        status=status,
        is_wplus_exclusive=wplus,
        area_original_price_fen=6200,
        area_member_price_fen=member,
        has_valid_area_member_price=valid,
    )


def area(
    area_code: str = "36",
    *,
    zone: str = "W+",
    available: list[str] | None = None,
) -> WplusAreaFact:
    available = ["probe-1"] if available is None else available
    return WplusAreaFact(
        area_code=area_code,
        zone_type=zone,
        available_seat_ids=available,
        available_seat_count=len(available),
        wplus_available=bool(available),
    )


def seats(
    *,
    selected: list[ExactSeatFact] | None = None,
    manual: bool = False,
    wplus_areas: list[WplusAreaFact] | None = None,
    request_type: str | None = None,
) -> SeatFactsResult:
    selected = [] if selected is None else selected
    return SeatFactsResult(
        status="EXACT_SEATS_RESOLVED" if selected and not manual else "WPLUS_AREA_RESOLVED",
        seat_request_type=request_type or ("WPLUS_AREA" if manual or not selected else "EXACT_SEATS"),
        wanda_store_id="store-1",
        wanda_show_id="show-1",
        has_manual_mark=manual,
        exact_seats=selected,
        wplus_areas=wplus_areas or [],
    )


def test_unselected_area_uses_showtime_wplus_without_probe() -> None:
    result = resolve_wanda_cost(show(), seats(wplus_areas=[area(available=["s1"])]))

    assert result.status == "COST_READY"
    assert result.request_type == "WPLUS_AREA"
    assert result.cost_items[0].cost_fen == 4490
    assert result.cost_items[0].cost_source == "SHOWTIME_WPLUS"
    assert result.probe_required is False
    assert result.probe_targets == []


def test_unselected_area_with_manual_mark_uses_showtime_wplus() -> None:
    result = resolve_wanda_cost(show(), seats(manual=True, wplus_areas=[area()]))

    assert result.status == "COST_READY"
    assert result.request_type == "WPLUS_AREA"
    assert result.cost_items[0].cost_source == "SHOWTIME_WPLUS"


def test_manual_mark_ignores_formal_selected_seats() -> None:
    result = resolve_wanda_cost(
        show(),
        seats(selected=[exact("s1", member=9999)], manual=True, wplus_areas=[area()]),
    )

    assert result.request_type == "WPLUS_AREA"
    assert result.cost_items[0].seat_label is None
    assert result.cost_items[0].cost_fen == 4490


def test_unselected_area_without_showtime_price_returns_probe_target() -> None:
    result = resolve_wanda_cost(show(None), seats(wplus_areas=[area("40", available=["s1"])]))

    assert result.status == "PROBE_REQUIRED"
    assert result.request_type == "WPLUS_AREA"
    assert result.cost_items == []
    assert [item.model_dump() for item in result.probe_targets] == [{"area_code": "40", "zone_type": "W+", "seat_id": "s1"}]
    assert result.probe_executed is False


def test_unselected_area_without_available_wplus_is_unavailable() -> None:
    result = resolve_wanda_cost(show(None), seats(wplus_areas=[area("40", available=[])]))

    assert result.status == "COST_UNAVAILABLE"
    assert result.probe_required is True
    assert result.probe_targets == []


def test_normal_exact_seat_uses_area_member_price() -> None:
    result = resolve_wanda_cost(
        show(None), seats(selected=[exact("s1", area="10", zone="普通", member=4800, wplus=False)]),
    )

    assert result.status == "COST_READY"
    assert result.request_type == "EXACT_SEATS"
    assert [item.model_dump() for item in result.cost_items] == [{
        "seat_label": "8排9座", "area_code": "10", "zone_type": "普通",
        "cost_fen": 4800, "cost_source": "REALTIME_AREA_WPLUS",
    }]
    assert result.probe_required is False


def test_exact_wplus_seat_uses_area_member_price() -> None:
    result = resolve_wanda_cost(show(None), seats(selected=[exact("s1", member=4500)]))

    assert result.status == "COST_READY"
    assert result.cost_items[0].cost_fen == 4500
    assert result.cost_items[0].cost_source == "REALTIME_AREA_WPLUS"


def test_exact_seat_without_valid_area_member_price_requires_probe() -> None:
    result = resolve_wanda_cost(
        show(None), seats(selected=[exact("s1", area="40", zone="普通", member=None, valid=False, wplus=False)]),
    )

    assert result.status == "PROBE_REQUIRED"
    assert result.cost_items[0].cost_fen is None
    assert result.cost_items[0].cost_source is None
    assert [item.model_dump() for item in result.probe_targets] == [{"area_code": "40", "zone_type": "普通", "seat_id": "s1"}]


def test_same_exact_area_and_zone_get_one_probe_target() -> None:
    selected = [
        exact("s1", label="8排9座", area="40", zone="普通", member=None, valid=False, wplus=False),
        exact("s2", label="8排10座", area="40", zone="普通", member=None, valid=False, wplus=False),
    ]
    result = resolve_wanda_cost(show(None), seats(selected=selected))

    assert result.status == "PROBE_REQUIRED"
    assert [item.model_dump() for item in result.probe_targets] == [{"area_code": "40", "zone_type": "普通", "seat_id": "s1"}]
    assert len(result.cost_items) == 2


def test_different_exact_area_and_zone_get_one_target_each() -> None:
    selected = [
        exact("s1", label="8排9座", area="40", zone="普通", member=None, valid=False, wplus=False),
        exact("s2", label="8排10座", area="41", zone="W+", member=None, valid=False),
    ]
    result = resolve_wanda_cost(show(None), seats(selected=selected))

    assert result.status == "PROBE_REQUIRED"
    assert [item.model_dump() for item in result.probe_targets] == [
        {"area_code": "40", "zone_type": "普通", "seat_id": "s1"},
        {"area_code": "41", "zone_type": "W+", "seat_id": "s2"},
    ]


def test_original_area_price_never_replaces_missing_member_price() -> None:
    result = resolve_wanda_cost(
        show(None), seats(selected=[exact("s1", member=None, valid=False)]),
    )

    assert result.cost_items[0].cost_fen is None
    assert result.cost_items[0].cost_source is None
    assert result.original_price_used_when_member_missing is False


def test_invalid_showtime_price_falls_back_to_probe() -> None:
    result = resolve_wanda_cost(show(0), seats(wplus_areas=[area("40", available=["s1"])]))

    assert result.status == "PROBE_REQUIRED"
    assert result.probe_required is True
    assert result.cost_items == []


def test_incomplete_show_facts_are_rejected() -> None:
    incomplete = ShowResolutionResult(status="INPUT_INCOMPLETE")
    result = resolve_wanda_cost(incomplete, seats())

    assert result.status == "INPUT_INCOMPLETE"
    assert result.cost_items == []
    assert result.probe_targets == []


def test_resolver_has_no_probe_or_pricing_side_effects() -> None:
    result = resolve_wanda_cost(show(4490), seats(wplus_areas=[area()]))

    assert result.probe_executed is False
    assert result.pricing_called is False
    assert result.normal_seat_member_price_supported is True
