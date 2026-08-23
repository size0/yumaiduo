from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas import Recognition, SeatZoneType
from app.wanda_quote_domain import (
    SeatFact,
    _bounded_quote_for_seat,
    _locked_offer_unit_cents,
    _seat_facts,
    _select_seats,
)


def recognition(*, selected: list[str] | None = None) -> Recognition:
    selected = selected or []
    return Recognition.model_validate({
        "image_type": "SEAT_MAP",
        "cinema": "测试万达影城",
        "movie": "奥德赛",
        "date": "2026-08-23",
        "showtime": "17:15",
        "seat_zone_types": ["W+"],
        "official_selection": {
            "is_selected": bool(selected),
            "selected_seat_numbers": selected,
            "selected_count": len(selected),
        },
    })


def seat(identifier: str, label: str, *, area: str = "36", original: int = 6490, member: int = 5540) -> SeatFact:
    return SeatFact(identifier, area, original, member, 0, label, SeatZoneType.WPLUS)


def test_domain_parses_only_realtime_available_seats_with_bounded_prices() -> None:
    facts = _seat_facts({"data": {"area": [{
        "areaCode": "36",
        "areaName": "W+专享",
        "areaPrice": {"salesPrice": 6490, "channelFee": 200, "wPlusActivity": {"price": 5540}},
        "seat": [
            {"seatId": "available", "status": 1, "row": "8", "column": "9"},
            {"seatId": "sold", "status": 0, "row": "8", "column": "10"},
        ],
    }]}})

    assert facts == [SeatFact("available", "36", 6490, 5540, 200, "8排9座", SeatZoneType.WPLUS)]


def test_domain_exact_selection_must_match_every_official_seat() -> None:
    selected, exact, zone = _select_seats(
        recognition(selected=["8排9座", "8排10座"]),
        [seat("a", "8排9座"), seat("b", "8排10座")],
        2,
    )
    assert [item.label for item in selected] == ["8排9座", "8排10座"]
    assert exact is True
    assert zone is SeatZoneType.WPLUS

    with pytest.raises(HTTPException, match="官方已选座"):
        _select_seats(recognition(selected=["8排9座", "8排10座"]), [seat("a", "8排9座")], 2)


def test_domain_area_probe_is_deterministic_and_ignores_non_wplus_seats() -> None:
    regular = SeatFact("regular", "1", 5990, 5240, 0, "8排8座", SeatZoneType.REGULAR)
    selected, exact, zone = _select_seats(
        recognition(),
        [seat("later", "8排10座", area="36"), regular, seat("first", "8排9座", area="35")],
        1,
    )
    assert [item.seat_id for item in selected] == ["first"]
    assert exact is False
    assert zone is SeatZoneType.WPLUS


def test_domain_quote_stays_between_member_floor_and_original_ceiling_and_requires_unique_offer() -> None:
    quote = _bounded_quote_for_seat(
        seat("a", "8排9座"),
        SeatZoneType.WPLUS,
        wplus_adjustment_cents=-290,
        wplus_member_price_threshold_cents=6000,
        regular_adjustment_cents=100,
    )
    assert quote == 6200

    offers = {"activities": [{
        "name": "W+会员专享优惠", "able": True,
        "allot_seat": {"totalPayPrice": 11080},
    }]}
    assert _locked_offer_unit_cents(offers, quantity=2, allow_friday=False) == 5540
