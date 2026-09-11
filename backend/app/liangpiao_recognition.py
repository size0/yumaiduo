from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import date
from typing import Any, Mapping

import httpx

from .config import Settings
from .errors import ProviderError
from .liangpiao_client import LiangpiaoClient
from .models import (
    CinemaCandidate,
    MovieCandidate,
    MovieImageInfo,
    ProviderPriceOption,
    SelectedSeat,
    ShowCandidate,
)


_SHOWTIME_PATTERN = re.compile(r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:\d{2})")
_TIME_PATTERN = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)")


class LiangpiaoRecognitionClient:
    """Small, signed adapter for Liangpiao screenshot recognition APIs."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._api = LiangpiaoClient(settings, http_client=http_client)

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("LIANGPIAO_BASE_URL must use HTTPS")
        if normalized.endswith("/api/v1"):
            return normalized
        return f"{normalized}/api/v1"

    async def recognize_url(
        self, image_url: str, *, city_name: str | None = None,
        out_trade_no: str | None = None,
    ) -> MovieImageInfo:
        if self._settings.liangpiao_recognition_async_enabled:
            return await self.recognize_url_async(
                image_url, city_name=city_name, out_trade_no=out_trade_no,
            )
        kwargs: dict[str, Any] = {}
        if city_name and city_name.strip():
            kwargs["cityName"] = city_name.strip()
        try:
            data = await self._api.recognize(image_url, **kwargs)
        except ProviderError as error:
            # The synchronous endpoint has a documented short timeout.  A
            # transport timeout leaves the provider result unknown, so switch
            # once to the idempotent async task using the caller's stable
            # outTradeNo.  Business rejections must remain terminal and are
            # never retried through another endpoint.
            if error.code != "liangpiao_network_error":
                raise
            trade_no = str(out_trade_no or ("recognize-" + uuid.uuid4().hex)).strip()
            return await self.recognize_url_async(
                image_url, city_name=city_name, out_trade_no=trade_no,
            )
        return self._map_result(data)

    async def recognize_url_async(
        self, image_url: str, *, city_name: str | None = None,
        out_trade_no: str | None = None,
    ) -> MovieImageInfo:
        """Submit async recognition and poll task/detail until it is terminal.

        The event id is supplied by the caller as ``out_trade_no`` so a retry
        after a worker restart reuses the same provider task rather than
        creating a second billable recognition.
        """
        trade_no = str(out_trade_no or ("recognize-" + uuid.uuid4().hex)).strip()
        if not trade_no or len(trade_no) > 64:
            raise ValueError("liangpiao_out_trade_no_invalid")
        kwargs: dict[str, Any] = {}
        if city_name and city_name.strip():
            kwargs["cityName"] = city_name.strip()
        task = await self._api.recognize_async(
            image_url, out_trade_no=trade_no, **kwargs,
        )
        status = str(task.get("status") or "").upper()
        if status == "SUCCESS" and isinstance(task.get("result"), Mapping):
            return self._map_result(_merge_transport_metadata(task["result"], task))
        if status == "FAILED":
            raise ProviderError(
                f"liangpiao_async_recognition_{task.get('errorCode') or 'failed'}",
                str(task.get("errorMsg") or "良票异步识别失败，请重新发送截图。")[:200],
            )
        task_id = _text(task.get("taskId"))
        if not task_id:
            raise ProviderError("liangpiao_async_task_missing", "良票异步识别未返回任务号，请稍后重试。")
        deadline = time.monotonic() + self._settings.liangpiao_recognition_poll_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(self._settings.liangpiao_recognition_poll_interval_seconds)
            detail = await self._api.task_detail(
                task_id, trace_id=_text(task.get("trace_id")),
            )
            status = str(detail.get("status") or "").upper()
            if status == "SUCCESS":
                result = detail.get("result")
                if not isinstance(result, Mapping):
                    raise ProviderError("liangpiao_async_result_missing", "良票异步识别结果为空，请稍后重试。")
                return self._map_result(_merge_transport_metadata(result, detail))
            if status == "FAILED":
                raise ProviderError(
                    f"liangpiao_async_recognition_{detail.get('errorCode') or 'failed'}",
                    str(detail.get("errorMsg") or "良票异步识别失败，请重新发送截图。")[:200],
                )
        raise ProviderError("liangpiao_async_task_timeout", "良票识别任务处理超时，请稍后重试。")

    async def confirm(
        self, recognize_id: str, cinema_id: int | None = None, *,
        movie_id: int | None = None, show_id: str | None = None,
        city_name: str | None = None,
    ) -> MovieImageInfo:
        data = await self._api.confirm(
            recognize_id, cinema_id, movie_id=movie_id, show_id=show_id,
            city_name=city_name,
        )
        return self._map_result(data)

    async def aclose(self) -> None:
        await self._api.aclose()

    @classmethod
    def _map_result(cls, data: Mapping[str, Any]) -> MovieImageInfo:
        raw = data.get("rawResults") if isinstance(data.get("rawResults"), Mapping) else {}
        final = data.get("finalResults") if isinstance(data.get("finalResults"), Mapping) else {}
        showtime = _text(final.get("showtime")) or _text(raw.get("showtime"))
        show_date, show_start = _parse_showtime(showtime)
        final_seats = final.get("seat")
        raw_seats = raw.get("seat")
        seats_raw = final_seats if isinstance(final_seats, list) and final_seats else raw_seats
        seats: list[SelectedSeat] = []
        if isinstance(seats_raw, list):
            for item in seats_raw:
                if not isinstance(item, Mapping):
                    continue
                name = _text(item.get("seatName")) or _text(item.get("seat_name"))
                if name:
                    seats.append(SelectedSeat(
                        seat_number=name,
                        displayed_price=_seat_price(item),
                        row_no=_int(item.get("rowNo") or item.get("row_no")),
                        col_no=_int(item.get("colNo") or item.get("col_no")),
                        area_id=_text(item.get("areaId") or item.get("area_id")),
                        seat_no=_text(item.get("seatNo") or item.get("seat_no")),
                        status=_text(item.get("status")),
                    ))
        raw_city = _text(raw.get("city"))
        final_city = _text(final.get("city"))
        city_conflict = bool(raw_city and final_city and raw_city != final_city)
        candidate_payload = final.get("candidates")
        candidates = _map_candidates(candidate_payload, city_name=raw_city)
        candidate_movies = _map_movie_candidates(candidate_payload)
        candidate_shows = _map_show_candidates(candidate_payload)
        provider_prices = _map_provider_prices(final.get("prices"))
        no_match_reason = _enum_text(final.get("noMatchReason"))
        provider_match_level = _enum_text(final.get("matchLevel"))
        seat_matched = _bool(final.get("seatMatched"))
        price_mismatch = _bool(final.get("priceMismatch"))
        recognition_blocker = _recognition_blocker(
            no_match_reason=no_match_reason,
            seat_matched=seat_matched,
            price_mismatch=price_mismatch,
        )
        warnings = ["liangpiao_candidate_cinema"] if candidates else []
        resolved_city = raw_city if city_conflict else final_city or raw_city
        resolved_cinema = (
            _text(raw.get("cinema")) if city_conflict
            else _text(final.get("cinema")) or _text(raw.get("cinema"))
        )
        resolved_cinema_id = None if city_conflict else _int(final.get("cinemaId") or final.get("cinema_id"))
        resolved_movie = _text(final.get("film")) or _text(raw.get("film"))
        resolved_hall = _text(final.get("hall")) or _text(raw.get("hall"))
        match_level = "CANDIDATE" if city_conflict and resolved_cinema else provider_match_level
        normalized_no_match_reason = None if city_conflict else no_match_reason
        resolution_diagnostic = _resolution_diagnostic(
            city_conflict=city_conflict,
            city=resolved_city,
            cinema=resolved_cinema,
            showtime=show_start,
            show_id=_text(final.get("showId")),
            seat_matched=seat_matched,
            match_level=match_level,
        )
        missing_fields = [
            field for field, value in (
                ("city", resolved_city), ("cinema_name", resolved_cinema),
                ("movie_name", resolved_movie), ("date", show_date),
                ("showtime_start", show_start), ("hall_name", resolved_hall),
            ) if not value
        ]
        if (
            _bool(raw.get("cinemaTruncated")) is True
            and match_level != "EXACT"
            and "cinema_name" not in missing_fields
        ):
            missing_fields.append("cinema_name")
        return MovieImageInfo(
            platform=_text(final.get("platform")) or _text(raw.get("platform")),
            is_seat_selection=_bool(raw.get("isSeatSelection")),
            cinema_truncated=_bool(raw.get("cinemaTruncated")),
            cinema_id=resolved_cinema_id,
            cinema_name=resolved_cinema,
            cinema_address=_text(final.get("cinemaAddress") or final.get("cinema_address")),
            brand_name=_text(final.get("brandName") or final.get("brand_name")),
            city_code=_text(final.get("cityCode") or final.get("city_code")),
            city=resolved_city,
            movie_name=resolved_movie,
            movie_id=_int(final.get("movieId") or final.get("movie_id")),
            date_text=show_date.isoformat() if show_date else showtime,
            date=show_date,
            showtime_start=show_start,
            hall_name=resolved_hall,
            language=_text(final.get("language")) or _text(raw.get("language")),
            format=_text(final.get("dimension")) or _text(raw.get("dimension")),
            selected_seats=seats,
            selected_count_visible=len(seats),
            displayed_total=_displayed_total(final, raw),
            confidence=_float(raw.get("confidence"), default=_float(final.get("confidence"), default=0)),
            missing_fields=missing_fields,
            warnings=warnings,
            recognition_id=_text(data.get("recognizeId")),
            recognition_cached=_bool(data.get("cached")),
            match_level=match_level,
            no_match_reason=normalized_no_match_reason,
            provider_match_level=provider_match_level,
            provider_no_match_reason=no_match_reason,
            recognition_blocker=recognition_blocker,
            resolution_diagnostic=resolution_diagnostic,
            provider_request_id=_provider_request_id(data),
            trace_id=_text(data.get("trace_id")),
            raw_results=dict(raw),
            final_results=dict(final),
            raw_response=(
                dict(data["raw_response"])
                if isinstance(data.get("raw_response"), Mapping)
                else {}
            ),
            cinema_hit_count=(
                len(candidates)
                if raw_city and isinstance(candidate_payload, Mapping)
                and isinstance(candidate_payload.get("cinemas"), list)
                else _nonnegative_int(final.get("cinemaHitNums"))
            ),
            price_mismatch=price_mismatch,
            seat_matched=seat_matched,
            show_id=_text(final.get("showId")),
            candidate_cinemas=candidates,
            candidate_movies=candidate_movies,
            candidate_shows=candidate_shows,
            provider_prices=provider_prices,
        )


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _enum_text(value: Any) -> str | None:
    normalized = str(value or "").strip().upper()
    return normalized or None


def _recognition_blocker(
    *, no_match_reason: str | None, seat_matched: bool | None,
    price_mismatch: bool | None,
) -> str | None:
    if no_match_reason == "SHOW_EXPIRED":
        return "SHOW_EXPIRED"
    if seat_matched is False:
        return "SEAT_UNMATCHED"
    if price_mismatch is True:
        return "PRICE_MISMATCH"
    return None


def _merge_transport_metadata(
    result: Mapping[str, Any], transport: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(result)
    for key in ("raw_response", "trace_id", "request_id", "http_status"):
        if key in transport:
            merged[key] = transport[key]
    return merged


def _provider_request_id(data: Mapping[str, Any]) -> str | None:
    direct = _text(data.get("request_id"))
    if direct:
        return direct
    response = data.get("raw_response")
    if not isinstance(response, Mapping):
        return None
    return _text(response.get("requestId") or response.get("request_id"))


def _displayed_total(final: Mapping[str, Any], raw: Mapping[str, Any]) -> float | None:
    fen = final.get("priceAllFen") or final.get("price_all_fen")
    if fen is not None:
        try:
            return int(fen) / 100
        except (TypeError, ValueError):
            pass
    value = raw.get("priceAll") or raw.get("price_all")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _seat_price(value: Mapping[str, Any]) -> float | None:
    fen = value.get("seatPriceFen") or value.get("seat_price_fen")
    if fen is not None:
        try:
            return int(fen) / 100
        except (TypeError, ValueError):
            return None
    price = value.get("seatPrice") or value.get("seat_price")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


def _optional_cents(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _map_movie_candidates(value: Any) -> list[MovieCandidate]:
    movies = value.get("movies") if isinstance(value, Mapping) else None
    if not isinstance(movies, list):
        return []
    candidates: list[MovieCandidate] = []
    for item in movies[:5]:
        if not isinstance(item, Mapping):
            continue
        movie_id = _int(item.get("movieId") or item.get("movie_id"))
        name = _text(item.get("name") or item.get("movieName") or item.get("film"))
        if movie_id is None or not name:
            continue
        candidates.append(MovieCandidate(
            movie_id=movie_id, name=name, score=_float(item.get("score"), default=0),
        ))
    return candidates


def _map_show_candidates(value: Any) -> list[ShowCandidate]:
    shows = value.get("shows") if isinstance(value, Mapping) else None
    if not isinstance(shows, list):
        return []
    candidates: list[ShowCandidate] = []
    for item in shows[:10]:
        if not isinstance(item, Mapping):
            continue
        show_id = _text(item.get("showId") or item.get("show_id"))
        if not show_id:
            continue
        candidates.append(ShowCandidate(
            show_id=show_id,
            cinema_id=_int(item.get("cinemaId") or item.get("cinema_id")),
            movie_id=_int(item.get("movieId") or item.get("movie_id")),
            movie_name=_text(item.get("movieName") or item.get("movie_name") or item.get("film")),
            hall_name=_text(item.get("hallName") or item.get("hall_name") or item.get("hall")),
            start_time=_text(item.get("startTime") or item.get("start_time") or item.get("showtime")),
            end_time=_text(item.get("endTime") or item.get("end_time")),
            dimension=_text(item.get("dimension") or item.get("format")),
            language=_text(item.get("language")),
            score=_float(item.get("score"), default=0),
        ))
    return candidates


def _map_provider_prices(value: Any) -> list[ProviderPriceOption]:
    if not isinstance(value, list):
        return []
    prices: list[ProviderPriceOption] = []
    for item in value[:12]:
        if not isinstance(item, Mapping):
            continue
        ticket_mode = _text(item.get("ticketMode") or item.get("ticket_mode"))
        price_mode = _text(item.get("priceMode") or item.get("price_mode"))
        if not ticket_mode or not price_mode:
            continue
        prices.append(ProviderPriceOption(
            ticket_mode=ticket_mode,
            price_mode=price_mode,
            price_cents=_optional_cents(item.get("price")),
            max_price_cents=_optional_cents(item.get("maxPrice") or item.get("max_price")),
            original_price_cents=_optional_cents(item.get("originalPrice") or item.get("original_price")),
            stop_sale_time=_text(item.get("stopSaleTime") or item.get("stop_sale_time")),
            available=bool(_bool(item.get("available"))),
        ))
    return prices


def _parse_showtime(value: str | None) -> tuple[date | None, str | None]:
    if not value:
        return None, None
    match = _SHOWTIME_PATTERN.search(value)
    if match:
        try:
            parsed_date = date.fromisoformat(match.group("date"))
        except ValueError:
            parsed_date = None
        return parsed_date, _normalize_time(match.group("time"))
    time_match = _TIME_PATTERN.search(value)
    return None, _normalize_time(time_match.group(0)) if time_match else None


def _normalize_time(value: str) -> str | None:
    match = _TIME_PATTERN.fullmatch(value)
    if not match:
        return None
    hour = int(match.group("hour"))
    return f"{hour:02d}:{match.group('minute')}" if 0 <= hour <= 23 else None


def _resolution_diagnostic(
    *, city_conflict: bool, city: str | None, cinema: str | None,
    showtime: str | None, show_id: str | None, seat_matched: bool | None,
    match_level: str | None,
) -> str | None:
    if city_conflict:
        return "RESOLVER_WRONG_MATCH"
    if not city or not cinema:
        return "RAW_RECOGNITION_INSUFFICIENT"
    if match_level not in {"EXACT", "RESOLVED"} and not show_id and showtime:
        return "SHOW_NOT_RESOLVED"
    if seat_matched is False:
        return "SEAT_NOT_RESOLVED"
    return None


def _map_candidates(
    value: Any, *, city_name: str | None = None,
) -> list[CinemaCandidate]:
    cinemas = value.get("cinemas") if isinstance(value, Mapping) else None
    if not isinstance(cinemas, list):
        return []
    expected_city = _text(city_name)
    candidates: list[CinemaCandidate] = []
    for item in cinemas:
        if not isinstance(item, Mapping):
            continue
        candidate_city = _text(item.get("cityName") or item.get("city_name"))
        if expected_city and candidate_city != expected_city:
            continue
        try:
            cinema_id = int(item.get("cinemaId"))
        except (TypeError, ValueError):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        candidates.append(CinemaCandidate(
            cinema_id=cinema_id,
            name=name,
            city_name=candidate_city,
            address=_text(item.get("address")),
            score=_float(item.get("score"), default=0),
        ))
        if len(candidates) >= 5:
            break
    return candidates
