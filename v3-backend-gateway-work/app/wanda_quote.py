from __future__ import annotations

import asyncio
import os
import re
import time
from copy import deepcopy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
from fastapi import HTTPException, status

from .local_catalog import LocalWandaCatalog
from .wanda_direct_gateway import DirectGatewayError, build_wanda_direct_gateway_from_env
from .schemas import AvailableWplusSeatsResponse, QuoteRealtimeRequest, QuoteRealtimeResponse, QuoteShowtimeResolveResponse, Recognition, SeatQuote, SeatZoneType


WPLUS_STANDARD_NAME: Final = "W+会员专享优惠"
WPLUS_FRIDAY_NAME: Final = "W+周五会员日专享"
REGULAR_SEAT_MARKUP_CENTS: Final = 100
RELEASE_RECHECK_DELAYS_SECONDS: Final = (0.0, 2.0, 5.0)
_SHOWTIME_LOCKS: dict[str, asyncio.Lock] = {}


def _showtime_lock(showtime_id: str) -> asyncio.Lock:
    return _SHOWTIME_LOCKS.setdefault(showtime_id, asyncio.Lock())


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _showtime_start(value: str | None) -> str | None:
    if not value:
        return None
    matched = re.match(r"^(\d{2}:\d{2})", value.strip())
    return matched.group(1) if matched else value


def _gateway_showtime_hint(recognition: Recognition) -> str | None:
    start = _showtime_start(recognition.showtime)
    if recognition.date and start:
        return f"{recognition.date.isoformat()} {start}"
    return start


def _gateway_match_text(hints: Mapping[str, Any]) -> str:
    labels = (("城市", "city"), ("影院", "cinema"), ("电影", "movie"), ("场次", "showtime"), ("影厅", "hall"))
    return "\n".join(f"{label}：{value}" for label, key in labels if (value := _text(hints.get(key))))


def _gateway_auth_headers() -> dict[str, str]:
    """Authenticate V3-to-ticket-gateway calls without exposing an operator session."""
    key = os.getenv("WANDA_QUOTE_GATEWAY_KEY", "").strip()
    return {"X-Plugin-Bridge-Key": key} if key else {}


@dataclass(frozen=True)
class SeatFact:
    seat_id: str
    area_code: str
    original_price_cents: int
    wplus_member_price_cents: int | None
    channel_fee_cents: int
    label: str
    zone_type: SeatZoneType


class TicketGateway(Protocol):
    async def for_quote(self) -> "TicketGateway": ...

    def account_mobile(self) -> str: ...

    async def match(self, recognition: Recognition) -> Mapping[str, Any]: ...

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]: ...

    async def lock(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def available_offers(self, *, cinema_id: str, showtime_id: str, partition: str, order_id: str) -> Mapping[str, Any]: ...

    async def cancel(self, order_id: str) -> bool: ...


class LocalTicketGateway:
    """Adapter for the locally deployed ticket gateway; it owns Wanda signing/token use."""

    def __init__(self, base_url: str | None = None, account_phone: str | None = None) -> None:
        self._base_url = (base_url or os.getenv("WANDA_QUOTE_GATEWAY_URL", "http://127.0.0.1:8000")).rstrip("/")
        self._account_phone = (account_phone or os.getenv("WANDA_ACCOUNT_PHONE", "")).strip()

    def _require_account_phone(self) -> str:
        if not self._account_phone:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="万达核价账号尚未配置")
        return self._account_phone

    def account_mobile(self) -> str:
        return self._require_account_phone()

    async def for_quote(self) -> TicketGateway:
        if self._account_phone:
            return self
        phone = await self._select_online_wplus_account_phone()
        return LocalTicketGateway(self._base_url, phone)

    async def _select_online_wplus_account_phone(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=4)) as client:
                response = await client.get(
                    f"{self._base_url}/api/auth/internal/wplus-accounts",
                    headers=_gateway_auth_headers(),
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=502, detail=f"万达账号池网关返回 HTTP {error.response.status_code}") from error
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="万达账号池网关连接失败") from error
        accounts = payload.get("accounts") if isinstance(payload, Mapping) else None
        if not isinstance(accounts, list):
            raise HTTPException(status_code=502, detail="万达账号池返回格式无效")
        candidates: list[tuple[int, str]] = []
        for account in accounts:
            if not isinstance(account, Mapping) or account.get("available") is not True:
                continue
            phone = _text(account.get("phone"))
            remaining = account.get("remaining")
            remaining_count = remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else 0
            if phone:
                candidates.append((remaining_count, phone))
        if candidates:
            return sorted(candidates, key=lambda item: (-item[0], item[1]))[0][1]
        raise HTTPException(status_code=422, detail="线上账号池没有可用的 W+ 会员账号")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            headers = _gateway_auth_headers()
            extra_headers = kwargs.pop("headers", None)
            if isinstance(extra_headers, Mapping):
                headers.update({str(key): str(value) for key, value in extra_headers.items()})
            async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=4)) as client:
                response = await client.request(method, f"{self._base_url}{path}", headers=headers, **kwargs)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as error:
            raise HTTPException(status_code=502, detail=f"万达核价网关返回 HTTP {error.response.status_code}") from error
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="万达核价网关连接失败") from error
        if not isinstance(body, Mapping):
            raise HTTPException(status_code=502, detail="万达核价网关返回格式无效")
        return body

    async def match(self, recognition: Recognition) -> Mapping[str, Any]:
        hints = {
            "city": recognition.city,
            "cinema": recognition.cinema,
            "movie": recognition.movie,
            "showtime": _gateway_showtime_hint(recognition),
            "hall": recognition.hall,
            "seats": recognition.official_selection.selected_seat_numbers,
        }
        text = _gateway_match_text(hints)
        return await self._request(
            "POST",
            "/api/order/match",
            json={"text": text, "mode": "screenshot", "phone": self._require_account_phone(), "auto_select_seats": False, "hints": hints},
        )

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]:
        return await self._request("GET", "/api/showtime/seats", params={"showtimeId": showtime_id, "phone": self._require_account_phone()})

    async def lock(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._request("POST", "/api/order/create-ticket-flow", json=payload)

    async def available_offers(self, *, cinema_id: str, showtime_id: str, partition: str, order_id: str) -> Mapping[str, Any]:
        return await self._request(
            "GET",
            "/api/order/available-offers",
            params={
                "cinemaId": cinema_id,
                "showtimeId": showtime_id,
                "partition": partition,
                "orderId": order_id,
                "phone": self._require_account_phone(),
                "includeYqk": "false",
            },
        )

    async def cancel(self, order_id: str) -> bool:
        try:
            result = await self._request(
                "POST",
                "/api/order/cancel",
                json={"order_id": order_id, "phone": self._require_account_phone()},
            )
        except HTTPException:
            return False
        return result.get("code") in (None, 0, "0") or result.get("success") is True


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


def _match_has_cinema(match: Mapping[str, Any]) -> bool:
    data = _data(match)
    cinema = data.get("cinema")
    cinema = cinema if isinstance(cinema, Mapping) else {}
    return bool(_identifier(cinema.get("cinemaId") or cinema.get("id") or data.get("cinema_id")))


def _showtime_and_cinema(match: Mapping[str, Any]) -> tuple[str, str, str | None]:
    data = _data(match)
    showtime = data.get("showtime")
    showtime = showtime if isinstance(showtime, Mapping) else {}
    showtime_id = _identifier(showtime.get("showtimeId") or showtime.get("id") or data.get("showtime_id"))
    cinema = data.get("cinema")
    cinema = cinema if isinstance(cinema, Mapping) else {}
    cinema_id = _identifier(showtime.get("cinemaId") or cinema.get("cinemaId") or cinema.get("id") or data.get("cinema_id"))
    cinema_name = _text(cinema.get("cinemaName") or cinema.get("name") or showtime.get("cinemaName") or data.get("cinemaName")) or None
    if not showtime_id or not cinema_id:
        raise HTTPException(status_code=422, detail="截图信息无法唯一匹配万达场次")
    return showtime_id, cinema_id, cinema_name


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


def _match_diagnostics(match: Mapping[str, Any]) -> dict[str, Any]:
    """Return only operator-safe match facts; never echo gateway payloads."""
    root = _data(match)
    showtime = root.get("showtime") if isinstance(root.get("showtime"), Mapping) else {}
    cinema = root.get("cinema") if isinstance(root.get("cinema"), Mapping) else {}
    candidates = root.get("matches") or root.get("showtimes") or root.get("items")
    count = len(candidates) if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)) else (1 if _identifier(showtime.get("showtimeId") or showtime.get("id") or root.get("showtime_id")) else 0)
    return {
        "result_count": count,
        "cinema": _text(cinema.get("cinemaName") or cinema.get("name") or root.get("cinemaName")),
        "showtime": _text(showtime.get("showTime") or showtime.get("startTime") or showtime.get("showtime") or root.get("showtime")),
    }


def _requested_match_diagnostics(recognition: Recognition) -> dict[str, str]:
    """Keep bounded, non-sensitive identity facts needed to debug zero matches."""
    showtime = _text(recognition.showtime).split("-", 1)[0][:5]
    date_text = recognition.date.isoformat() if recognition.date is not None else ""
    return {
        "city": _text(recognition.city)[:80],
        "cinema": _text(recognition.cinema)[:160],
        "movie": _text(recognition.movie)[:160],
        "date": date_text,
        "showtime": showtime,
        "hall": _text(recognition.hall)[:80],
    }


def _realtime_area_diagnostics(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    root = _data(payload)
    realtime = root.get("realtimeSeats") or root.get("realtime_seats") or root
    areas = realtime.get("area") or realtime.get("areas") or [] if isinstance(realtime, Mapping) else []
    if not isinstance(areas, Sequence) or isinstance(areas, (str, bytes)):
        return []
    summaries: list[dict[str, Any]] = []
    for area in areas[:20]:
        if not isinstance(area, Mapping):
            continue
        area_price = area.get("areaPrice") if isinstance(area.get("areaPrice"), Mapping) else {}
        activity = area.get("wPlusActivity") or area_price.get("wPlusActivity")
        activity = activity if isinstance(activity, Mapping) else {}
        seats = area.get("seat") or area.get("seats") or []
        available = sum(1 for seat in seats if isinstance(seat, Mapping) and seat.get("status") in (1, "1", "可选")) if isinstance(seats, Sequence) and not isinstance(seats, (str, bytes)) else 0
        summaries.append({
            "area_code": _identifier(area.get("areaCode") or area.get("areaId") or area.get("code")),
            "label": _text(area.get("areaName") or area.get("name") or area.get("label") or area_price.get("areaName"))[:80],
            "sales_price_cents": _positive_int(area.get("areaSalesPriceCents") or (area.get("areaPrice") if not isinstance(area.get("areaPrice"), Mapping) else area_price.get("salesPrice"))),
            "wplus_member_price_cents": _positive_int(activity.get("price")),
            "available_seat_count": available,
        })
    return summaries


def _quote_failure_code(error: HTTPException, step: str) -> str:
    detail = _text(error.detail)
    if "官方影院库" in detail:
        return "cinema_catalog_not_unique"
    if "仅支持万达" in detail:
        return "non_wanda_cinema"
    if "张数" in detail and "不一致" in detail:
        return "ticket_count_conflict"
    if "官方已选座" in detail and "实时" in detail:
        return "official_selection_unverifiable"
    if "没有可用的 W+座位" in detail:
        return "wplus_seats_unavailable"
    if "唯一匹配" in detail or "场次" in detail or "匹配" in detail:
        return "showtime_not_unique"
    if "足够" in detail and "座位" in detail:
        return "insufficient_available_seats"
    if "原价与W+会员价" in detail and "冲突" in detail:
        return "quote_price_conflict"
    if "W+区域" in detail:
        return "wplus_area_unavailable"
    if "W+会员价" in detail or "W+会员专属优惠价" in detail or "W+会员专享优惠价" in detail or "W+ 优惠" in detail:
        return "wplus_price_unavailable"
    if "临时锁座未确认释放" in detail:
        return "temporary_lock_release_unverified"
    if "临时锁座" in detail:
        return "temporary_lock_failed"
    if "账号池" in detail or "会员账号" in detail:
        return "wplus_account_unavailable"
    if error.status_code >= 500:
        return "wanda_gateway_unavailable"
    return f"quote_{step}_failed"


def _quote_failure_message(code: str) -> str:
    return {
        "cinema_catalog_not_unique": "影院无法在官方影院库唯一匹配",
        "non_wanda_cinema": "仅支持万达影院实时核价",
        "ticket_count_conflict": "文字张数与官方已选座张数不一致，请人工确认",
        "official_selection_unverifiable": "官方已选座无法在实时座位图逐座核验，请人工确认",
        "showtime_not_unique": "未能唯一匹配万达场次",
        "insufficient_available_seats": "实时座位图中没有足够的同类可用座位",
        "wplus_seats_unavailable": "当前场次没有可用的 W+座位",
        "wplus_area_unavailable": "实时座位图未找到可核验的 W+区域，请人工复核",
        "wplus_price_unavailable": "实时座位图未返回可用的 W+会员专属优惠价",
        "quote_price_conflict": "实时会员优惠不低于原价，按当前规则无法形成安全报价",
        "wplus_account_unavailable": "W+ 核价账号暂不可用",
        "temporary_lock_failed": "万达临时锁座核价未完成",
        "temporary_lock_release_unverified": "临时核价座位未确认释放，已停止自动报价",
        "wanda_gateway_unavailable": "万达实时核价服务暂不可用",
    }.get(code, "实时核价未通过")


def _diagnostic_failure(error: HTTPException, *, step: str, recognition: Recognition | None = None, match: Mapping[str, Any] | None = None, realtime: Mapping[str, Any] | None = None) -> HTTPException:
    # Do not return raw gateway messages, account data, request payloads, or
    # seat IDs. The bounded summary is safe for event logs and UI diagnostics.
    code = _quote_failure_code(error, step)
    diagnostics: dict[str, Any] = {
        "failure_step": step,
        "safe_error_code": code,
        "upstream_status": error.status_code,
    }
    if recognition is not None:
        diagnostics["requested_match"] = _requested_match_diagnostics(recognition)
    if match is not None:
        diagnostics["match"] = _match_diagnostics(match)
    if realtime is not None:
        diagnostics["realtime_areas"] = _realtime_area_diagnostics(realtime)
    return HTTPException(status_code=error.status_code, detail={"code": code, "message": _quote_failure_message(code), "diagnostics": diagnostics})


WANDA_CINEMA_ALIASES = frozenset({"万达"})


def is_wanda_cinema_name(cinema: str | None) -> bool:
    return bool(cinema and any(alias in cinema for alias in WANDA_CINEMA_ALIASES))


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
        if total is not None and quantity > 0 and total % quantity == 0:
            candidates.append(total // quantity)
    if len(set(candidates)) != 1:
        raise HTTPException(status_code=422, detail="未找到唯一可用的 W+会员专享优惠价")
    return candidates[0]


def _all_seats_released(payload: Mapping[str, Any], expected: Sequence[SeatFact]) -> bool:
    available_ids = {seat.seat_id for seat in _seat_facts(payload)}
    return bool(expected) and all(seat.seat_id in available_ids for seat in expected)


class RealtimeQuoteService:
    def __init__(
        self,
        gateway: TicketGateway | None = None,
        *,
        cinema_catalog: LocalWandaCatalog | None = None,
        allow_friday_member_day: bool | None = None,
        direct_lock_gateway: Any | None = None,
    ) -> None:
        self._gateway = gateway or LocalTicketGateway()
        self._cinema_catalog = cinema_catalog
        self._direct_lock_gateway = direct_lock_gateway if direct_lock_gateway is not None else build_wanda_direct_gateway_from_env()
        self._allow_friday_member_day = (
            allow_friday_member_day
            if allow_friday_member_day is not None
            else os.getenv("WANDA_ALLOW_FRIDAY_MEMBER_DAY", "false").lower() == "true"
        )
        self._match_cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._match_inflight: dict[str, asyncio.Task[Mapping[str, Any]]] = {}

    async def aclose(self) -> None:
        """Drain bounded delayed seat-release checks before service shutdown."""
        wait_for_rechecks = getattr(self._direct_lock_gateway, "wait_for_background_rechecks", None)
        if callable(wait_for_rechecks):
            await wait_for_rechecks()

    @staticmethod
    def _has_complete_joint_identity(recognition: Recognition) -> bool:
        return bool(
            _text(recognition.cinema)
            and _text(recognition.movie)
            and recognition.date is not None
            and _showtime_start(recognition.showtime)
        )

    def _catalog_error(self, recognition: Recognition) -> HTTPException:
        explicit_cinema = _text(recognition.cinema)
        supported_brand = any(brand in explicit_cinema for brand in ("万达", "寰映", "儒意"))
        detail = "仅支持万达影院实时核价" if explicit_cinema and not supported_brand else "影院无法在官方影院库唯一匹配"
        return _diagnostic_failure(
            HTTPException(status_code=422, detail=detail),
            step="validate_cinema_catalog",
            recognition=recognition,
        )

    def _catalog_match_input(self, recognition: Recognition) -> tuple[Recognition, bool]:
        """Return a strict catalog match or defer bounded ambiguity to the full identity matcher."""
        if self._cinema_catalog is None:
            return recognition, False
        resolution = self._cinema_catalog.resolve(recognition)
        if resolution.matched:
            return resolution.recognition, False
        explicit_cinema = _text(recognition.cinema)
        supported_brand = any(brand in explicit_cinema for brand in ("万达", "寰映", "儒意"))
        if supported_brand and self._has_complete_joint_identity(recognition):
            # The ticket gateway searches the same official SQLite catalog and
            # resolves bounded cinema-name ambiguity with authoritative movie,
            # absolute date and start time.  The result is verified below.
            return recognition, True
        raise self._catalog_error(recognition)

    def _catalog_recognition(self, recognition: Recognition) -> Recognition:
        resolved, deferred = self._catalog_match_input(recognition)
        if deferred:
            raise self._catalog_error(recognition)
        return resolved

    def _verify_joint_match(self, match: Mapping[str, Any], cinema_id: str) -> None:
        root = _data(match)
        evidence = root.get("showtime_match")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        components = evidence.get("components")
        components = components if isinstance(components, Mapping) else {}

        def status_of(name: str) -> str:
            value = components.get(name)
            return _text(value.get("status")) if isinstance(value, Mapping) else ""

        confidence = evidence.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0
        candidate_count = evidence.get("candidate_count")
        candidate_count = candidate_count if isinstance(candidate_count, int) and not isinstance(candidate_count, bool) else 0
        verified = (
            confidence >= 0.85
            and candidate_count == 1
            and status_of("cinema") in {"exact", "normalized_partial", "address_fragment"}
            and status_of("movie") in {"exact", "short_unique"}
            and status_of("date") == "exact"
            and status_of("time") == "exact"
        )
        if not verified:
            raise HTTPException(status_code=422, detail="影院、影片和场次无法唯一匹配")
        contains = getattr(self._cinema_catalog, "contains_cinema_id", None)
        if not callable(contains) or not contains(cinema_id):
            raise HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")

    @staticmethod
    def _match_cache_key(recognition: Recognition) -> str:
        selected = ",".join(recognition.official_selection.selected_seat_numbers)
        date_text = recognition.date.isoformat() if recognition.date is not None else ""
        return "\u001f".join((
            _text(recognition.city), _text(recognition.cinema), _text(recognition.movie),
            date_text, _showtime_start(recognition.showtime) or "", _text(recognition.hall), selected,
        ))

    async def _read_only_match(self, gateway: TicketGateway, recognition: Recognition) -> Mapping[str, Any]:
        key = self._match_cache_key(recognition)
        now = time.monotonic()
        cached = self._match_cache.get(key)
        if cached and cached[0] > now:
            return deepcopy(cached[1])
        if cached:
            self._match_cache.pop(key, None)
        pending = self._match_inflight.get(key)
        if pending is None:
            pending = asyncio.create_task(gateway.match(recognition))
            self._match_inflight[key] = pending
        try:
            result = await asyncio.shield(pending)
        finally:
            if self._match_inflight.get(key) is pending and pending.done():
                self._match_inflight.pop(key, None)
        # Cache only a complete, uniquely identified showtime. Negative and
        # partial matches are coalesced while in flight but never retained.
        try:
            _showtime_and_cinema(result)
        except HTTPException:
            return result
        self._match_cache[key] = (time.monotonic() + 15.0, deepcopy(result))
        while len(self._match_cache) > 200:
            self._match_cache.pop(next(iter(self._match_cache)))
        return deepcopy(result)

    async def _match_joint_identity(
        self,
        gateway: TicketGateway,
        recognition: Recognition,
        joint_match_required: bool,
    ) -> tuple[Mapping[str, Any], Recognition, str, str, str | None]:
        initial_match = await self._read_only_match(gateway, recognition)
        try:
            showtime_id, cinema_id, cinema_name = _showtime_and_cinema(initial_match)
            if joint_match_required:
                self._verify_joint_match(initial_match, cinema_id)
            return initial_match, recognition, showtime_id, cinema_id, cinema_name
        except HTTPException as initial_error:
            # Wanda's seat-map title often truncates a long branch name with an
            # ellipsis. If the official matcher still identifies exactly one
            # canonical cinema, retry only that canonical identity. All movie,
            # absolute date, start time, hall and seat facts remain unchanged.
            raw_cinema = _text(recognition.cinema)
            canonical_cinema = _text(_match_diagnostics(initial_match).get("cinema"))
            truncated = "..." in raw_cinema or "…" in raw_cinema
            if not truncated or not canonical_cinema or canonical_cinema == raw_cinema:
                initial_error.match_result = initial_match
                raise initial_error
            retried_recognition = recognition.model_copy(update={"cinema": canonical_cinema})
            retried_match = await self._read_only_match(gateway, retried_recognition)
            try:
                showtime_id, cinema_id, cinema_name = _showtime_and_cinema(retried_match)
                if joint_match_required:
                    self._verify_joint_match(retried_match, cinema_id)
            except HTTPException as retried_error:
                retried_error.match_result = retried_match
                raise
            return retried_match, retried_recognition, showtime_id, cinema_id, cinema_name

    async def resolve_showtime(self, recognition: Recognition) -> QuoteShowtimeResolveResponse:
        """Resolve the joint showtime identity without reading seats or creating a temporary order."""
        recognition, joint_match_required = self._catalog_match_input(recognition)
        try:
            gateway = await self._gateway.for_quote()
            match, recognition, _showtime_id, cinema_id, matched_cinema_name = await self._match_joint_identity(
                gateway, recognition, joint_match_required,
            )
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(error, step="resolve_showtime", recognition=recognition, match=matched) from error
        resolved = recognition.model_copy(update={"cinema": matched_cinema_name}) if matched_cinema_name else recognition
        return QuoteShowtimeResolveResponse(recognition=resolved, matched_cinema_name=matched_cinema_name)

    async def available_wplus_seats(self, recognition: Recognition, row: int) -> AvailableWplusSeatsResponse:
        """List current read-only W+ availability for one explicitly requested row."""
        recognition, joint_match_required = self._catalog_match_input(recognition)
        try:
            gateway = await self._gateway.for_quote()
            match, recognition, showtime_id, cinema_id, matched_cinema_name = await self._match_joint_identity(
                gateway, recognition, joint_match_required,
            )
            realtime = await gateway.realtime_seats(showtime_id)
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(
                error,
                step="available_seats",
                recognition=recognition,
                match=matched,
                realtime=locals().get("realtime"),
            ) from error
        wplus_facts = [seat for seat in _seat_facts(realtime) if seat.zone_type is SeatZoneType.WPLUS]
        row_prefix = f"{row}排"
        labels = sorted(
            {seat.label for seat in wplus_facts if seat.label.startswith(row_prefix)},
            key=lambda label: int(re.search(r"排(\d{1,3})座", label).group(1)) if re.search(r"排(\d{1,3})座", label) else 10_000,
        )
        return AvailableWplusSeatsResponse(
            row=row,
            seats=labels[:30],
            available_count=len(labels),
            wplus_offer_available=any(seat.wplus_member_price_cents is not None for seat in wplus_facts),
            matched_cinema_name=matched_cinema_name,
        )

    async def _locked_member_offer(
        self,
        gateway: TicketGateway,
        *,
        showtime_id: str,
        cinema_id: str,
        seats: Sequence[SeatFact],
        stage_timings: dict[str, int] | None = None,
    ) -> int:
        if not seats:
            raise HTTPException(status_code=422, detail="未找到足够的 W+座位用于临时锁座核价")
        partition = _partition(seats)
        if self._direct_lock_gateway is not None:
            direct_started = time.perf_counter()
            try:
                result = await self._direct_lock_gateway.probe_activity_offers({
                    "cinema_id": cinema_id,
                    "showtime_id": showtime_id,
                    "seat_ids": [seat.seat_id for seat in seats],
                    "seat_payloads": [
                        f"{seat.seat_id},{seat.original_price_cents},{seat.channel_fee_cents},0"
                        for seat in seats
                    ],
                    "partition": partition,
                    "total_price_cents": sum(seat.original_price_cents for seat in seats),
                })
                offers = result.get("offers") if isinstance(result, Mapping) else None
                if not isinstance(offers, Mapping) or result.get("release_verified") is not True:
                    raise DirectGatewayError("temporary_lock_release_unverified")
                member_unit = _locked_offer_unit_cents(
                    offers,
                    quantity=len(seats),
                    allow_friday=self._allow_friday_member_day,
                )
                if stage_timings is not None:
                    stage_timings["temporary_lock"] = round((time.perf_counter() - direct_started) * 1000)
                    stage_timings["available_offers"] = 0
                    stage_timings["cancel"] = 0
                    stage_timings["release_recheck"] = 0
                return member_unit
            except DirectGatewayError as error:
                if error.code == "temporary_lock_release_unverified":
                    raise HTTPException(status_code=502, detail="临时锁座未确认释放，已停止报价") from error
                if error.code in {"wplus_account_unavailable", "account_lease_unavailable"}:
                    raise HTTPException(status_code=422, detail="线上账号池没有可用的 W+ 会员账号") from error
                raise HTTPException(status_code=502, detail="万达临时锁座核价失败") from error
        order_id = ""
        quote_error: BaseException | None = None
        member_unit: int | None = None
        released = False
        async with _showtime_lock(showtime_id):
            try:
                lock_started = time.perf_counter()
                lock_result = await gateway.lock({
                    "showtime_id": showtime_id,
                    "seat_ids": [
                        f"{seat.seat_id},{seat.original_price_cents},{seat.channel_fee_cents},0"
                        for seat in seats
                    ],
                    "total_price": sum(seat.original_price_cents for seat in seats),
                    "mobile": gateway.account_mobile(),
                    "phone": gateway.account_mobile(),
                    "cinema_id": cinema_id,
                    "quantity": len(seats),
                    "partition": partition,
                    # Probe seat labels are deliberately not persisted or
                    # exposed as the buyer's selected fulfillment seats.
                    "seat_names": [],
                })
                if stage_timings is not None:
                    stage_timings["temporary_lock"] = round((time.perf_counter() - lock_started) * 1000)
                lock_data = _data(lock_result)
                order_id = _identifier(lock_data.get("orderId") or lock_data.get("order_id"))
                if not order_id:
                    raise HTTPException(status_code=502, detail="万达临时锁座失败，未生成核价订单")
                offers_started = time.perf_counter()
                offers = await gateway.available_offers(
                    cinema_id=cinema_id,
                    showtime_id=showtime_id,
                    partition=partition,
                    order_id=order_id,
                )
                if stage_timings is not None:
                    stage_timings["available_offers"] = round((time.perf_counter() - offers_started) * 1000)
                member_unit = _locked_offer_unit_cents(
                    offers,
                    quantity=len(seats),
                    allow_friday=self._allow_friday_member_day,
                )
            except BaseException as error:
                quote_error = error
            finally:
                if order_id:
                    try:
                        cancel_started = time.perf_counter()
                        cancelled = await asyncio.shield(gateway.cancel(order_id))
                        if stage_timings is not None:
                            stage_timings["cancel"] = round((time.perf_counter() - cancel_started) * 1000)
                        if cancelled:
                            release_started = time.perf_counter()
                            for delay_seconds in RELEASE_RECHECK_DELAYS_SECONDS:
                                if delay_seconds:
                                    await asyncio.shield(asyncio.sleep(delay_seconds))
                                refreshed = await asyncio.shield(gateway.realtime_seats(showtime_id))
                                released = _all_seats_released(refreshed, seats)
                                if released:
                                    break
                            if stage_timings is not None:
                                stage_timings["release_recheck"] = round((time.perf_counter() - release_started) * 1000)
                    except BaseException:
                        released = False
        if order_id and not released:
            raise HTTPException(status_code=502, detail="临时锁座未确认释放，已停止报价")
        if quote_error is not None:
            raise quote_error
        if member_unit is None:
            raise HTTPException(status_code=422, detail="未找到唯一可用的 W+会员专享优惠价")
        return member_unit

    async def quote(
        self,
        request: QuoteRealtimeRequest,
        *,
        wplus_adjustment_cents: int = -290,
        wplus_member_price_threshold_cents: int = 6000,
        regular_adjustment_cents: int = REGULAR_SEAT_MARKUP_CENTS,
    ) -> QuoteRealtimeResponse:
        quote_started = time.perf_counter()
        timings_ms: dict[str, int] = {}
        recognition, joint_match_required = self._catalog_match_input(request.recognition)
        request = request.model_copy(update={"recognition": recognition})

        official_count = (
            len(recognition.official_selection.selected_seat_numbers)
            if recognition.official_selection.is_selected
            else 0
        )
        if request.ticket_count is not None and official_count and request.ticket_count != official_count:
            error = HTTPException(status_code=422, detail="文字张数与官方已选座张数不一致")
            raise _diagnostic_failure(error, step="validate_ticket_count", recognition=recognition)

        account_started = time.perf_counter()
        try:
            gateway = await self._gateway.for_quote()
            timings_ms["account"] = round((time.perf_counter() - account_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="account", recognition=recognition) from error
        # Hand-drawn marks neither select seats nor imply a ticket count.
        # When the platform has no confirmed selected seat, quote one W+
        # probe seat and ask the buyer how many tickets are needed.
        requested_count = request.ticket_count or official_count or None
        is_count_known = requested_count is not None
        match_started = time.perf_counter()
        try:
            match, recognition, showtime_id, cinema_id, matched_cinema_name = await self._match_joint_identity(
                gateway, request.recognition, joint_match_required,
            )
            timings_ms["match"] = round((time.perf_counter() - match_started) * 1000)
            request = request.model_copy(update={"recognition": recognition})
        except HTTPException as error:
            matched = locals().get("match") or getattr(error, "match_result", None)
            if joint_match_required and isinstance(matched, Mapping) and not _match_has_cinema(matched):
                error = HTTPException(status_code=422, detail="影院无法在官方影院库唯一匹配")
            raise _diagnostic_failure(error, step="match", recognition=recognition, match=matched) from error
        realtime_started = time.perf_counter()
        try:
            realtime = await gateway.realtime_seats(showtime_id)
            timings_ms["realtime_seats"] = round((time.perf_counter() - realtime_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="realtime_seats", recognition=recognition, match=match) from error
        all_seats = _seat_facts(realtime)
        selected_labels = request.recognition.official_selection.selected_seat_numbers
        selection_quantity = (
            len(selected_labels)
            if request.recognition.official_selection.is_selected and selected_labels
            else (requested_count or 1)
        )
        target_zone = (
            _requested_zone(request.recognition)
            if request.recognition.official_selection.is_selected
            else SeatZoneType.WPLUS
        )
        try:
            selected, is_exact, zone = _select_seats(request.recognition, all_seats, selection_quantity)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="select_seats", recognition=recognition, match=match, realtime=realtime) from error
        if not is_exact and request.ticket_count is None and official_count == 0:
            requested_count = None
            is_count_known = False
        exact_zones = {seat.zone_type for seat in selected} if is_exact else set()
        exact_uniform_selection = is_exact and len(exact_zones) == 1
        offer_quantity = len(selected) if exact_uniform_selection else (1 if is_exact else (requested_count or 1))
        if exact_uniform_selection:
            # A verified regular/discount/premium selection can expose the same
            # named W+ member offer through available-offers. Probe those exact
            # seats instead of requiring an unrelated available W+ area.
            offer_seats = selected
        elif is_exact:
            # Mixed-zone selections retain the established single-offer probe;
            # never average a multi-zone available-offers total across seats.
            offer_seats = _wplus_probe_candidates(all_seats)[:offer_quantity]
        else:
            offer_seats = selected
        if len(offer_seats) != offer_quantity:
            error = HTTPException(status_code=422, detail="未找到足够的 W+座位用于临时锁座核价")
            raise _diagnostic_failure(error, step="select_offer_seats", recognition=recognition, match=match, realtime=realtime)
        locked_offer_started = time.perf_counter()
        try:
            member_unit = await self._locked_member_offer(
                gateway,
                showtime_id=showtime_id,
                cinema_id=cinema_id,
                seats=offer_seats,
                stage_timings=timings_ms,
            )
            timings_ms["locked_offer"] = round((time.perf_counter() - locked_offer_started) * 1000)
        except HTTPException as error:
            raise _diagnostic_failure(error, step="locked_offer", recognition=recognition, match=match, realtime=realtime) from error

        calculate_started = time.perf_counter()
        if is_exact:
            try:
                seat_quotes = [
                    SeatQuote(
                        seat_number=seat.label,
                        seat_zone_type=seat.zone_type,
                        original_price_cents=seat.original_price_cents,
                        member_price_cents=member_unit,
                        channel_fee_cents=seat.channel_fee_cents,
                        unit_quote_cents=_bounded_quote_for_seat(
                            SeatFact(
                                seat.seat_id, seat.area_code, seat.original_price_cents,
                                member_unit, seat.channel_fee_cents, seat.label, seat.zone_type,
                            ),
                            seat.zone_type,
                            wplus_adjustment_cents=wplus_adjustment_cents,
                            wplus_member_price_threshold_cents=wplus_member_price_threshold_cents,
                            regular_adjustment_cents=regular_adjustment_cents,
                        ),
                    )
                    for seat in selected
                ]
            except HTTPException as error:
                raise _diagnostic_failure(error, step="calculate_quote", recognition=recognition, match=match, realtime=realtime) from error
            quote_values = {item.unit_quote_cents for item in seat_quotes}
            member_values = {item.member_price_cents for item in seat_quotes}
            zone_values = {item.seat_zone_type for item in seat_quotes}
            timings_ms["calculate_quote"] = round((time.perf_counter() - calculate_started) * 1000)
            timings_ms["total"] = round((time.perf_counter() - quote_started) * 1000)
            return QuoteRealtimeResponse(
                quote_scope="exact_seats",
                seat_zone_type=next(iter(zone_values)) if len(zone_values) == 1 else SeatZoneType.UNKNOWN,
                member_unit_price_cents=next(iter(member_values)) if len(member_values) == 1 else None,
                unit_quote_cents=next(iter(quote_values)) if len(quote_values) == 1 else None,
                total_quote_cents=sum(item.unit_quote_cents for item in seat_quotes),
                channel_fee_total_cents=sum(item.channel_fee_cents for item in seat_quotes),
                seat_quotes=seat_quotes,
                ticket_count=len(seat_quotes),
                needs_ticket_count=False,
                pricing_source="万达临时锁座 available-offers + 后台报价规则",
                detail="官方已选座已逐座核对；临时锁座读取优惠后已取消并确认座位恢复可售",
                matched_cinema_name=matched_cinema_name,
                timings_ms=timings_ms,
            )

        probe_seat = selected[0]
        quote_zone = target_zone if target_zone is not SeatZoneType.UNKNOWN else zone
        try:
            unit_quote = _bounded_quote_for_seat(
                SeatFact(
                    probe_seat.seat_id, probe_seat.area_code, probe_seat.original_price_cents,
                    member_unit, probe_seat.channel_fee_cents, probe_seat.label, probe_seat.zone_type,
                ),
                quote_zone,
                wplus_adjustment_cents=wplus_adjustment_cents,
                wplus_member_price_threshold_cents=wplus_member_price_threshold_cents,
                regular_adjustment_cents=regular_adjustment_cents,
            )
        except HTTPException as error:
            raise _diagnostic_failure(error, step="calculate_quote", recognition=recognition, match=match, realtime=realtime) from error
        total = unit_quote * requested_count if is_count_known and requested_count is not None else None
        detail = "图中未显示官方已选座，按万达实时座位图和后台规则试价；请确认需要几张"
        timings_ms["calculate_quote"] = round((time.perf_counter() - calculate_started) * 1000)
        timings_ms["total"] = round((time.perf_counter() - quote_started) * 1000)
        return QuoteRealtimeResponse(
            quote_scope="area_probe",
            seat_zone_type=quote_zone,
            member_unit_price_cents=member_unit,
            unit_quote_cents=unit_quote,
            total_quote_cents=total,
            channel_fee_total_cents=probe_seat.channel_fee_cents * requested_count if is_count_known and requested_count is not None else None,
            ticket_count=requested_count,
            needs_ticket_count=not is_count_known,
            pricing_source="万达临时锁座 available-offers + 后台报价规则",
            detail=f"{detail}；临时试价座位已取消并确认恢复可售",
            matched_cinema_name=matched_cinema_name,
            timings_ms=timings_ms,
        )
