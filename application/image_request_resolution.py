"""Resolve a text cinema plus existing OCR facts using read-only catalogues."""

from copy import deepcopy
from datetime import datetime, timedelta
import re

from .cinema_matching import match_cinema, name_key, title_key
from .ticket_details import CHINA, parse_date, parse_time


class ResolutionPending(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def resolve(api, conv, result, request):
    """Return resolved image facts and the independently verified cinema ID.

    No image/model call, quote, order or provider selection occurs here.
    Missing supplier identities are not proof of a text/image contradiction.
    """
    if any(
        request.get(k, getattr(conv, k, d)) != getattr(conv, k, d)
        for k, d in (("demand_id", ""), ("demand_rev", 0))
    ):
        raise ResolutionPending(
            "STALE_REQUEST",
            "之前的购票需求已经变化，请确认本次影院和位置；不会套用旧需求。",
        )
    raw = result.raw_results or {}
    if not request.get("cinema") and result.match_level == "EXACT":
        return result, None
    cinema = str(request.get("cinema") or raw.get("cinema") or result.cinema_name or "")
    if not cinema:
        raise ResolutionPending(
            "CINEMA_MISSING", "请补充影院门店名称；图片里已有的影片、时间和座位会保留。"
        )
    if (
        result.match_level == "EXACT"
        and not result.cinema_truncated
        and result.cinema_id
        and title_key(cinema) == title_key(result.cinema_name)
    ):
        return result, result.cinema_id
    city = str(raw.get("city") or result.city_name or request.get("city") or "")
    matched = match_cinema(
        api,
        cinema,
        city_name=city,
        city_code=(result.final_results or {}).get("cityCode") or "",
    )
    if matched["status"] != "MATCHED":
        raise ResolutionPending(matched["status"], matched["reason"])
    selected = matched["cinema"]
    if result.cinema_id and str(result.cinema_id) != str(selected["cinemaId"]):
        raise ResolutionPending(
            "CINEMA_CONFLICT",
            "文字影院与截图已匹配的影院不同，请确认本次要哪家影院；其他信息已保留。",
        )
    image_cinema = raw.get("cinema") or result.cinema_name
    if image_cinema:
        image_key = name_key(image_cinema, city)
        directory_key = name_key(selected["name"], city)
        compatible = image_key == directory_key or (
            raw.get("cinemaTruncated")
            and len(image_key) >= 2
            and directory_key.startswith(image_key)
        )
        if not compatible:
            raise ResolutionPending(
                "CINEMA_CONFLICT",
                "文字影院与截图中的门店尚未核对一致，请确认具体门店；影片、时间和座位已保留。",
            )
    if result.match_level == "EXACT" and result.show_id and result.start_time:
        resolved = deepcopy(result)
        resolved.cinema_name = selected["name"]
        resolved.cinema_truncated = False
        return resolved, selected["cinemaId"]
    movie = str(raw.get("film") or result.movie_name or request.get("movie") or "")
    start = raw.get("showtime") or result.start_time
    if not start and request.get("date") and request.get("time"):
        time, ambiguous = parse_time(request["time"])
        if ambiguous:
            raise ResolutionPending(
                "TIME_AMBIGUOUS", "请确认开场时间是上午还是晚上；其他已识别信息会保留。"
            )
        start = parse_date(request["date"]) + "T" + time
    if not movie or not start:
        raise ResolutionPending(
            "SHOW_DETAILS_MISSING",
            "影院已核对，还需要"
            + ("影片名" if not movie else "")
            + ("和" if not movie and not start else "")
            + ("观影日期及开场时间" if not start else "")
            + "，其他已识别信息会保留。",
        )
    try:
        stamp = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        stamp = (
            stamp.replace(tzinfo=CHINA)
            if stamp.tzinfo is None
            else stamp.astimezone(CHINA)
        )
        day = stamp.date()
        business_day = day - timedelta(days=1) if stamp.hour < 8 else day
        rows = api.ch4_show_list(selected["cinemaId"], business_day.isoformat())

        def at(row):
            value = datetime.fromisoformat(
                str(row.get("startTime") or "").replace("Z", "+00:00")
            )
            return (
                value.replace(tzinfo=CHINA)
                if value.tzinfo is None
                else value.astimezone(CHINA)
            )

        hits = []
        for row in rows:
            if title_key(row.get("movieName")) != title_key(movie):
                continue
            try:
                if at(row).replace(second=0, microsecond=0) == stamp.replace(
                    second=0, microsecond=0
                ):
                    hits.append(row)
            except (ValueError, TypeError):
                continue
        if len(hits) != 1 or not hits[0].get("showId"):
            raise ResolutionPending(
                "SHOW_NOT_UNIQUE",
                "影院已核对，但该影片在所给开场时间的场次尚未唯一确认，请核对开场时间或影厅。",
            )
        show = api.ch4_show_detail(str(hits[0]["showId"]))
        if (
            str(show.get("showId")) != str(hits[0]["showId"])
            or str(show.get("cinemaId")) != str(selected["cinemaId"])
            or title_key(show.get("movieName")) != title_key(movie)
            or at(show).replace(second=0, microsecond=0)
            != stamp.replace(second=0, microsecond=0)
            or not show.get("movieId")
            or not show.get("hallName")
        ):
            raise ResolutionPending(
                "SHOW_DETAIL_CONFLICT",
                "场次详情与本次影院、影片或开场时间不一致，暂不报价，请店家核对。",
            )
        # An explicit OCR hall identity must not be replaced by a different one.
        wanted_hall = str(raw.get("hall") or result.hall_name or "")

        def formats(value):
            text = str(value or "").upper()
            return {
                token
                for token in ("IMAX", "IMAXGT", "4DX", "CINITY", "杜比", "PRIME")
                if token in text
            }

        wanted_formats = formats(wanted_hall + " " + str(raw.get("dimension") or ""))
        actual_formats = formats(
            str(show["hallName"]) + " " + str(show.get("dimension") or "")
        )
        if not wanted_formats.issubset(actual_formats):
            raise ResolutionPending(
                "HALL_FORMAT_CONFLICT",
                "截图的影厅类型与排片详情尚未一致，请确认是否为"
                + "、".join(sorted(wanted_formats))
                + "场次；其他信息已保留，暂不按普通厅报价。",
            )
        if re.search(r"[\d一二三四五六七八九十]+\s*(?:号|厅)", wanted_hall):
            from .ticket_flow import _hall_mismatch

            if _hall_mismatch(wanted_hall, show["hallName"]):
                raise ResolutionPending(
                    "HALL_CONFLICT",
                    "截图影厅与查询到的场次不同，请确认本次影厅；其他信息已保留。",
                )
    except ResolutionPending:
        raise
    except Exception:
        raise ResolutionPending(
            "SHOW_QUERY_UNAVAILABLE",
            "影院已保留，但排片详情暂未查询成功，请店家核对；不会沿用旧场次报价。",
        ) from None
    resolved = deepcopy(result)
    resolved.match_level = "EXACT"
    resolved.no_match_reason = None
    resolved.cinema_id = selected["cinemaId"]
    resolved.cinema_name = selected["name"]
    resolved.cinema_address = selected["address"]
    resolved.city_name = selected["cityName"]
    resolved.cinema_truncated = False
    resolved.cinema_hit_nums = 1
    resolved.show_id = str(show["showId"])
    resolved.movie_id = show["movieId"]
    resolved.movie_name = show["movieName"]
    resolved.start_time = at(show).isoformat()
    resolved.hall_name = show["hallName"]
    resolved.candidates = {}
    resolved.final_results = {
        **(resolved.final_results or {}),
        "cityCode": selected["cityCode"],
    }
    resolved.raw_results = {**raw, "directory_resolved_image_request": True}
    for seat in resolved.seats:
        if isinstance(seat, dict) and not seat.get("seatNo") and seat.get("seatName"):
            seat["seatNo"] = seat["seatName"]
    if resolved.seats:
        resolved.expected_seat_count = len(resolved.seats)
    return resolved, selected["cinemaId"]
