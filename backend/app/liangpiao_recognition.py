from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import date
from typing import Any, Mapping

import httpx

from .config import Settings
from .errors import ProviderError
from .liangpiao_client import LiangpiaoClient
from .models import CinemaCandidate, MovieImageInfo, SelectedSeat


_SHOWTIME_PATTERN = re.compile(r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:\d{2})")
_TIME_PATTERN = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)")


class LiangpiaoRecognitionClient:
    """Small, signed adapter for Liangpiao screenshot recognition APIs."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._api = LiangpiaoClient(settings, http_client=http_client)

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("LIANGPIAO_BASE_URL must use HTTPS")
        if normalized.endswith("/api/v1"):
            return normalized
        return f"{normalized}/api/v1"

    async def recognize_url(self, image_url: str, *, city_name: str | None = None) -> MovieImageInfo:
        kwargs: dict[str, Any] = {}
        if city_name and city_name.strip():
            kwargs["cityName"] = city_name.strip()
        data = await self._api.recognize(image_url, **kwargs)
        return self._map_result(data)

    async def confirm(self, recognize_id: str, cinema_id: int) -> MovieImageInfo:
        data = await self._api.confirm(recognize_id, cinema_id)
        return self._map_result(data)

    async def _request(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._api.request(path, payload)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        sign_source = f"{self._app_key}{timestamp}{nonce}{body}".encode()
        signature = hmac.new(self._app_secret.encode(), sign_source, hashlib.sha256).hexdigest()
        try:
            response = await self._client.post(
                f"{self._base_url}/{path}",
                content=body.encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "x-app-key": self._app_key,
                    "x-timestamp": timestamp,
                    "x-nonce": nonce,
                    "x-sign": signature,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise ProviderError("liangpiao_network_error", "良票识别服务暂时不可用，请稍后重试。") from error
        try:
            parsed = response.json()
        except ValueError as error:
            raise ProviderError("liangpiao_invalid_response", "良票识别服务返回格式异常。") from error
        if not response.is_success:
            raise ProviderError("liangpiao_http_error", "良票识别服务暂时不可用，请稍后重试。")
        if not isinstance(parsed, Mapping) or parsed.get("code") != 0 or not isinstance(parsed.get("data"), Mapping):
            raise ProviderError("liangpiao_business_error", "良票识别未能完成，请重新发送截图。")
        return parsed["data"]

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
                    ))
        candidates = _map_candidates(final.get("candidates"))
        match_level = _text(final.get("matchLevel"))
        warnings = ["liangpiao_candidate_cinema"] if candidates else []
        return MovieImageInfo(
            platform=_text(final.get("platform")) or _text(raw.get("platform")),
            city=_text(final.get("city")) or _text(raw.get("city")),
            cinema_name=_text(final.get("cinema")) or _text(raw.get("cinema")),
            movie_name=_text(final.get("film")) or _text(raw.get("film")),
            date_text=show_date.isoformat() if show_date else showtime,
            date=show_date,
            showtime_start=show_start,
            hall_name=_text(final.get("hall")) or _text(raw.get("hall")),
            language=_text(final.get("language")) or _text(raw.get("language")),
            format=_text(final.get("dimension")) or _text(raw.get("dimension")),
            selected_seats=seats,
            selected_count_visible=len(seats),
            confidence=_float(raw.get("confidence"), default=_float(final.get("confidence"), default=0)),
            missing_fields=[],
            warnings=warnings,
            recognition_id=_text(data.get("recognizeId")),
            match_level=match_level,
            show_id=_text(final.get("showId")),
            candidate_cinemas=candidates,
        )


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


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


def _float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


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


def _map_candidates(value: Any) -> list[CinemaCandidate]:
    cinemas = value.get("cinemas") if isinstance(value, Mapping) else None
    if not isinstance(cinemas, list):
        return []
    candidates: list[CinemaCandidate] = []
    for item in cinemas[:5]:
        if not isinstance(item, Mapping):
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
            city_name=_text(item.get("cityName")),
            address=_text(item.get("address")),
            score=_float(item.get("score"), default=0),
        ))
    return candidates
