from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import HTTPException

from .schemas import Recognition
from .wanda_quote_domain import _data, _identifier, _positive_int, _text


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

quote_failure_code = _quote_failure_code
diagnostic_failure = _diagnostic_failure
