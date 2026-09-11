from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .liangpiao import LiangpiaoRecognitionResponse, LiangpiaoV2Transport
from .models import RecognitionResult


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


class RecognitionV2Service:
    """Isolated recognition facts: provider call, raw preservation, normalization."""

    def __init__(
        self,
        transport: RecognitionV2Transport,
        *,
        enrichment_service: Any | None = None,
        screenshot_price_detector: Any | None = None,
    ) -> None:
        self._transport = transport
        self._enrichment_service = enrichment_service
        self._screenshot_price_detector = screenshot_price_detector

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
        detect = getattr(self._screenshot_price_detector, "detect_wplus_member_total_from_url", None)
        if result.selected_seats and callable(detect):
            try:
                screenshot_facts = await detect(image_url)
            except Exception:
                # The auxiliary OCR pass must never block the authoritative
                # recognition path or turn an ordinary screenshot into a
                # quote failure.
                screenshot_facts = None
            result = apply_screenshot_wplus_facts(result, screenshot_facts)
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
    quality = _quality_facts(provider_data.get("finalResults"))
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
        seat_matched=quality["seat_matched"],
        price_mismatch=quality["price_mismatch"],
        seat_confirm_required=quality["seat_confirm_required"],
        seat_confirm_reasons=quality["seat_confirm_reasons"],
        seat_set_verified=quality["seat_set_verified"],
        has_manual_mark=has_manual_mark,
        raw_provider_result=dict(raw_provider_result),
    )


def apply_screenshot_wplus_facts(
    recognition: RecognitionResult,
    facts: Mapping[str, Any] | None,
) -> RecognitionResult:
    """Attach an explicit bottom W+ total only when seat facts agree."""
    if not isinstance(recognition, RecognitionResult) or not isinstance(facts, Mapping):
        return recognition
    total = _positive_int(facts.get("total_price_fen"))
    raw_ticket_count = facts.get("ticket_count")
    explicit_ticket_count = _positive_int(raw_ticket_count)
    visible_count = len(recognition.selected_seats)
    ticket_count = explicit_ticket_count if raw_ticket_count is not None else visible_count
    if (
        total is None or visible_count < 1 or ticket_count is None
        or ticket_count != visible_count or not 1 <= ticket_count <= 20
    ):
        return recognition.model_copy(update={
            "screenshot_wplus_total_price_fen": None,
            "screenshot_wplus_ticket_count": None,
        })
    return recognition.model_copy(update={
        "screenshot_wplus_total_price_fen": total,
        "screenshot_wplus_ticket_count": ticket_count,
    })


def _quality_facts(value: Any) -> dict[str, Any]:
    """Normalize provider recognition quality without promoting provider IDs.

    ``has_selected_seats`` above is intentionally independent: a non-empty
    seat list means the image contained seat labels, while ``seat_set_verified``
    requires the provider's complete/exact confirmation and no confirmation
    blocker.  Neither fact says anything about current Wanda availability.
    """
    final = value if isinstance(value, Mapping) else {}
    reasons_raw = final.get("seatConfirmReasons")
    reasons = [
        str(item).strip().upper()
        for item in reasons_raw
        if str(item).strip()
    ] if isinstance(reasons_raw, list) else []
    price_mismatch = _optional_bool(final.get("priceMismatch")) is True
    price_mismatch = price_mismatch or "PRICE_MISMATCH" in reasons
    seat_matched = _optional_bool(final.get("seatMatched"))
    seat_confirm_required = _bool(final.get("seatConfirmRequired"), default=False)
    match_level = _text(final.get("matchLevel"))
    return {
        "seat_matched": seat_matched,
        "price_mismatch": price_mismatch,
        "seat_confirm_required": seat_confirm_required,
        "seat_confirm_reasons": reasons,
        "seat_set_verified": (
            seat_matched is True
            and match_level is not None
            and match_level.upper() == "EXACT"
            and not price_mismatch
            and not seat_confirm_required
        ),
    }


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _bool(value: Any, *, default: bool) -> bool:
    parsed = _optional_bool(value)
    return parsed if parsed is not None else default


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


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _confidence(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 1 else None
