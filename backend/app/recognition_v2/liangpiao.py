from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..liangpiao_client import LiangpiaoClient
from .models import RecognitionResult


@dataclass(frozen=True)
class LiangpiaoRecognitionResponse:
    """The provider data and the lossless response envelope, without business mapping."""

    data: Mapping[str, Any]
    raw_provider_result: Mapping[str, Any]


class LiangpiaoV2Transport:
    """Thin transport for exactly one read-only Liangpiao recognition endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = LiangpiaoClient(settings, http_client=http_client)
        self._timeout_seconds = settings.liangpiao_request_timeout_seconds

    async def recognize(
        self,
        image_url: str,
        *,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> LiangpiaoRecognitionResponse:
        image = str(image_url or "").strip()
        if not image or len(image) > 2_048:
            raise ValueError("recognition_v2_image_url_invalid")
        data = await self._client.request(
            "recognize",
            {"imageUrl": image},
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds or self._timeout_seconds,
        )
        raw = data.get("raw_response")
        raw_provider_result = dict(raw) if isinstance(raw, Mapping) else dict(data)
        return LiangpiaoRecognitionResponse(
            data=data,
            raw_provider_result=raw_provider_result,
        )

    async def enrich(self, recognition: RecognitionResult) -> RecognitionResult:
        """Enrich incomplete recognition with provider read facts.

        Provider IDs are evidence for these read calls only.  They are never
        converted into Wanda identifiers and the route service still performs
        the authoritative Wanda directory match afterward.
        """
        if not recognition.cinema_truncated and recognition.city_text and recognition.cinema_text:
            return recognition
        ids = _final_ids(recognition.raw_provider_result)
        cinema_id = ids.get("cinema_id")
        show_id = ids.get("show_id")
        if not cinema_id and not show_id:
            return recognition
        responses: list[Mapping[str, Any]] = []
        if cinema_id:
            try:
                responses.append(await self._client.cinema_detail(
                    cinemaId=int(cinema_id),
                    trace_id=f"{recognition.provider_recognize_id or 'recognition'}:cinema-enrichment",
                ))
            except Exception:
                pass
        if show_id:
            try:
                responses.append(await self._client.show_detail(
                    showId=show_id,
                    trace_id=f"{recognition.provider_recognize_id or 'recognition'}:show-enrichment",
                ))
            except Exception:
                pass
        city = recognition.city_text or _first_response_value(responses, "cityName", "city_name", "city")
        cinema = recognition.cinema_text
        if recognition.cinema_truncated or not cinema:
            cinema = _first_response_value(responses, "cinemaName", "cinema_name", "cinema", "name")
        hall = recognition.hall or _first_response_value(responses, "hallName", "hall_name", "hall")
        movie = recognition.movie or _first_response_value(responses, "filmName", "movieName", "movie", "film")
        show_date = recognition.show_date or _first_response_value(responses, "showDate", "show_date", "date")
        start_time = recognition.start_time or _first_response_value(
            responses, "realtime", "realTime", "startTime", "showtimeStart", "showtime_start",
        )
        # Do not mark a truncated value complete unless the provider supplied
        # a non-empty replacement and a city was recovered as well.
        applied = bool(city and cinema and (recognition.cinema_truncated or city != recognition.city_text))
        if not applied:
            return recognition
        return recognition.model_copy(update={
            "city_text": city,
            "cinema_text": cinema,
            "cinema_truncated": False,
            "hall": hall,
            "movie": movie,
            "show_date": show_date,
            "start_time": _normalize_time(start_time),
        })

    async def aclose(self) -> None:
        await self._client.aclose()


def _final_ids(raw: Mapping[str, Any]) -> dict[str, str | None]:
    data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
    final = data.get("finalResults") if isinstance(data.get("finalResults"), Mapping) else {}
    def pick(*names: str) -> str | None:
        for name in names:
            value = str(final.get(name) or "").strip()
            if value:
                return value
        return None
    return {
        "cinema_id": pick("cinemaId", "cinema_id"),
        "show_id": pick("showId", "show_id"),
    }


def _walk_values(value: object, names: tuple[str, ...]):
    if isinstance(value, Mapping):
        for name in names:
            candidate = value.get(name)
            if candidate is not None and str(candidate).strip():
                yield candidate
        for child in value.values():
            yield from _walk_values(child, names)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child, names)


def _first_response_value(responses: list[Mapping[str, Any]], *names: str) -> str | None:
    for response in responses:
        for value in _walk_values(response, tuple(names)):
            text = str(value).strip()
            if text:
                return text
    return None


def _normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text
