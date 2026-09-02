from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.selected_seat_quote_service import QuoteServiceError, SelectedSeatQuoteRequest, SelectedSeatQuoteService
from app.rule_contracts import ReplyPlan
from app.rule_templates import validate_reply_plan


class FakeClient:
    async def show_list(self, **_: object) -> dict[str, object]:
        return {"list": [{
            "showId": "show-1", "cinemaId": 1, "movieName": "电影",
            "hallName": "5号厅", "startTime": "2026-08-29T20:00:00+08:00",
            "endTime": "2026-08-29T22:00:00+08:00",
        }]}

    async def seat_list(self, **_: object) -> dict[str, object]:
        return {"items": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A", "status": "AVAILABLE"}]}

    async def order_preflight(self, **_: object) -> dict[str, object]:
        return {
            "available": True, "totalAmount": "8800", "estimateAmount": "8800",
            "marketAmount": "8000", "estimated": False, "pricingRuleVersion": "rule-7",
        }


def request(**overrides: object) -> SelectedSeatQuoteRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant", "conversation_id": "chat", "cinema_id": 1,
        "movie_name": "电影", "show_date": "2026-08-29", "showtime_start": "20:00",
        "seats": [{"row_no": 5, "col_no": 8, "seat_no": "5排8座", "area_id": "A"}],
    }
    values.update(overrides)
    return SelectedSeatQuoteRequest.model_validate(values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"seats": [
            {"row_no": 5, "col_no": index, "seat_no": f"5排{index}座"}
            for index in range(1, 8)
        ]},
        {"ticket_mode": "SLOW"},
        {"price_mode": "AUTO"},
    ],
)
def test_selected_seat_quote_request_rejects_values_outside_provider_contract(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


@pytest.mark.asyncio
async def test_selected_seat_quote_uses_only_supported_show_list_filters() -> None:
    class ContractClient(FakeClient):
        def __init__(self) -> None:
            self.show_kwargs: dict[str, object] | None = None
            self.seat_kwargs: dict[str, object] | None = None
            self.preflight_kwargs: dict[str, object] | None = None

        async def show_list(self, **kwargs: object) -> dict[str, object]:
            self.show_kwargs = kwargs
            return await super().show_list(**kwargs)

        async def seat_list(self, **kwargs: object) -> dict[str, object]:
            self.seat_kwargs = kwargs
            return await super().seat_list(**kwargs)

        async def order_preflight(self, **kwargs: object) -> dict[str, object]:
            self.preflight_kwargs = kwargs
            return await super().order_preflight(**kwargs)

    client = ContractClient()
    result = await SelectedSeatQuoteService(client).quote(request())

    assert result.show_id == "show-1"
    assert client.show_kwargs == {"cinema_id": 1, "show_date": "2026-08-29"}
    assert client.seat_kwargs == {"show_id": "show-1"}
    assert client.preflight_kwargs is not None
    assert client.preflight_kwargs["show_id"] == "show-1"
    assert "cinema_id" not in client.preflight_kwargs
    assert "cinemaId" not in client.preflight_kwargs
    assert "generation" not in client.preflight_kwargs
    assert "trace_id" not in client.preflight_kwargs


@pytest.mark.asyncio
async def test_area_quote_strategy_is_forwarded_and_snapshotted() -> None:
    client = FakeClient()
    captured: dict[str, object] = {}

    async def preflight(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return await FakeClient.order_preflight(client, **kwargs)

    client.order_preflight = preflight  # type: ignore[method-assign]
    result = await SelectedSeatQuoteService(client).quote(
        request(area_quote_strategy="HIGHEST"),
    )

    assert captured["area_quote_strategy"] == "HIGHEST"
    assert result.snapshot["preflight_request"]["area_quote_strategy"] == "HIGHEST"


@pytest.mark.asyncio
async def test_selected_seat_quote_resolves_show_matches_seat_and_snapshots() -> None:
    result = await SelectedSeatQuoteService(FakeClient()).quote(request())
    assert result.show_id == "show-1"
    assert result.provider_amount_fen == 8800
    assert result.buyer_amount_fen == 8800
    assert result.pricing_rule_version == "rule-7"
    assert len(result.quote_hash) == 64
    assert result.expires_at > datetime.now(timezone.utc)
    assert result.reply_plan["template_key"] == "flow.quote.ready"
    assert validate_reply_plan(ReplyPlan.model_validate(result.reply_plan), state="QUOTED")


@pytest.mark.asyncio
async def test_provider_column_seat_label_matches_recognized_seat_number() -> None:
    class ColumnLabelClient(FakeClient):
        async def seat_list(self, **_: object) -> dict[str, object]:
            return {"items": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8列", "areaId": "A", "status": "AVAILABLE"}]}

    result = await SelectedSeatQuoteService(ColumnLabelClient()).quote(request())

    assert result.seats[0].row_no == 5
    assert result.seats[0].col_no == 8


@pytest.mark.asyncio
async def test_limit_quote_applies_enabled_liangpiao_operator_rule() -> None:
    from app.models import PricingRulesUpdate

    class LimitClient(FakeClient):
        async def order_preflight(self, **_: object) -> dict[str, object]:
            return {
                "available": True, "totalAmount": "9500", "estimateAmount": "8800",
                "marketAmount": "10000", "estimated": True, "pricingRuleVersion": "rule-8",
            }

    rules = PricingRulesUpdate(
        enabled=True,
        liangpiao_rules=[{"min_discount_percent": 0, "max_discount_percent": 100, "markup_percent": 10}],
    )
    result = await SelectedSeatQuoteService(LimitClient(), pricing_rules=lambda: rules).quote(request(price_mode="LIMIT"))

    assert result.provider_amount_fen == 8800
    assert result.buyer_amount_fen == 9680
    assert result.max_price_fen == 9500
    assert result.operator_pricing_applied is True


@pytest.mark.asyncio
async def test_limit_quote_reports_estimate_and_keeps_provider_upper_limit() -> None:
    class LimitClient(FakeClient):
        async def order_preflight(self, **_: object) -> dict[str, object]:
            return {
                "available": True, "totalAmount": "9500", "estimateAmount": "8800",
                "marketAmount": "8000", "estimated": True, "pricingRuleVersion": "rule-8",
            }

    result = await SelectedSeatQuoteService(LimitClient()).quote(request(price_mode="LIMIT"))

    assert result.buyer_amount_fen == 8800
    assert result.max_price_fen == 9500
    assert result.snapshot["max_price_fen"] == 9500


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
            return {"items": [
                {
                    "showId": "a", "showDate": "2026-08-29",
                    "startTime": "20:00",
                },
                {
                    "showId": "b", "showDate": "2026-08-29",
                    "startTime": "20:00",
                },
            ]}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(Ambiguous()).quote(
            request(show_id=None, movie_name=None, show_date="2026-08-29", showtime_start=None),
        )
    assert error.value.code == "LIANGPIAO_SHOW_AMBIGUOUS"

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(Ambiguous()).quote(
            request(show_id=None, movie_name=None, show_date=None, showtime_start=None),
        )
    assert error.value.code == "LIANGPIAO_SHOW_DATE_MISSING"

    class InvalidAmount(FakeClient):
        async def order_preflight(self, **_: object) -> dict[str, object]:
            return {"available": True, "totalAmount": "0", "estimated": False}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(InvalidAmount()).quote(request(show_id="show-1"))
    assert error.value.code == "LIANGPIAO_PREFLIGHT_INVALID"

    class Unavailable(FakeClient):
        async def order_preflight(self, **_: object) -> dict[str, object]:
            return {"available": False, "reason": "所选座位不可用"}

    with pytest.raises(QuoteServiceError) as error:
        await SelectedSeatQuoteService(Unavailable()).quote(request(show_id="show-1"))
    assert error.value.code == "LIANGPIAO_PREFLIGHT_FAILED"
