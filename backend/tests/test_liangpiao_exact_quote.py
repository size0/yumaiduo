from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.liangpiao_exact_quote import LiangpiaoExactQuoteAdapter
from app.models import MovieImageInfo
from app.selected_seat_quote_service import SelectedSeatQuoteResult


class FakeSelectedSeatService:
    def __init__(self) -> None:
        self.request = None

    async def quote(self, request):
        self.request = request
        return SelectedSeatQuoteResult(
            quote_id="lpq-1", quote_hash="a" * 64, show_id="show-1",
            seats=request.seats, provider_amount_fen=5000, buyer_amount_fen=5200,
            pricing_rule_version="liangpiao-r1",
            expires_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
            generation=request.generation, trace_id=request.trace_id,
        )


def recognition() -> MovieImageInfo:
    return MovieImageInfo(
        cinema_id=41, cinema_name="CGV影城（测试店）", brand_name="CGV影城",
        city="深圳", city_code="440300", movie_name="测试片",
        date="2026-08-30", show_id="show-1", showtime_start="17:00",
        selected_seats=[
            {"seat_number": "5排6座", "row_no": 5, "col_no": 6},
            {"seat_number": "5排7座", "row_no": 5, "col_no": 7},
        ], selected_count_visible=2,
    )


@pytest.mark.asyncio
async def test_non_wanda_exact_quote_uses_liangpiao_preflight() -> None:
    service = FakeSelectedSeatService()
    adapter = LiangpiaoExactQuoteAdapter(service, price_mode_provider=lambda: "LIMIT")

    quote = await adapter.quote(recognition(), tenant_id="tenant-1", conversation_id="chat-1")

    assert service.request.tenant_id == "tenant-1"
    assert service.request.conversation_id == "chat-1"
    assert service.request.cinema_id == 41
    assert service.request.show_id == "show-1"
    assert service.request.price_mode == "LIMIT"
    assert [(seat.row_no, seat.col_no) for seat in service.request.seats] == [(5, 6), (5, 7)]
    assert quote.quote_scope == "exact_seats"
    assert quote.total_quote_cents == 5200
    assert quote.unit_quote_cents == 2600
    assert quote.price_source == "liangpiao_realtime_preflight"
    assert quote.pricing_rule_version == "liangpiao-r1"


@pytest.mark.asyncio
async def test_non_wanda_quote_requires_explicit_seats() -> None:
    service = FakeSelectedSeatService()
    adapter = LiangpiaoExactQuoteAdapter(service)
    request = recognition().model_copy(update={"selected_seats": [], "selected_count_visible": 0})

    with pytest.raises(Exception, match="明确座位"):
        await adapter.quote(request, tenant_id="tenant-1", conversation_id="chat-1")
