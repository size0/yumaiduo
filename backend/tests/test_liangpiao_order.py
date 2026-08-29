from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.liangpiao_order_service import LiangpiaoOrderRequest, LiangpiaoOrderService, OrderServiceError
from app.selected_seat_quote_service import SelectedSeatQuoteResult


class FakeOrderClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def order_create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {"providerOrderNo": "provider-1", "status": "created"}

    async def order_detail(self, **_: object) -> dict[str, object]:
        return {}


def quote() -> SelectedSeatQuoteResult:
    return SelectedSeatQuoteResult(
        quote_id="quote-1", quote_hash="a" * 64, show_id="show-1",
        seats=[{"row_no": 5, "col_no": 8, "seat_no": "5排8座", "area_id": "A"}],
        provider_amount_fen=8000, buyer_amount_fen=8800, pricing_rule_version="rule-1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), generation=1, trace_id="trace",
    )


def order_request(**overrides: object) -> LiangpiaoOrderRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant", "conversation_id": "chat", "confirmation_id": "confirm-1",
        "quote_id": "quote-1", "quote_hash": "a" * 64, "latest_buyer_message": "确认下单",
        "buyer_phone": "13800138000", "generation": 1, "trace_id": "trace", "buyer_confirmed": True,
    }
    values.update(overrides)
    return LiangpiaoOrderRequest.model_validate(values)


@pytest.mark.asyncio
async def test_order_requires_both_write_fuses() -> None:
    service = LiangpiaoOrderService(FakeOrderClient(), order_create_enabled=False, external_writes_enabled=False)
    with pytest.raises(OrderServiceError) as error:
        await service.create(order_request(), quote())
    assert error.value.code == "LIANGPIAO_ORDER_CREATE_DISABLED"


@pytest.mark.asyncio
async def test_order_is_idempotent_and_uses_quote_snapshot_values() -> None:
    client = FakeOrderClient()
    service = LiangpiaoOrderService(client, order_create_enabled=True, external_writes_enabled=True)
    first = await service.create(order_request(), quote())
    second = await service.create(order_request(), quote())
    assert first.out_order_no == second.out_order_no
    assert len(client.calls) == 1
    assert client.calls[0]["maxPrice"] == 8800
    assert client.calls[0]["allowSeatChange"] is False


@pytest.mark.asyncio
async def test_order_idempotency_recovers_from_persisted_snapshot() -> None:
    client = FakeOrderClient()
    stored: dict[str, object] = {}

    class Store:
        def save_liangpiao_order(self, snapshot: dict[str, object]) -> None:
            stored.update(snapshot)

        def find_liangpiao_order(self, *, out_order_no: str | None = None, provider_order_no: str | None = None):
            return stored if out_order_no and stored.get("out_order_no") == out_order_no else None

    first = LiangpiaoOrderService(client, order_store=Store(), order_create_enabled=True, external_writes_enabled=True)
    created = await first.create(order_request(), quote())
    second = LiangpiaoOrderService(client, order_store=Store(), order_create_enabled=True, external_writes_enabled=True)
    recovered = await second.create(order_request(), quote())
    assert recovered.out_order_no == created.out_order_no
    assert recovered.provider_order_no == created.provider_order_no
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_order_rejects_quote_hash_generation_and_confirmation_mismatch() -> None:
    service = LiangpiaoOrderService(FakeOrderClient(), order_create_enabled=True, external_writes_enabled=True)
    with pytest.raises(OrderServiceError) as error:
        await service.create(order_request(quote_hash="b" * 64), quote())
    assert error.value.code == "LIANGPIAO_QUOTE_HASH_MISMATCH"
    with pytest.raises(OrderServiceError) as error:
        await service.create(order_request(generation=2), quote())
    assert error.value.code == "LIANGPIAO_QUOTE_GENERATION_MISMATCH"
    with pytest.raises(OrderServiceError) as error:
        await service.create(order_request(latest_buyer_message="我再看看"), quote())
    assert error.value.code == "LIANGPIAO_CONFIRMATION_REQUIRED"
