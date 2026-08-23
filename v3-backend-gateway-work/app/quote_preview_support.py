from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, HTTPException

from .schemas import QuoteRealtimeResponse, Recognition, VisionRecognizeRequest
from .vision import VisionFailure, VisionService

SCREENSHOT_PRICE_CONFIDENCE_THRESHOLD = 0.85
PREVIEW_VISION_RETRY_DELAY_SECONDS = 0.35


def _explicit_yuan_cents(value: object) -> int | None:
    try:
        cents = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        return None
    if cents != cents.to_integral_value():
        return None
    result = int(cents)
    return result if result > 0 else None


def _buyer_app_has_lower_price(quote: QuoteRealtimeResponse, recognition: Recognition) -> bool:
    """Use screenshot prices only to suppress a worse offer, never to price ours."""
    if recognition.confidence.price < SCREENSHOT_PRICE_CONFIDENCE_THRESHOLD:
        return False
    count = quote.ticket_count
    quoted_total = quote.total_quote_cents
    if not count or not quoted_total:
        return False

    comparable_totals: list[int] = []
    selected = recognition.official_selection
    if selected.is_selected and selected.selected_count == count:
        explicit_total = _explicit_yuan_cents(selected.total_price)
        if explicit_total:
            comparable_totals.append(explicit_total)
        elif len(selected.seats) == count:
            seat_prices = [_explicit_yuan_cents(seat.price) for seat in selected.seats]
            if all(price is not None for price in seat_prices):
                comparable_totals.append(sum(price for price in seat_prices if price is not None))

    # Without an official selected-seat total, compare only the same explicit
    # seat zone. A cheaper unrelated zone must not suppress the verified quote.
    if not comparable_totals:
        quote_zones = {seat.seat_zone_type for seat in quote.seat_quotes}
        if not quote_zones:
            quote_zones = {quote.seat_zone_type}
        if len(quote_zones) == 1:
            quote_zone = next(iter(quote_zones))
            unit_prices = [
                cents
                for item in recognition.visible_prices
                if item.zone_type == quote_zone
                if (cents := _explicit_yuan_cents(item.price_yuan)) is not None
            ]
            if unit_prices:
                comparable_totals.append(min(unit_prices) * count)

    return bool(comparable_totals) and quoted_total > min(comparable_totals)


def _recognition_needs_seat_or_count_confirmation(recognition: Recognition, requested_ticket_count: int | None) -> bool:
    # A hand-drawn circle is an artificial-delivery preference, never evidence
    # of quantity. Only an official selected-seat card or explicit buyer text
    # can satisfy the ticket-count requirement.
    return recognition.official_selection.selected_count <= 0 and not requested_ticket_count


def _preview_failure_code(error: Exception) -> str:
    """Return a stable, non-sensitive code for preview diagnostics."""
    if isinstance(error, VisionFailure):
        return error.code
    if isinstance(error, HTTPException):
        detail = str(error.detail)
        if re.fullmatch(r"(?:ai_vision|image)_[a-z0-9_]+", detail):
            return detail
        match = re.fullmatch(r"模型服务返回 HTTP (\d{3})", str(error.detail))
        if match:
            return f"model_http_{match.group(1)}"
        return f"http_{error.status_code}"
    exception_name = re.sub(r"[^a-z0-9]+", "_", type(error).__name__.lower()).strip("_")
    return f"unexpected_{exception_name or 'error'}"


async def _recognize_preview_image(app: FastAPI, request: VisionRecognizeRequest):
    """Retry one transient image-read validation failure before marking a quote preview failed."""
    for attempt in range(2):
        try:
            service = app.state.vision_service
            if isinstance(service, VisionService):
                return await service.recognize(request, app.state.settings_store.read(), app.state.knowledge_base_store.active("vision"))
            return await service.recognize(request, app.state.settings_store.read())
        except HTTPException as error:
            # Older FastAPI/Starlette releases used by the production service
            # do not expose HTTP_422_UNPROCESSABLE_CONTENT. Compare the HTTP
            # status value directly so a valid image-validation failure is
            # retried instead of being turned into an AttributeError/HTTP 500.
            if error.status_code != 422 or attempt == 1:
                raise
            await asyncio.sleep(PREVIEW_VISION_RETRY_DELAY_SECONDS)
    raise AssertionError("preview vision retry loop must return or raise")
