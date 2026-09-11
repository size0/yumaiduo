from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.recognition_v2.models import RecognitionResult
from app.recognition_v2.service import apply_screenshot_wplus_facts
from app.quote_v2.service import _screenshot_wplus_pricing
from app.seat_facts_v2.models import SeatFactsResult
from app.service import MovieImageRecognitionService
from app.wplus_screenshot_price import quote_from_screenshot_wplus_total


def test_bottom_wplus_total_is_rounded_up_to_whole_yuan() -> None:
    result = quote_from_screenshot_wplus_total(9_932, ticket_count=2)

    assert result == {"total_sell_price_fen": 10_000, "unit_sell_price_fen": 5_000}


def test_bottom_wplus_total_respects_the_configured_floor() -> None:
    result = quote_from_screenshot_wplus_total(
        6_800, ticket_count=2, minimum_unit_price_fen=3_590,
    )

    assert result == {"total_sell_price_fen": 7_180, "unit_sell_price_fen": 3_590}


@pytest.mark.asyncio
async def test_detector_reads_only_explicit_bottom_wplus_total() -> None:
    service = MovieImageRecognitionService(
        Settings(api_key="key", chat_api_key="chat"),
    )
    service._download_image_url = AsyncMock(return_value=(b"image", "image/png"))  # type: ignore[method-assign]
    service._request_completion = AsyncMock(return_value=json.dumps({
        "present": True, "total_price_yuan": 99.32, "ticket_count": None,
    }))  # type: ignore[method-assign]

    facts = await service.detect_wplus_member_total_from_url("https://img.alicdn.com/test.png")

    assert facts == {"total_price_fen": 9_932}


def test_canonical_pricing_uses_bottom_wplus_total_after_seat_verification() -> None:
    recognition = RecognitionResult(
        selected_seats=["1排13座", "1排12座"],
        screenshot_wplus_total_price_fen=9_932,
        screenshot_wplus_ticket_count=2,
    )
    seat_facts = SeatFactsResult(
        status="EXACT_SEATS_RESOLVED", seat_request_type="EXACT_SEATS",
        exact_seats=[],
    )

    pricing = _screenshot_wplus_pricing(recognition, seat_facts)

    assert pricing is not None
    assert pricing.total_sell_price_fen == 10_000
    assert pricing.unit_sell_price_fen == 5_000
    assert pricing.price_source == "screenshot_bottom_wplus"


def test_screenshot_wplus_facts_are_attached_only_when_seat_count_matches() -> None:
    recognition = RecognitionResult(selected_seats=["1排13座", "1排12座"])

    accepted = apply_screenshot_wplus_facts(
        recognition, {"total_price_fen": 9_932, "ticket_count": 2},
    )
    accepted_without_printed_count = apply_screenshot_wplus_facts(
        recognition, {"total_price_fen": 9_932},
    )
    rejected = apply_screenshot_wplus_facts(
        recognition, {"total_price_fen": 9_932, "ticket_count": 1},
    )

    assert accepted.screenshot_wplus_total_price_fen == 9_932
    assert accepted.screenshot_wplus_ticket_count == 2
    assert accepted_without_printed_count.screenshot_wplus_total_price_fen == 9_932
    assert accepted_without_printed_count.screenshot_wplus_ticket_count == 2
    assert rejected.screenshot_wplus_total_price_fen is None
    assert rejected.screenshot_wplus_ticket_count is None
