from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .models import ExactSeatFact, SeatFactsResult, WplusAreaFact
from .wanda_source import WandaRealtimeSeatSource


class SeatFactsV2Service:
    """Read Wanda realtime seat facts after store/show identity is complete."""

    def __init__(self, source: WandaRealtimeSeatSource) -> None:
        self._source = source

    async def resolve(
        self,
        request: Mapping[str, Any],
        *,
        manual_mark_detector: Any | None = None,
    ) -> SeatFactsResult:
        route = _text(request.get("route"))
        store_id = _text(request.get("wanda_store_id"))
        show_id = _text(request.get("wanda_show_id"))
        selected_seats = [
            text for value in request.get("selected_seats") or []
            if (text := _text(value)) is not None
        ]
        mark = request.get("has_manual_mark")
        if mark not in (True, False, None):
            mark = None
        if not store_id or not show_id:
            return _result(
                "INPUT_INCOMPLETE", "MANUAL_MARK_REQUIRED" if mark is None else _request_type(mark, selected_seats),
                store_id, show_id, mark, "WANDA_STORE_AND_SHOW_REQUIRED",
            )
        if route != "WANDA_SELF":
            return _result(
                "INPUT_INCOMPLETE", "MANUAL_MARK_REQUIRED" if mark is None else _request_type(mark, selected_seats),
                store_id, show_id, mark, "WANDA_SELF_ROUTE_REQUIRED",
            )

        image_url = _text(request.get("image_url"))
        if mark is None and manual_mark_detector is not None and image_url:
            try:
                mark = await manual_mark_detector.detect(image_url)
            except Exception:
                mark = None
            if mark not in (True, False):
                mark = None
        if mark is None:
            return _result(
                "MANUAL_MARK_REQUIRED", "MANUAL_MARK_REQUIRED",
                store_id, show_id, mark, "MANUAL_MARK_UNAVAILABLE",
            )

        request_type = _request_type(mark, selected_seats)
        try:
            response = await self._source.get_realtime_seats(show_id)
            if not _provider_success(response):
                return _result("PROVIDER_UNAVAILABLE", request_type, store_id, show_id, mark, "WANDA_REALTIME_PROVIDER_UNAVAILABLE")
            areas = _flatten_areas(response)
        except Exception:
            return _result("PROVIDER_UNAVAILABLE", request_type, store_id, show_id, mark, "WANDA_REALTIME_PROVIDER_UNAVAILABLE")

        if request_type == "EXACT_SEATS":
            return _resolve_exact(store_id, show_id, mark, selected_seats, areas)
        return _resolve_wplus(store_id, show_id, mark, areas)


def select_same_type_available_reference(
    selected: list[Mapping[str, Any]],
    seats: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Select one nearby available seat in the exact same pricing group.

    This deterministic primitive is shared with the legacy service only for
    candidate selection. It never changes requested seats or computes a price.
    """
    if not selected:
        return None
    selected_keys = {_same_type_key(item) for item in selected}
    if len(selected_keys) != 1 or None in selected_keys:
        return None
    wanted_ids = {_text(item.get("wanda_seat_id") or item.get("seat_id")) for item in selected}
    wanted_key = next(iter(selected_keys))
    candidates = [
        dict(item) for item in seats
        if _seat_is_available(item)
        and _text(item.get("wanda_seat_id") or item.get("seat_id")) not in wanted_ids
        and _same_type_key(item) == wanted_key
    ]
    if not candidates:
        return None
    reference = selected[0]
    reference_row = _positive_int(reference.get("row"))
    reference_col = _positive_int(reference.get("col"))

    def distance(item: Mapping[str, Any]) -> tuple[int, str]:
        row = _positive_int(item.get("row"))
        col = _positive_int(item.get("col"))
        value = (
            abs(row - reference_row) + abs(col - reference_col)
            if reference_row is not None and reference_col is not None and row is not None and col is not None
            else 10**9
        )
        return value, _text(item.get("wanda_seat_id") or item.get("seat_id")) or ""

    candidates.sort(key=distance)
    return candidates[0]


def _same_type_key(seat: Mapping[str, Any]) -> tuple[str, str, str, str, bool] | None:
    area_code = _text(seat.get("area_code") or seat.get("area_id"))
    zone_type = _text(seat.get("zone_type") or seat.get("zone"))
    seat_type = _text(seat.get("seat_type") or seat.get("seat_kind")) or ""
    member_group = _text(seat.get("member_price_group"))
    if not member_group:
        member_group = ":".join(str(value or "") for value in (
            seat.get("area_member_activity_code_hint"),
            seat.get("area_original_price_fen"),
            seat.get("area_member_price_fen"),
        )).strip(":") or None
    if not area_code or not zone_type or not member_group:
        return None
    return area_code, zone_type, seat_type, member_group, bool(seat.get("is_wplus_exclusive"))


def _seat_is_available(seat: Mapping[str, Any]) -> bool:
    return seat.get("available") is True or str(seat.get("status") or "").strip().upper() in {
        "AVAILABLE", "可选", "1",
    }


def _resolve_exact(
    store_id: str,
    show_id: str,
    mark: bool,
    requested: list[str],
    areas: list[dict[str, Any]],
) -> SeatFactsResult:
    all_seats = [seat for area in areas for seat in area["seats"]]
    facts: list[ExactSeatFact] = []
    for label in requested:
        matches = [seat for seat in all_seats if _label_key(seat.get("label")) == _label_key(label)]
        if len(matches) != 1 or not matches[0].get("wanda_seat_id"):
            return _result(
                "SEAT_NOT_FOUND", "EXACT_SEATS", store_id, show_id, mark,
                "TARGET_SEAT_NOT_FOUND", exact_seats=facts,
            )
        facts.append(_seat_fact(matches[0]))
    if any(fact.status != "AVAILABLE" for fact in facts):
        reference = select_same_type_available_reference(
            [fact.model_dump(mode="json") for fact in facts], all_seats,
        )
        return _result(
            "SEAT_UNAVAILABLE", "EXACT_SEATS", store_id, show_id, mark,
            "TARGET_SEAT_NOT_AVAILABLE_SAME_TYPE_REFERENCE" if reference else "TARGET_SEAT_NOT_AVAILABLE",
            exact_seats=facts,
            same_type_reference=_seat_fact(reference) if reference else None,
        )
    return _result(
        "EXACT_SEATS_RESOLVED", "EXACT_SEATS", store_id, show_id, mark,
        "ALL_TARGET_SEATS_AVAILABLE", exact_seats=facts,
    )


def _resolve_wplus(
    store_id: str,
    show_id: str,
    mark: bool,
    areas: list[dict[str, Any]],
) -> SeatFactsResult:
    facts = [_wplus_fact(area) for area in areas if area["is_wplus_area"]]
    return _result(
        "WPLUS_AREA_RESOLVED", "WPLUS_AREA", store_id, show_id, mark,
        "WPLUS_AREAS_READ" if facts else "NO_WPLUS_AREA_REPORTED",
        wplus_areas=facts,
    )


def _flatten_areas(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return []
    realtime = data.get("realtimeSeats") or data.get("realtime_seats") or data
    if not isinstance(realtime, Mapping):
        return []
    raw_areas = realtime.get("area") or realtime.get("areas") or []
    if not isinstance(raw_areas, list):
        return []
    areas: list[dict[str, Any]] = []
    for raw_area in raw_areas:
        if not isinstance(raw_area, Mapping):
            continue
        raw_seats = raw_area.get("seat") or raw_area.get("seats") or []
        if not isinstance(raw_seats, list):
            raw_seats = []
        area_name = _first(raw_area, "areaName", "name", "zoneName")
        area_price_data = raw_area.get("areaPrice")
        if isinstance(area_price_data, Mapping):
            area_code = (
                _first(raw_area, "areaCode")
                or _first(area_price_data, "areaCode")
                or _first(raw_area, "areaId", "id")
            )
        else:
            area_code = _first(raw_area, "areaCode", "areaId", "id")
        area_original_price_fen = _extract_area_original_price(area_price_data)
        area_member_price_fen = _extract_area_member_price(raw_area, area_price_data)
        has_valid_area_member_price = (
            area_original_price_fen is not None
            and area_member_price_fen is not None
            and area_member_price_fen <= area_original_price_fen
        )
        area_member_activity_code_hint = _extract_activity_code(raw_area)
        has_wplus_pricing = _has_wplus_pricing(raw_area, area_member_price_fen)
        area_zone_type = _first(raw_area, "zoneType", "zone") or area_name or (
            "WPLUS" if any(
                isinstance(raw_seat, Mapping) and _is_wplus_seat(raw_seat)
                for raw_seat in raw_seats
            ) or has_wplus_pricing else None
        )
        seats: list[dict[str, Any]] = []
        for raw_seat in raw_seats:
            if not isinstance(raw_seat, Mapping):
                continue
            label = _first(raw_seat, "name", "seatName", "label")
            if not label:
                continue
            row = _positive_int(_first(raw_seat, "row", "rowNo", "coordy"))
            col = _positive_int(_first(raw_seat, "column", "col", "colNo", "coordx"))
            parsed_row, parsed_col = _parse_label(label)
            seats.append({
                "label": label,
                "wanda_seat_id": _first(raw_seat, "seatId", "seat_id", "id"),
                "row": row or parsed_row,
                "col": col or parsed_col,
                "area_code": area_code,
                "zone_type": _first(raw_seat, "zoneType", "zone", "areaName", "seatTypeStr") or area_zone_type,
                "seat_type": _first(raw_seat, "seatTypeCode", "seatType", "seatKind", "seat_kind") or "",
                "member_price_group": _member_price_group(
                    raw_seat, area_member_activity_code_hint,
                    area_original_price_fen, area_member_price_fen,
                ),
                "status": _seat_status(raw_seat),
                "is_wplus_exclusive": _is_wplus_seat(raw_seat),
                "area_original_price_fen": area_original_price_fen,
                "area_member_price_fen": area_member_price_fen,
                "has_valid_area_member_price": has_valid_area_member_price,
                "area_member_activity_code_hint": area_member_activity_code_hint,
            })
        areas.append({
            "area_code": area_code,
            "area_name": area_name,
            "zone_type": area_zone_type,
            "is_wplus_area": any(seat["is_wplus_exclusive"] for seat in seats) or has_wplus_pricing,
            "area_original_price_fen": area_original_price_fen,
            "area_member_price_fen": area_member_price_fen,
            "has_valid_area_member_price": has_valid_area_member_price,
            "area_member_activity_code_hint": area_member_activity_code_hint,
            "seats": seats,
        })
    return areas


def _is_wplus_seat(seat: Mapping[str, Any]) -> bool:
    return seat.get("payMemberSeatStatus") in (1, "1")


def _seat_fact(value: Mapping[str, Any]) -> ExactSeatFact:
    return ExactSeatFact(
        label=str(value["label"]),
        seat_label=str(value["label"]),
        wanda_seat_id=value.get("wanda_seat_id"),
        seat_id=value.get("wanda_seat_id"),
        row=value.get("row"),
        col=value.get("col"),
        area_code=value.get("area_code"),
        zone_type=value.get("zone_type"),
        seat_type=value.get("seat_type"),
        member_price_group=value.get("member_price_group"),
        status=value["status"],
        is_wplus_exclusive=value.get("is_wplus_exclusive", False),
        area_original_price_fen=value.get("area_original_price_fen"),
        area_member_price_fen=value.get("area_member_price_fen"),
        has_valid_area_member_price=value.get("has_valid_area_member_price", False),
        area_member_activity_code_hint=value.get("area_member_activity_code_hint"),
    )


def _wplus_fact(area: Mapping[str, Any]) -> WplusAreaFact:
    available_seat_ids = [
        str(seat["wanda_seat_id"])
        for seat in area["seats"]
        if seat["status"] == "AVAILABLE" and seat.get("wanda_seat_id")
    ]
    return WplusAreaFact(
        area_code=area.get("area_code"),
        zone_type=area.get("zone_type"),
        available_seat_ids=available_seat_ids,
        available_seat_count=len(available_seat_ids),
        wplus_available=bool(available_seat_ids),
        area_original_price_fen=area.get("area_original_price_fen"),
        area_member_price_fen=area.get("area_member_price_fen"),
        has_valid_area_member_price=area.get("has_valid_area_member_price", False),
        area_member_activity_code_hint=area.get("area_member_activity_code_hint"),
    )


def _request_type(mark: bool | None, selected_seats: list[str]) -> str:
    if mark is None:
        return "MANUAL_MARK_REQUIRED"
    if mark is True:
        return "WPLUS_AREA"
    return "EXACT_SEATS" if selected_seats else "WPLUS_AREA"


def _provider_success(response: Mapping[str, Any]) -> bool:
    return response.get("code") in (None, 0, "0") and isinstance(response.get("data"), Mapping)


def _seat_status(seat: Mapping[str, Any]) -> str:
    if seat.get("available") is True:
        return "AVAILABLE"
    raw = seat.get("status")
    if raw in (1, "1", "AVAILABLE", "available"):
        return "AVAILABLE"
    if raw in (0, "0", 2, "2", "OCCUPIED", "occupied", "SOLD", "sold", "UNAVAILABLE", "unavailable"):
        return "OCCUPIED"
    return "UNKNOWN"


def _extract_area_original_price(area_price: Any) -> int | None:
    if not isinstance(area_price, Mapping):
        return None
    return _first_positive_price(area_price, "salesPrice", "salePrice", "price", "originalPrice")


def _extract_area_member_price(area: Mapping[str, Any], area_price: Any) -> int | None:
    activity = area.get("wPlusActivity") or area.get("wplusActivity")
    if isinstance(activity, Mapping):
        price = _first_positive_price(activity, "price", "activityPrice", "wPlusActivityPrice", "memberPrice")
        if price is not None:
            return price
    price = _first_positive_price(area, "memberPrice", "payMemberPrice")
    if price is not None:
        return price
    if isinstance(area_price, Mapping):
        return _first_positive_price(
            area_price, "memberPrice", "payMemberPrice", "activityPrice", "wPlusActivityPrice",
        )
    return None


def _member_price_group(
    seat: Mapping[str, Any], activity_code: str | None,
    original_price: int | None, member_price: int | None,
) -> str | None:
    explicit = _first(seat, "memberPriceGroup", "member_price_group", "priceGroup", "priceGroupId")
    return _text(explicit) or ":".join(str(value or "") for value in (
        activity_code, original_price, member_price,
    )).strip(":") or None


def _extract_activity_code(area: Mapping[str, Any]) -> str | None:
    activity = area.get("wPlusActivity") or area.get("wplusActivity")
    return _first(activity, "activityCode") if isinstance(activity, Mapping) else None


def _has_wplus_pricing(area: Mapping[str, Any], member_price: int | None) -> bool:
    activity = area.get("wPlusActivity") or area.get("wplusActivity")
    return isinstance(activity, Mapping) or member_price is not None


def _first_positive_price(item: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = item.get(key)
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _label_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).replace("排", "排").replace("座", "座")


def _parse_label(value: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d+)\s*排\s*(\d+)\s*座", unicodedata.normalize("NFKC", value))
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _first(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _text(item.get(key))
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _result(
    status: str,
    request_type: str,
    store_id: str | None,
    show_id: str | None,
    mark: bool | None,
    reason: str,
    *,
    exact_seats: list[ExactSeatFact] | None = None,
    wplus_areas: list[WplusAreaFact] | None = None,
    same_type_reference: ExactSeatFact | None = None,
) -> SeatFactsResult:
    return SeatFactsResult(
        status=status,
        seat_request_type=request_type,
        wanda_store_id=store_id,
        wanda_show_id=show_id,
        has_manual_mark=mark,
        exact_seats=exact_seats or [],
        wplus_areas=wplus_areas or [],
        same_type_reference=same_type_reference,
        resolution_reason=reason,
    )
