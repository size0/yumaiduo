from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ProviderError
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


class TimeoutThenIdempotentOrderClient(FakeOrderClient):
    async def order_create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        if len(self.calls) == 1:
            raise TimeoutError("provider response lost")
        return {"orderNo": "LP-provider-1", "status": "SUBMITTING", "duplicated": True}


class BusinessRejectOrderClient(FakeOrderClient):
    async def order_create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        raise ProviderError("liangpiao_business_300004", "没有可用供应商")


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
async def test_order_uses_authoritative_confirmation_flag_not_message_words() -> None:
    client = FakeOrderClient()
    service = LiangpiaoOrderService(client, order_create_enabled=True, external_writes_enabled=True)

    created = await service.create(
        order_request(latest_buyer_message="好的"), quote(),
    )

    assert created.status == "ok"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_limit_order_uses_provider_upper_limit_not_buyer_estimate() -> None:
    client = FakeOrderClient()
    service = LiangpiaoOrderService(client, order_create_enabled=True, external_writes_enabled=True)
    limit_quote = quote().model_copy(update={"price_mode": "LIMIT", "max_price_fen": 9500})

    await service.create(order_request(), limit_quote)

    assert client.calls[0]["priceMode"] == "LIMIT"
    assert client.calls[0]["maxPrice"] == 9500


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
async def test_order_timeout_replays_same_documented_idempotent_create_payload() -> None:
    client = TimeoutThenIdempotentOrderClient()
    service = LiangpiaoOrderService(
        client, order_create_enabled=True, external_writes_enabled=True,
    )

    created = await service.create(order_request(), quote())

    assert created.provider_order_no == "LP-provider-1"
    assert len(client.calls) == 2
    assert client.calls[0] == client.calls[1]
    assert client.calls[0]["outOrderNo"] == created.out_order_no


@pytest.mark.asyncio
async def test_order_business_rejection_is_definitive_and_not_retried() -> None:
    client = BusinessRejectOrderClient()
    service = LiangpiaoOrderService(
        client, order_create_enabled=True, external_writes_enabled=True,
    )

    with pytest.raises(OrderServiceError) as caught:
        await service.create(order_request(), quote())

    assert caught.value.code == "LIANGPIAO_ORDER_CREATE_REJECTED"
    assert caught.value.message == "没有可用供应商"
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
        await service.create(order_request(latest_buyer_message="我再看看", buyer_confirmed=False), quote())
    assert error.value.code == "LIANGPIAO_CONFIRMATION_REQUIRED"
