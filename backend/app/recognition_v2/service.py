from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .liangpiao import LiangpiaoRecognitionResponse, LiangpiaoV2Transport
from .models import RecognitionResult
from ..recovery.service_contracts import RecognitionGateMixin


_SHOWTIME_PATTERN = re.compile(r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:[0-5]\d)")
_TIME_PATTERN = re.compile(r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)")


class RecognitionV2Transport(Protocol):
    async def recognize(
        self,
        image_url: str,
        *,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> LiangpiaoRecognitionResponse: ...

    async def aclose(self) -> None: ...


class RecognitionV2Service(RecognitionGateMixin):
    """Isolated recognition facts: provider call, raw preservation, normalization."""

    def __init__(self, transport: RecognitionV2Transport, *, enrichment_service: Any | None = None) -> None:
        self._transport = transport
        self._enrichment_service = enrichment_service

    @classmethod
    def from_settings(cls, settings: Any) -> "RecognitionV2Service":
        return cls(LiangpiaoV2Transport(settings))

    async def recognize(
        self,
        image_url: str,
        *,
        trace_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RecognitionResult:
        provider = await self._transport.recognize(
            image_url,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )
        result = _normalize(
            provider.data,
            raw_provider_result=provider.raw_provider_result,
            has_manual_mark=None,
        )
        enrich = getattr(self._enrichment_service, "enrich", None)
        if callable(enrich):
            enriched = await enrich(result)
            if isinstance(enriched, RecognitionResult):
                result = enriched
        return result

    async def aclose(self) -> None:
        await self._transport.aclose()


def _normalize(
    provider_data: Mapping[str, Any],
    *,
    raw_provider_result: Mapping[str, Any],
    has_manual_mark: bool | None,
) -> RecognitionResult:
    raw = provider_data.get("rawResults")
    raw_results = raw if isinstance(raw, Mapping) else {}
    seats_raw = raw_results.get("seat")
    has_selected_seats = isinstance(seats_raw, list) and len(seats_raw) > 0
    selected_seats = [
        seat_name
        for item in seats_raw or []
        if isinstance(item, Mapping)
        for seat_name in [_text(item.get("seatName"))]
        if seat_name is not None
    ]
    show_date, start_time = _split_showtime(_text(raw_results.get("showtime")))
    return RecognitionResult(
        provider_recognize_id=_text(provider_data.get("recognizeId")),
        platform_text=_text(raw_results.get("platform")),
        city_text=_text(raw_results.get("city")),
        cinema_text=_text(raw_results.get("cinema")),
        cinema_truncated=_bool(raw_results.get("cinemaTruncated"), default=False),
        movie=_text(raw_results.get("film") or raw_results.get("movie")),
        show_date=show_date,
        start_time=start_time,
        hall=_text(raw_results.get("hall")),
        language=_text(raw_results.get("language")),
        dimension=_text(raw_results.get("dimension") or raw_results.get("format")),
        selected_seats=selected_seats,
        has_selected_seats=has_selected_seats,
        image_total_price_fen=_price_fen(raw_results),
        confidence=_confidence(raw_results.get("confidence")),
        has_manual_mark=has_manual_mark,
        raw_provider_result=dict(raw_provider_result),
    )


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return default


def _split_showtime(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = _SHOWTIME_PATTERN.search(value)
    if match:
        year, month, day = (int(part) for part in match.group("date").split("-"))
        if 1 <= month <= 12 and 1 <= day <= 31:
            hour = int(match.group("time").split(":", 1)[0])
            return f"{year:04d}-{month:02d}-{day:02d}", f"{hour:02d}:{match.group('time').split(':', 1)[1]}"
        return None, None
    match = _TIME_PATTERN.search(value)
    if not match:
        return None, None
    return None, f"{int(match.group('hour')):02d}:{match.group('minute')}"


def _price_fen(raw_results: Mapping[str, Any]) -> int | None:
    fen = raw_results.get("priceAllFen")
    if fen is not None:
        try:
            parsed = int(str(fen).strip())
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            pass
    value = raw_results.get("priceAll")
    if value is None:
        return None
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or amount < 0:
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        return None


def _confidence(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 1 else None
