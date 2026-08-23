from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import HTTPException

from .schemas import Recognition, SeatZoneType


WPLUS_STANDARD_NAME: Final = "W+会员专享优惠"
WPLUS_FRIDAY_NAME: Final = "W+周五会员日专享"
REGULAR_SEAT_MARKUP_CENTS: Final = 100


@dataclass(frozen=True)
class SeatFact:
    seat_id: str
    area_code: str
    original_price_cents: int
    wplus_member_price_cents: int | None
    channel_fee_cents: int
    label: str
    zone_type: SeatZoneType


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _data(body: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = body.get("data")
    return nested if isinstance(nested, Mapping) else body


def _identifier(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _zone_from_text(value: Any) -> SeatZoneType:
    text = _text(value)
    if "W+" in text:
        return SeatZoneType.WPLUS
    if "普通" in text:
        return SeatZoneType.REGULAR
    if "特惠" in text:
        return SeatZoneType.DISCOUNT
    if "优选" in text:
        return SeatZoneType.PREMIUM
    return SeatZoneType.UNKNOWN


def _seat_facts(payload: Mapping[str, Any]) -> list[SeatFact]:
    root = _data(payload)
    realtime = root.get("realtimeSeats") or root.get("realtime_seats") or root
    realtime = realtime if isinstance(realtime, Mapping) else {}
    areas = realtime.get("area") or realtime.get("areas") or []
    if not isinstance(areas, Sequence) or isinstance(areas, (str, bytes)):
        return []
    facts: list[SeatFact] = []
    for area in areas:
        if not isinstance(area, Mapping):
            continue
        area_code = _identifier(area.get("areaCode") or area.get("areaId") or area.get("code"))
        area_price = area.get("areaPrice")
        area_price = area_price if isinstance(area_price, Mapping) else {}
        zone = _zone_from_text(
            area.get("areaName") or area.get("name") or area.get("label") or area_price.get("areaName")
        )
        raw_default_price = (
            area.get("areaSalesPriceCents") or area.get("areaPrice")
            if not isinstance(area.get("areaPrice"), Mapping)
            else area_price.get("salesPrice")
        )
        default_price = _positive_int(raw_default_price)
        wplus_activity = area.get("wPlusActivity") or area_price.get("wPlusActivity")
        wplus_activity = wplus_activity if isinstance(wplus_activity, Mapping) else {}
        wplus_member_price = _positive_int(wplus_activity.get("price"))
        channel_fee = area_price.get("channelFee", area.get("areaChannelFeeCents", 0))
        channel_fee = channel_fee if isinstance(channel_fee, int) and not isinstance(channel_fee, bool) and channel_fee >= 0 else 0
        seats = area.get("seat") or area.get("seats") or []
        if not isinstance(seats, Sequence) or isinstance(seats, (str, bytes)):
            continue
        for seat in seats:
            if not isinstance(seat, Mapping) or seat.get("status") not in (1, "1", "可选"):
                continue
            seat_id = _identifier(seat.get("seatId") or seat.get("id"))
            price = _positive_int(seat.get("areaSalesPriceCents") or seat.get("areaPrice") or seat.get("price")) or default_price
            if not seat_id or not area_code or price is None:
                continue
            row = _text(seat.get("row") or seat.get("rowNum"))
            column = _text(seat.get("column") or seat.get("colNum"))
            label = _text(seat.get("name") or seat.get("label")) or (f"{row}排{column}座" if row and column else "")
            facts.append(SeatFact(seat_id, area_code, price, wplus_member_price, channel_fee, label, zone))
    return facts


def _requested_zone(recognition: Recognition) -> SeatZoneType:
    # Hand-drawn marks are not a quote instruction. Without a confirmed
    # platform selection, the buyer flow always probes W+ and asks the count.
    # Official selections are verified exactly later and never fall back to
    # an unrelated area probe when a selected seat cannot be found.
    visible_zones = set(recognition.seat_zone_types)
    if recognition.official_selection.is_selected and visible_zones == {SeatZoneType.REGULAR}:
        return SeatZoneType.REGULAR
    return SeatZoneType.UNKNOWN


def _round_quote_cents_to_tenth(value: int) -> int:
    """Round positive cents half-up to a 0.1-yuan quote increment."""
    if not isinstance(value, int) or value < 0:
        raise ValueError("quote cents must be a non-negative integer")
    return ((value + 5) // 10) * 10


def _unit_quote_cents(zone: SeatZoneType, original_price_cents: int, member_price_cents: int | None, *, wplus_adjustment_cents: int, wplus_member_price_threshold_cents: int = 6000, regular_adjustment_cents: int) -> int:
    """Apply the merchant policy using only verified real-time prices."""
    if zone is SeatZoneType.WPLUS:
        # W+专享 is only a seat-area label. A negative merchant adjustment is
        # safe only when Wanda explicitly returns a realtime wPlusActivity
        # offer; otherwise salesPrice may equal the seller's actual cost.
        if member_price_cents is None:
            raise HTTPException(status_code=422, detail="实时座位图未返回可核验的 W+会员专属优惠价")
        adjusted_original_price = original_price_cents + wplus_adjustment_cents
        price = member_price_cents if member_price_cents > wplus_member_price_threshold_cents else max(adjusted_original_price, member_price_cents)
    else:
        if member_price_cents is None:
            raise HTTPException(status_code=422, detail="实时座位图未返回可核验的 W+会员价")
        price = member_price_cents + regular_adjustment_cents
    if price <= 0:
        raise HTTPException(status_code=422, detail="报价规则计算结果必须大于零")
    return price


def _bounded_quote_for_seat(
    seat: SeatFact,
    zone: SeatZoneType,
    *,
    wplus_adjustment_cents: int,
    wplus_member_price_threshold_cents: int,
    regular_adjustment_cents: int,
) -> int:
    raw_quote = _unit_quote_cents(
        zone,
        seat.original_price_cents,
        seat.wplus_member_price_cents,
        wplus_adjustment_cents=wplus_adjustment_cents,
        wplus_member_price_threshold_cents=wplus_member_price_threshold_cents,
        regular_adjustment_cents=regular_adjustment_cents,
    )
    rounded_member_floor = (
        ((seat.wplus_member_price_cents + 9) // 10) * 10
        if seat.wplus_member_price_cents is not None else 0
    )
    quote = max(_round_quote_cents_to_tenth(raw_quote), rounded_member_floor)
    rounded_original_ceiling = (seat.original_price_cents // 10) * 10
    if rounded_member_floor > rounded_original_ceiling:
        raise HTTPException(status_code=422, detail="实时原价与W+会员价在十分位报价规则下冲突，不能自动报价")
    # The buyer must never be quoted above Wanda's current original price. If
    # the configured adjustment has no room but the original still covers the
    # authoritative member-price floor, quote the rounded original directly.
    return min(quote, rounded_original_ceiling)


def _wplus_probe_candidates(all_seats: list[SeatFact]) -> list[SeatFact]:
    """Return only verified real-time W+ candidates for an area probe.

    Screenshot prices must not select, cap, or otherwise influence a quote.
    The deterministic sample is chosen solely from the current Wanda seat map.
    """
    return [
        seat
        for seat in all_seats
        if seat.zone_type is SeatZoneType.WPLUS
    ]


def _select_seats(recognition: Recognition, all_seats: list[SeatFact], quantity: int) -> tuple[list[SeatFact], bool, SeatZoneType]:
    labels = set(recognition.official_selection.selected_seat_numbers)
    if recognition.official_selection.is_selected and labels:
        selected = [seat for seat in all_seats if seat.label in labels]
        if len(selected) == len(labels) == quantity:
            zones = {seat.zone_type for seat in selected}
            zone = next(iter(zones)) if len(zones) == 1 else SeatZoneType.UNKNOWN
            return selected, True, zone
        # Official selected-seat evidence must be verified seat by seat against
        # the current Wanda map. Never replace a failed exact verification with
        # an unrelated W+ area sample.
        raise HTTPException(status_code=422, detail="官方已选座无法在实时座位图逐座核验")

    # When the platform selection is absent, probe an actually available W+
    # live map, probe an actually available W+ seat. The result is explicitly
    # an area price, never a claim that this is the buyer's exact seat.
    zone = SeatZoneType.WPLUS
    # W+ eligibility comes from the verified wPlusActivity price, not from an
    # operator-defined area name such as “特惠区” or “普通区”.
    candidates = _wplus_probe_candidates(all_seats)
    if not candidates:
        raise HTTPException(status_code=422, detail="当前场次没有可用的 W+座位")
    if len(candidates) < quantity:
        raise HTTPException(status_code=422, detail="未找到足够的同类可用座位用于核价")
    # Choose a deterministic realtime sample. Random probes could quote two
    # different W+ areas for the same unchanged buyer conversation. This does
    # not reserve seats; it only makes the area-probe price reproducible until
    # the live seat map itself changes.
    ordered = sorted(candidates, key=lambda seat: (seat.area_code, seat.original_price_cents, seat.wplus_member_price_cents or 0, seat.seat_id))
    return ordered[:quantity], False, zone


def _partition(seats: Sequence[SeatFact]) -> str:
    groups: dict[str, list[str]] = {}
    for seat in seats:
        groups.setdefault(seat.area_code, []).append(seat.seat_id)
    return "|".join(f"{area}-{','.join(ids)}" for area, ids in groups.items())


def _locked_offer_unit_cents(response: Mapping[str, Any], *, quantity: int, allow_friday: bool) -> int:
    # `allotSeat.totalPayPrice` is the payable price for the probed seat type,
    # not an order total that can safely be divided by the buyer ticket count.
    # Quotes therefore probe exactly one representative seat per type.
    if quantity != 1:
        raise HTTPException(status_code=422, detail="会员优惠单价只能通过单座试价读取")
    data = _data(response)
    activities = data.get("activities") or response.get("activities") or []
    if not isinstance(activities, Sequence) or isinstance(activities, (str, bytes)):
        raise HTTPException(status_code=502, detail="万达优惠接口未返回活动列表")
    candidates: list[int] = []
    for item in activities:
        if not isinstance(item, Mapping) or item.get("able") is not True:
            continue
        name = _text(item.get("name"))
        standard = WPLUS_STANDARD_NAME in name
        friday = WPLUS_FRIDAY_NAME in name
        if not standard and not (allow_friday and friday):
            continue
        allot = item.get("allot_seat") or item.get("allotSeat")
        if not isinstance(allot, Mapping):
            continue
        total = _positive_int(allot.get("totalPayPrice"))
        if total is not None:
            candidates.append(total)
    if len(set(candidates)) != 1:
        raise HTTPException(status_code=422, detail="未找到唯一可用的 W+会员专享优惠价")
    return candidates[0]


def _all_seats_released(payload: Mapping[str, Any], expected: Sequence[SeatFact]) -> bool:
    available_ids = {seat.seat_id for seat in _seat_facts(payload)}
    return bool(expected) and all(seat.seat_id in available_ids for seat in expected)
