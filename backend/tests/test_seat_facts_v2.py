from __future__ import annotations

from typing import Any

import pytest

from app.seat_facts_v2.service import SeatFactsV2Service


class FakeSeatSource:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or {"code": 0, "data": {"realtimeSeats": {"area": []}}}
        self.error = error
        self.calls: list[str] = []

    async def get_realtime_seats(self, wanda_show_id: str) -> dict[str, Any]:
        self.calls.append(wanda_show_id)
        if self.error:
            raise self.error
        return self.response


class FakeManualMarkDetector:
    def __init__(self, value: bool | None) -> None:
        self.value = value
        self.calls: list[str] = []

    async def detect(self, image_url: str) -> bool | None:
        self.calls.append(image_url)
        return self.value


def seat(
    seat_id: str,
    label: str,
    *,
    status: int = 1,
    area_id: str = "wplus-1",
    row: int | None = None,
    col: int | None = None,
    pay_member_status: int = 0,
) -> dict[str, Any]:
    return {
        "seatId": seat_id,
        "name": label,
        "status": status,
        "payMemberSeatStatus": pay_member_status,
        "row": row or int(label.split("排")[0]),
        "column": col or int(label.split("排")[1].removesuffix("座")),
    }


def realtime_response(
    *,
    areas: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"code": 0, "data": {"realtimeSeats": {"area": areas or []}}}


def area(
    area_id: str,
    name: str,
    seats: list[dict[str, Any]],
    *,
    realtime_price: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "areaId": area_id,
        "areaName": name,
        "seat": seats,
    }
    if realtime_price is not None:
        value["areaPrice"] = {"salesPrice": realtime_price}
    return value


def request(**updates: Any) -> dict[str, Any]:
    return {
        "route": "WANDA_SELF",
        "wanda_store_id": "315",
        "wanda_show_id": "101294120",
        "selected_seats": [],
        "has_selected_seats": False,
        "has_manual_mark": False,
        "image_url": "https://img.example/image.jpg",
        **updates,
    }


@pytest.mark.asyncio
async def test_manual_true_with_no_seats_is_wplus_area() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("wplus-1", "W+区域", [seat("1", "10排15座", pay_member_status=1)], realtime_price=5816)]))
    result = await SeatFactsV2Service(source).resolve(request(has_manual_mark=True))
    assert result.seat_request_type == "WPLUS_AREA"
    assert result.quote_scope == "WPLUS_AREA"
    assert result.status == "WPLUS_AREA_RESOLVED"
    assert result.has_selected_seats is False
    assert result.exact_seats == []
    assert result.wplus_areas[0].area_original_price_fen == 5816
    assert result.wplus_price_authoritative is False
    assert result.authoritative_wplus_price_fen is None
    assert result.wplus_areas[0].available_seat_ids == ["1"]


@pytest.mark.asyncio
async def test_manual_true_with_selected_seats_stays_wplus_area() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("wplus-1", "W+区域", [seat("1", "10排15座", pay_member_status=1)])]))
    result = await SeatFactsV2Service(source).resolve(
        request(has_manual_mark=True, selected_seats=["10排15座"], has_selected_seats=True),
    )
    assert result.seat_request_type == "WPLUS_AREA"
    assert result.quote_scope == "WPLUS_AREA"
    assert result.has_selected_seats is True
    assert result.exact_seats == []
    assert result.wplus_areas


@pytest.mark.asyncio
async def test_manual_false_with_selected_seat_is_exact_seats() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("s-15", "10排15座", pay_member_status=1)])]))
    result = await SeatFactsV2Service(source).resolve(
        request(selected_seats=["10 排 15 座"], has_selected_seats=True),
    )
    assert result.seat_request_type == "EXACT_SEATS"
    assert result.quote_scope == "EXACT_SEATS"
    assert result.has_selected_seats is True
    assert result.status == "EXACT_SEATS_RESOLVED"
    assert result.exact_seats[0].wanda_seat_id == "s-15"
    assert result.exact_seats[0].status == "AVAILABLE"
    assert result.exact_seats[0].is_wplus_exclusive is True
    assert result.exact_seats[0].is_wplus is True


@pytest.mark.asyncio
async def test_manual_false_without_selected_seat_is_wplus_area() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("wplus-1", "W+区域", [seat("1", "10排15座", pay_member_status=1)])]))
    result = await SeatFactsV2Service(source).resolve(request())
    assert result.seat_request_type == "WPLUS_AREA"
    assert result.quote_scope == "WPLUS_AREA"
    assert result.has_selected_seats is False
    assert result.status == "WPLUS_AREA_RESOLVED"


@pytest.mark.asyncio
async def test_manual_unknown_requires_manual_mark_and_does_not_query_seats() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("wplus-1", "W+区域", [seat("1", "10排15座", pay_member_status=1)])]))
    result = await SeatFactsV2Service(source).resolve(request(has_manual_mark=None))
    assert result.seat_request_type == "MANUAL_MARK_REQUIRED"
    assert result.quote_scope == "MISSING_CONTEXT"
    assert result.status == "MANUAL_MARK_REQUIRED"
    assert source.calls == []


@pytest.mark.asyncio
async def test_unknown_manual_mark_can_be_resolved_by_one_explicit_detector_call() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("1", "10排15座")])]))
    detector = FakeManualMarkDetector(False)
    result = await SeatFactsV2Service(source).resolve(
        request(has_manual_mark=None, selected_seats=["10排15座"], has_selected_seats=True),
        manual_mark_detector=detector,
    )
    assert result.status == "EXACT_SEATS_RESOLVED"
    assert result.quote_scope == "EXACT_SEATS"
    assert result.has_selected_seats is True
    assert detector.calls == ["https://img.example/image.jpg"]


@pytest.mark.asyncio
async def test_two_available_exact_seats_resolve() -> None:
    source = FakeSeatSource(realtime_response(areas=[area(
        "standard", "普通区", [seat("s-15", "10排15座"), seat("s-16", "10排16座")],
    )]))
    result = await SeatFactsV2Service(source).resolve(
        request(selected_seats=["10排15座", "10排16座"], has_selected_seats=True),
    )
    assert result.status == "EXACT_SEATS_RESOLVED"
    assert [item.wanda_seat_id for item in result.exact_seats] == ["s-15", "s-16"]


@pytest.mark.asyncio
async def test_missing_exact_seat_is_not_replaced_by_neighbor() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("s-16", "10排16座")])]))
    result = await SeatFactsV2Service(source).resolve(
        request(selected_seats=["10排15座"], has_selected_seats=True),
    )
    assert result.status == "SEAT_NOT_FOUND"
    assert result.exact_seats == []


@pytest.mark.asyncio
async def test_unavailable_exact_seat_is_not_resolved() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("s-15", "10排15座", status=0)])]))
    result = await SeatFactsV2Service(source).resolve(
        request(selected_seats=["10排15座"], has_selected_seats=True),
    )
    assert result.status == "SEAT_UNAVAILABLE"
    assert result.exact_seats[0].status == "OCCUPIED"


@pytest.mark.asyncio
async def test_normal_seat_can_have_valid_area_member_price() -> None:
    source = FakeSeatSource(realtime_response(areas=[{
        "areaId": "normal",
        "areaPrice": {"salesPrice": 6200},
        "wPlusActivity": {"price": 4800, "activityCode": "hint-only"},
        "seat": [seat("normal-1", "10排15座", pay_member_status=0)],
    }]))
    result = await SeatFactsV2Service(source).resolve(request(
        selected_seats=["10排15座"], has_selected_seats=True,
    ))
    fact = result.exact_seats[0]
    assert result.status == "EXACT_SEATS_RESOLVED"
    assert fact.is_wplus_exclusive is False
    assert fact.area_original_price_fen == 6200
    assert fact.area_member_price_fen == 4800
    assert fact.has_valid_area_member_price is True
    assert fact.area_member_activity_code_hint == "hint-only"


@pytest.mark.asyncio
async def test_nested_wplus_activity_is_returned_as_provider_price_fact() -> None:
    source = FakeSeatSource(realtime_response(areas=[{
        "areaId": "wplus-1",
        "wPlusActivity": {"price": 5816, "activityName": "W+会员专享优惠"},
        "areaPrice": {"salesPrice": 6200},
        "seat": [seat("1", "10排15座", pay_member_status=1)],
    }]))
    result = await SeatFactsV2Service(source).resolve(request())
    assert result.status == "WPLUS_AREA_RESOLVED"
    assert result.wplus_areas[0].area_original_price_fen == 6200
    assert result.wplus_areas[0].area_member_price_fen == 5816
    assert result.wplus_areas[0].has_valid_area_member_price is True
    assert result.wplus_price_authoritative is False


@pytest.mark.asyncio
async def test_invalid_area_member_price_is_not_corrected() -> None:
    for member_price in (7000, 0):
        source = FakeSeatSource(realtime_response(areas=[{
            "areaId": "normal",
            "areaPrice": {"salesPrice": 6200},
            "wPlusActivity": {"price": member_price},
            "seat": [seat("1", "10排15座", pay_member_status=0)],
        }]))
        result = await SeatFactsV2Service(source).resolve(request(
            selected_seats=["10排15座"], has_selected_seats=True,
        ))
        fact = result.exact_seats[0]
        assert fact.has_valid_area_member_price is False


@pytest.mark.asyncio
async def test_missing_area_activity_has_no_member_price() -> None:
    source = FakeSeatSource(realtime_response(areas=[{
        "areaId": "normal",
        "areaPrice": {"salesPrice": 6200},
        "seat": [seat("1", "10排15座", pay_member_status=0)],
    }]))
    result = await SeatFactsV2Service(source).resolve(request(
        selected_seats=["10排15座"], has_selected_seats=True,
    ))
    fact = result.exact_seats[0]
    assert fact.area_member_price_fen is None
    assert fact.has_valid_area_member_price is False


@pytest.mark.asyncio
async def test_area_price_aliases_are_supported() -> None:
    aliases = ("salePrice", "price", "originalPrice")
    for original_key in aliases:
        source = FakeSeatSource(realtime_response(areas=[{
            "areaId": original_key,
            "areaPrice": {original_key: 6200, "activityPrice": 4800},
            "seat": [seat("1", "10排15座", pay_member_status=0)],
        }]))
        result = await SeatFactsV2Service(source).resolve(request(
            selected_seats=["10排15座"], has_selected_seats=True,
        ))
        fact = result.exact_seats[0]
        assert fact.area_original_price_fen == 6200
        assert fact.area_member_price_fen == 4800
        assert fact.has_valid_area_member_price is True


@pytest.mark.asyncio
async def test_activity_price_does_not_make_normal_seat_wplus_exclusive() -> None:
    source = FakeSeatSource(realtime_response(areas=[{
        "areaId": "standard",
        "wPlusActivity": {"price": 5816, "activityName": "W+会员专享优惠"},
        "areaPrice": {"salesPrice": 6200},
        "seat": [seat("1", "10排15座", pay_member_status=0)],
    }]))
    result = await SeatFactsV2Service(source).resolve(request(
        selected_seats=["10排15座"], has_selected_seats=True,
    ))
    fact = result.exact_seats[0]
    assert fact.is_wplus_exclusive is False
    assert fact.has_valid_area_member_price is True


@pytest.mark.asyncio
async def test_multiple_area_prices_are_all_returned_without_selection() -> None:
    source = FakeSeatSource(realtime_response(areas=[
        {
            "areaId": "area-a",
            "areaPrice": {"salesPrice": 6200},
            "wPlusActivity": {"price": 4800, "activityCode": "hint-a"},
            "seat": [seat("a", "1排1座", pay_member_status=0)],
        },
        {
            "areaId": "area-b",
            "areaPrice": {"salesPrice": 7000},
            "wPlusActivity": {"price": 5500, "activityCode": "hint-b"},
            "seat": [seat("b", "2排2座", pay_member_status=0)],
        },
    ]))
    result = await SeatFactsV2Service(source).resolve(request())
    assert [item.area_code for item in result.wplus_areas] == ["area-a", "area-b"]
    assert [item.area_member_price_fen for item in result.wplus_areas] == [4800, 5500]
    assert result.wplus_areas[0].area_member_activity_code_hint == "hint-a"
    assert result.wplus_areas[1].area_member_activity_code_hint == "hint-b"


@pytest.mark.asyncio
async def test_multiple_wplus_areas_are_all_returned() -> None:
    source = FakeSeatSource(realtime_response(areas=[
        area("wplus-1", "W+区域A", [seat("a", "1排1座", pay_member_status=1)], realtime_price=5800),
        area("wplus-2", "会员活动区B", [seat("b", "2排2座", pay_member_status=1)], realtime_price=5900),
        area("standard", "普通区", [seat("c", "3排3座")]),
    ]))
    result = await SeatFactsV2Service(source).resolve(request())
    assert result.status == "WPLUS_AREA_RESOLVED"
    assert [item.area_code for item in result.wplus_areas] == ["wplus-1", "wplus-2"]
    assert result.wplus_areas[0].wplus_available is True


@pytest.mark.asyncio
async def test_no_wplus_area_is_explicit_and_does_not_probe() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("c", "3排3座")])]))
    result = await SeatFactsV2Service(source).resolve(request())
    assert result.status == "WPLUS_AREA_RESOLVED"
    assert result.wplus_areas == []
    assert result.resolution_reason == "NO_WPLUS_AREA_REPORTED"


@pytest.mark.asyncio
async def test_provider_failure_is_unavailable() -> None:
    source = FakeSeatSource(error=TimeoutError("timeout"))
    result = await SeatFactsV2Service(source).resolve(request())
    assert result.status == "PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_show_id_is_required_and_does_not_resolve_previous_phase() -> None:
    source = FakeSeatSource(realtime_response(areas=[]))
    result = await SeatFactsV2Service(source).resolve(request(wanda_show_id=None))
    assert result.status == "INPUT_INCOMPLETE"
    assert source.calls == []


@pytest.mark.asyncio
async def test_liangpiao_ids_in_input_debug_are_ignored() -> None:
    source = FakeSeatSource(realtime_response(areas=[area("standard", "普通区", [seat("s-15", "10排15座")])]))
    result = await SeatFactsV2Service(source).resolve(request(
        selected_seats=["10排15座"],
        has_selected_seats=True,
        raw_provider_result={"data": {"finalResults": {"showId": "liangpiao-show", "cinemaId": 4748}}},
    ))
    assert result.status == "EXACT_SEATS_RESOLVED"
    assert result.wanda_show_id == "101294120"
