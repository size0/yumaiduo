from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from .models import ShowResolutionResult, WandaShowCandidate
from .wanda_source import WandaShowSource


class ShowResolveV2Service:
    """Resolve a Wanda show ID only after the store ID is already authoritative."""

    def __init__(self, source: WandaShowSource) -> None:
        self._source = source

    async def resolve(self, request: Mapping[str, Any]) -> ShowResolutionResult:
        store_id = _text(request.get("wanda_store_id"))
        movie = _text(request.get("movie"))
        show_date = _date_key(request.get("show_date"))
        start_time = _time_key(request.get("start_time"))
        if not store_id or not movie or not show_date or not start_time:
            return ShowResolutionResult(
                status="INPUT_INCOMPLETE",
                wanda_store_id=store_id,
                movie_name=movie,
                show_date=show_date,
                start_time=start_time,
                resolution_reason="WANDA_STORE_MOVIE_DATE_TIME_REQUIRED",
            )

        try:
            response = await self._source.get_showtimes(store_id, show_date.replace("-", ""))
            if not _provider_success(response):
                return _provider_unavailable(store_id, movie, show_date, start_time)
            official = _flatten_showtimes(response)
        except Exception:
            return _provider_unavailable(store_id, movie, show_date, start_time)

        core_matches = [
            item for item in official
            if item["show_date"] == show_date
            and item["start_time"] == start_time
            and _movie_key(item["movie_name"]) == _movie_key(movie)
        ]
        core_matches = _dedupe_shows(core_matches)
        if not core_matches:
            return ShowResolutionResult(
                status="NOT_FOUND",
                wanda_store_id=store_id,
                movie_name=movie,
                show_date=show_date,
                start_time=start_time,
                resolution_reason="WANDA_SHOW_NOT_FOUND",
            )
        if len(core_matches) == 1:
            return _resolved(store_id, core_matches[0], "UNIQUE_MOVIE_DATE_TIME_MATCH")

        ranked = [
            (_auxiliary_rank(item, request), item)
            for item in core_matches
        ]
        best_rank = max(rank for rank, _ in ranked)
        best = [item for rank, item in ranked if rank == best_rank]
        if best_rank != (0, 0, 0) and len(best) == 1:
            return _resolved(store_id, best[0], "AUXILIARY_FIELDS_DISAMBIGUATED")

        candidates = [_candidate(item) for item in core_matches]
        return ShowResolutionResult(
            status="CANDIDATE_REQUIRED",
            wanda_store_id=store_id,
            movie_name=movie,
            show_date=show_date,
            start_time=start_time,
            resolution_reason="MULTIPLE_WANDA_SHOWS_REQUIRE_DISAMBIGUATION",
            candidate_count=len(candidates),
            candidates=candidates,
        )


def _provider_success(response: Mapping[str, Any]) -> bool:
    code = response.get("code")
    return code in (None, 0, "0") and isinstance(response.get("data"), Mapping)


def _flatten_showtimes(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = response["data"]
    groups = data.get("showtimeFilmInf") or data.get("films") or []
    if not isinstance(groups, list):
        return []
    result: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        group_movie = _first(group, "filmName", "movieName", "name")
        date_groups = group.get("showtimeFilmDateInf") or group.get("dates") or []
        if not isinstance(date_groups, list):
            continue
        for date_group in date_groups:
            if not isinstance(date_group, Mapping):
                continue
            group_date = _date_key(_first(date_group, "date", "showDate"))
            show_container = date_group.get("showtimesInf") or date_group.get("showtimes") or {}
            if isinstance(show_container, Mapping):
                shows = show_container.get("showtimeList") or show_container.get("shows") or []
            else:
                shows = show_container
            if not isinstance(shows, list):
                continue
            for show in shows:
                if not isinstance(show, Mapping):
                    continue
                films = show.get("filmList")
                film = films[0] if isinstance(films, list) and films and isinstance(films[0], Mapping) else {}
                movie = (
                    _first(show, "filmName", "movieName", "movie", "name")
                    or _first(film, "filmName", "movieName", "name")
                    or group_movie
                )
                show_date = _date_key(_first(show, "showDate", "date")) or group_date
                result.append({
                    "wanda_show_id": _first(show, "showtimeId", "showId", "id"),
                    "wanda_film_id": (
                        _first(show, "filmId")
                        or _first(film, "filmId")
                        or _first(group, "filmId")
                    ),
                    "movie_name": movie,
                    "show_date": show_date,
                    "start_time": _time_key(_first(show, "realtime", "realTime", "startTime", "showtime", "beginTime")),
                    "hall_name": _first(show, "hallName", "hall"),
                    "language": _first(show, "language") or _first(film, "language"),
                    "dimension": (
                        _first(show, "dimension", "format", "edition", "ver")
                        or _first(film, "version", "dimension", "format")
                    ),
                    "sales_price_fen": _price(show.get("salesPrice")),
                    "min_area_price_fen": _price(show.get("minAreaPrice")),
                    "wplus_activity_price_fen": _price(show.get("wPlusActivityPrice")),
                    "wplus_activity_code_hint": _first(show, "wPlusActivityCode"),
                })
    return [item for item in result if item["wanda_show_id"] and item["movie_name"] and item["show_date"] and item["start_time"]]


def _auxiliary_rank(item: Mapping[str, str | None], request: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(_same_hall(request.get("hall"), item.get("hall_name"))),
        int(_same_value(request.get("dimension"), item.get("dimension"))),
        int(_same_language(request.get("language"), item.get("language"))),
    )


def _same_hall(left: Any, right: Any) -> bool:
    left_key = _hall_key(left)
    right_key = _hall_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _same_value(left: Any, right: Any) -> bool:
    left_key = _simple_key(left)
    right_key = _simple_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _same_language(left: Any, right: Any) -> bool:
    left_key = _simple_key(left).replace("英语", "英文")
    right_key = _simple_key(right).replace("英语", "英文")
    return bool(left_key and right_key and left_key == right_key)


def _hall_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def _movie_key(value: Any) -> str:
    return _simple_key(value)


def _simple_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text)


def _date_key(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/]?(\d{1,2})[-/]?(\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _price(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _time_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{10,13}", text):
        timestamp = int(text)
        if len(text) == 13:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        except (OverflowError, OSError, ValueError):
            return None
    match = re.search(r"(?<!\d)(\d{1,2}):([0-5]\d)", text)
    if not match:
        return None
    hour = int(match.group(1))
    return f"{hour:02d}:{match.group(2)}" if 0 <= hour <= 23 else None


def _first(item: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = _text(item.get(name))
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _dedupe_shows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        show_id = str(item["wanda_show_id"])
        if show_id not in seen:
            seen.add(show_id)
            result.append(item)
    return result


def _candidate(item: Mapping[str, Any]) -> WandaShowCandidate:
    return WandaShowCandidate(
        wanda_show_id=str(item["wanda_show_id"]),
        wanda_film_id=item.get("wanda_film_id"),
        movie_name=item.get("movie_name"),
        start_time=item.get("start_time"),
        hall_name=item.get("hall_name"),
        language=item.get("language"),
        dimension=item.get("dimension"),
        sales_price_fen=item.get("sales_price_fen"),
        min_area_price_fen=item.get("min_area_price_fen"),
        wplus_activity_price_fen=item.get("wplus_activity_price_fen"),
        wplus_activity_code_hint=item.get("wplus_activity_code_hint"),
    )


def _resolved(store_id: str, item: Mapping[str, Any], reason: str) -> ShowResolutionResult:
    return ShowResolutionResult(
        status="RESOLVED",
        wanda_store_id=store_id,
        wanda_show_id=str(item["wanda_show_id"]),
        wanda_film_id=item.get("wanda_film_id"),
        movie_name=item.get("movie_name"),
        show_date=item.get("show_date"),
        start_time=item.get("start_time"),
        hall_name=item.get("hall_name"),
        language=item.get("language"),
        dimension=item.get("dimension"),
        sales_price_fen=item.get("sales_price_fen"),
        min_area_price_fen=item.get("min_area_price_fen"),
        wplus_activity_price_fen=item.get("wplus_activity_price_fen"),
        wplus_activity_code_hint=item.get("wplus_activity_code_hint"),
        resolution_reason=reason,
    )


def _provider_unavailable(store_id: str, movie: str, show_date: str, start_time: str) -> ShowResolutionResult:
    return ShowResolutionResult(
        status="PROVIDER_UNAVAILABLE",
        wanda_store_id=store_id,
        movie_name=movie,
        show_date=show_date,
        start_time=start_time,
        resolution_reason="WANDA_SHOW_PROVIDER_UNAVAILABLE",
    )
