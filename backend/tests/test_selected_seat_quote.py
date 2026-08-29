from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.selected_seat_quote_service import QuoteServiceError, SelectedSeatQuoteRequest, SelectedSeatQuoteService
from app.rule_contracts import ReplyPlan
from app.rule_templates import validate_reply_plan


class FakeClient:
    async def show_list(self, **_: object) -> dict[str, object]:
        return {"items": [{"showId": "show-1", "movieName": "电影", "showDate": "2026-08-29", "startTime": "20:00"}]}

    async def seat_list(self, **_: object) -> dict[str, object]:
        return {"items": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A", "status": "AVAILABLE"}]}

    async def order_preflight(self, **_: object) -> dict[str, object]:
        return {"providerAmountFen": 8000, "buyerAmountFen": 8800, "pricingRuleVersion": "rule-7", "ok": True}


def request(**overrides: object) -> SelectedSeatQuoteRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant", "conversation_id": "chat", "cinema_id": 1,
        "movie_name": "电影", "show_date": "2026-08-29", "showtime_start": "20:00",
        "seats": [{"row_no": 5, "col_no": 8, "seat_no": "5排8座", "area_id": "A"}],
    }
    values.update(overrides)
    return SelectedSeatQuoteRequest.model_validate(values)


@pytest.mark.asyncio
async def test_selected_seat_quote_resolves_show_matches_seat_and_snapshots() -> None:
    result = await SelectedSeatQuoteService(FakeClient()).quote(request())
    assert result.show_id == "show-1"
    assert result.buyer_amount_fen == 8800
    assert result.pricing_rule_version == "rule-7"
    assert len(result.quote_hash) == 64
    assert result.expires_at > datetime.now(timezone.utc)
    assert result.reply_plan["template_key"] == "flow.quote.ready"
    assert validate_reply_plan(ReplyPlan.model_validate(result.reply_plan), state="QUOTED")


@pytest.mark.asyncio
async def test_selected_seat_quote_rejects_unavailable_seat_without_downgrade() -> None:
    class Unavailable(FakeClient):
        async def seat_list(self, **_: object) -> dict[str, object]:
            return {"items": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A", "status": "SOLD"}]}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(Unavailable()).quote(request(show_id="show-1"))
    assert error.value.code == "LIANGPIAO_SEAT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_selected_seat_quote_rejects_ambiguous_show_and_invalid_amount() -> None:
    class Ambiguous(FakeClient):
        async def show_list(self, **_: object) -> dict[str, object]:
            return {"items": [{"showId": "a"}, {"showId": "b"}]}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(Ambiguous()).quote(request(show_id=None, movie_name=None, show_date=None, showtime_start=None))
    assert error.value.code == "LIANGPIAO_SHOW_AMBIGUOUS"

    class InvalidAmount(FakeClient):
        async def order_preflight(self, **_: object) -> dict[str, object]:
            return {"providerAmountFen": 0, "buyerAmountFen": 8800, "ok": True}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(InvalidAmount()).quote(request(show_id="show-1"))
    assert error.value.code == "LIANGPIAO_PREFLIGHT_INVALID"
