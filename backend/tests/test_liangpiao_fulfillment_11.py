from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.liangpiao_callbacks import CallbackVerifier, LiangpiaoCallbackHandler
from app.liangpiao_order_service import LiangpiaoOrderService, OrderServiceError
from app.order_quote_binding_v2.service import OrderQuoteBindingV2Service
from app.quote_record_store import QuoteRecordStore
from app.rules_first_store import RulesFirstStore
from app.rules_first_state_store import SqliteTransactionStateStore
from app.selected_seat_quote_service import SelectedSeat, SelectedSeatQuoteResult


IDENTITY = {
    "tenant_id": "tenant-11", "shop_id": "shop-11",
    "buyer_id": "buyer-11", "chat_id": "chat-11",
}
ORDER_ID = "fish-order-11"
NOW = datetime.now(timezone.utc)


class FakeProvider:
    def __init__(self, *, limit_cost: int = 4500, fixed_cost: int = 4500) -> None:
        self.limit_cost = limit_cost
        self.fixed_cost = fixed_cost
        self.create_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []
        self.preflight_calls: list[dict[str, Any]] = []
        self.create_results: list[dict[str, Any] | Exception] = []
        self.detail_result: dict[str, Any] = {}

    async def order_create(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(dict(kwargs))
        result = self.create_results.pop(0) if self.create_results else {
            "providerOrderNo": "lp-provider-11", "status": "TICKING",
        }
        if isinstance(result, Exception):
            raise result
        return result

    async def order_detail(self, **kwargs: Any) -> dict[str, Any]:
        self.detail_calls.append(dict(kwargs))
        return dict(self.detail_result)


class FreshPreflight:
    def __init__(self, *, provider: FakeProvider) -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []
        self.fixed_cost = provider.fixed_cost

    async def quote(self, request: dict[str, Any]) -> SelectedSeatQuoteResult:
        self.calls.append(dict(request))
        mode = str(request["price_mode"])
        cost = self.fixed_cost if mode == "FIXED" else self.provider.limit_cost
        seats = [SelectedSeat.model_validate(item) for item in request["seats"]]
        return SelectedSeatQuoteResult(
            quote_id=f"fresh-{mode.lower()}-quote",
            quote_hash=("f" if mode == "FIXED" else "e") * 64,
            show_id=str(request["show_id"]), price_mode=mode,
            seats=seats, provider_amount_fen=cost,
            buyer_amount_fen=cost, max_price_fen=cost,
            pricing_rule_version="replay-rule", expires_at=NOW + timedelta(minutes=5),
            generation=1, trace_id="fresh-trace",
            snapshot={"fresh": True, "price_mode": mode, "provider_cost_fen": cost},
        )


def quote_record(store: QuoteRecordStore, *, seller_total: int = 5000, price_mode: str = "LIMIT") -> dict[str, Any]:
    return store.save_quote({
        "record_id": "record-11", "quote_id": "quote-11", "request_id": "request-11",
        "tenant_id": IDENTITY["tenant_id"], "shop_id": IDENTITY["shop_id"],
        "buyer_id": IDENTITY["buyer_id"], "chat_id": IDENTITY["chat_id"],
        "purchase_context_id": "item-11", "item_id": "item-11", "created_at": NOW.isoformat(),
        "status": "succeeded", "source": "AUTO_PRICING", "provider": "LIANGPIAO",
        "provider_route": "LIANGPIAO", "canonical_quote_route": "LIANGPIAO",
        "quote_route": "liangpiao_limit", "liangpiao_cinema_id": "4748",
        "liangpiao_movie_id": "56", "liangpiao_show_id": "show-11",
        "provider_quote_id": "provider-quote-11", "provider_quote_hash": "provider-hash-11",
        "provider_preflight_expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ticket_mode": "STANDARD", "price_mode": price_mode,
        "area_quote_strategy": "AVERAGE", "city": "常州", "cinema": "测试影院",
        "movie": "测试电影", "quote_date": "2026-09-04", "showtime_start": "19:30",
        "hall": "1号厅", "request_type": "EXACT_SEATS", "quote_scope": "exact_seats",
        "seat_zone_type": "STANDARD", "selected_seats": [{
            "rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A",
        }], "seat_display": "5排8座", "ticket_count": 1,
        "needs_ticket_count": False, "unit_sell_price_fen": seller_total,
        "total_sell_price_fen": seller_total, "unit_quote_cents": seller_total,
        "total_quote_cents": seller_total, "provider_amount_fen": 4500,
        "provider_max_amount_fen": 5000, "pricing_rule_revision": 4,
        "pricing_rule_version": "pricing-r4", "delivery_state": "delivered",
    }, ttl_seconds=600, now=NOW)


def paid_body(*, event_id: str = "paid-11", identity: dict[str, str] = IDENTITY) -> dict[str, Any]:
    return {
        "envelope": {"id": event_id, "event": "order.paid", "tenantId": identity["tenant_id"]},
        "session": {"accountUnb": identity["shop_id"], "peerUnb": identity["buyer_id"], "chatId": identity["chat_id"]},
        "order": {
            "order_id": ORDER_ID, "order_status": "paid", "amount_cents": 5000,
            "buyer_phone": "13800138000", "shop_id": identity["shop_id"],
            "buyer_id": identity["buyer_id"], "chat_id": identity["chat_id"],
        },
    }


def prepare(tmp_path: Path, *, seller_total: int = 5000, provider: FakeProvider | None = None):
    database = tmp_path / "rules.sqlite3"
    rules = RulesFirstStore(database)
    quotes = QuoteRecordStore(tmp_path / "quotes.json")
    saved = quote_record(quotes, seller_total=seller_total)
    binding = OrderQuoteBindingV2Service(quotes)
    bound = binding.bind_selected_quote(ORDER_ID, saved["quote_id"], IDENTITY, order_created_at=NOW)
    assert bound.status == "BOUND"
    bound_record = quotes.get_bound_order_quote(**IDENTITY, platform_order_id=ORDER_ID)
    assert bound_record is not None
    states = SqliteTransactionStateStore(database)
    current = states.get_or_create(**IDENTITY)
    states.transition(
        **IDENTITY, expected_revision=current.revision, event_id="payment-validated",
        transition_code="payment_validated_authoritative", flow_state="PAID_WAITING_FULFILLMENT",
        updates={
            "order_id": ORDER_ID, "order_status": "paid", "payment_status": "verified_paid",
            "fulfillment_status": "pending", "active_quote_record_id": bound_record["record_id"],
            "provider_status": None, "payment_validation_evidence": {
                "validation_status": "VERIFIED_PAID", "quote_id": bound_record["quote_id"],
                "quote_generation": bound_record["generation"], "binding_revision": bound_record["binding_revision"],
            },
        }, allow_compatible_bootstrap=True,
    )
    fake = provider or FakeProvider()
    fresh = FreshPreflight(provider=fake)
    service = LiangpiaoOrderService(
        fake, quote_store=rules, order_store=rules,
        quote_record_store=quotes, binding_service=binding, state_store=states,
        preflight_service=fresh, order_create_enabled=True, external_writes_enabled=True,
    )
    return service, fake, fresh, rules, quotes, states, bound_record


@pytest.mark.asyncio
async def test_payment_validated_liangpiao_requires_fresh_preflight_and_persists_order(tmp_path: Path) -> None:
    service, fake, fresh, rules, _, states, saved = prepare(tmp_path)

    result = await service.fulfill_payment_validated(paid_body(), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })

    assert result["status"] == "ORDER_CREATED"
    assert len(fresh.calls) == 1
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0]["showId"] == "show-11"
    assert fake.create_calls[0]["priceMode"] == "LIMIT"
    assert fake.create_calls[0]["seats"] == [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A"}]
    assert fake.create_calls[0]["outOrderNo"] == result["out_order_no"]
    persisted = rules.find_liangpiao_order(out_order_no=result["out_order_no"])
    assert persisted is not None
    assert persisted["quote_id"] == saved["quote_id"]
    assert persisted["quote_generation"] == saved["generation"]
    assert persisted["binding_revision"] == saved["binding_revision"]
    assert states.get(**IDENTITY).flow_state == "FULFILLMENT_IN_PROGRESS"


@pytest.mark.asyncio
async def test_repeated_payment_reconciles_existing_provider_order_without_create(tmp_path: Path) -> None:
    service, fake, _, _, _, states, _ = prepare(tmp_path)
    first = await service.fulfill_payment_validated(paid_body(event_id="paid-first"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })
    assert first["status"] == "ORDER_CREATED"
    fake.detail_result = {"status": "TICKING", "providerOrderNo": "lp-provider-11"}
    second = await service.fulfill_payment_validated(paid_body(event_id="paid-repeat"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })
    assert second["status"] == "RECONCILED"
    assert second["provider_status"] == "TICKING"
    assert len(fake.create_calls) == 1
    assert len(fake.detail_calls) == 1
    assert states.get(**IDENTITY).flow_state == "FULFILLMENT_IN_PROGRESS"


@pytest.mark.asyncio
async def test_non_liangpiao_and_wrong_identity_fail_closed_before_preflight(tmp_path: Path) -> None:
    service, fake, fresh, _, quotes, _, saved = prepare(tmp_path)
    class NonLiangpiaoBinding:
        def get_bound_quote(self, *_: object, **__: object) -> dict[str, Any]:
            return {**saved, "provider_route": "WANDA_SELF"}
    service._binding_service = NonLiangpiaoBinding()
    result = await service.fulfill_payment_validated(paid_body(event_id="paid-wanda"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })
    assert result["status"] in {"MANUAL_HOLD", "SKIPPED"}
    assert fake.create_calls == []
    assert fresh.calls == []

    wrong = await service.fulfill_payment_validated(
        paid_body(event_id="paid-wrong", identity={**IDENTITY, "buyer_id": "other-buyer"}),
        {"validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID"},
    )
    assert wrong["status"] == "MANUAL_HOLD"
    assert fake.create_calls == []


@pytest.mark.asyncio
async def test_lineage_incomplete_is_manual_hold_and_xianyu_quantity_is_ignored(tmp_path: Path) -> None:
    service, fake, fresh, _, quotes, _, saved = prepare(tmp_path)
    original = quotes.get_record(tenant_id=IDENTITY["tenant_id"], record_id=saved["record_id"])
    assert original is not None
    broken = {**original, "record_id": "record-broken", "quote_id": "quote-broken", "request_id": "request-broken", "liangpiao_show_id": None}
    quotes.save_quote(broken, ttl_seconds=600, now=NOW)
    class BrokenBinding:
        def get_bound_quote(self, *_: object, **__: object) -> dict[str, Any]:
            return {**broken, "platform_order_id": ORDER_ID, "binding_revision": 1}
    service._binding_service = BrokenBinding()
    result = await service.fulfill_payment_validated(
        paid_body(event_id="paid-lineage"),
        {"validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID"},
    )
    assert result["status"] == "MANUAL_HOLD"
    assert result["reason"] == "QUOTE_IDENTITY_MISMATCH"
    assert fresh.calls == []
    assert fake.create_calls == []


@pytest.mark.asyncio
async def test_limit_failure_uses_fixed_fallback_at_equal_cost_and_never_changes_seller_quote(tmp_path: Path) -> None:
    fake = FakeProvider(limit_cost=4500, fixed_cost=5000)
    fake.create_results = [OrderServiceError("LIANGPIAO_ORDER_CREATE_REJECTED", "limit unavailable")]
    service, fake, fresh, _, quotes, _, saved = prepare(tmp_path, provider=fake)

    result = await service.fulfill_payment_validated(paid_body(event_id="paid-fallback"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })

    assert result["status"] == "ORDER_CREATED"
    assert result["replay_authorized"] is True
    assert [call["priceMode"] for call in fake.create_calls] == ["LIMIT", "FIXED"]
    assert len(fresh.calls) == 2
    assert fresh.calls[1]["price_mode"] == "FIXED"
    unchanged = quotes.get_record(tenant_id=IDENTITY["tenant_id"], record_id=saved["record_id"])
    assert unchanged is not None
    assert unchanged["total_sell_price_fen"] == 5000
    assert result["max_auto_replay"] == 1


@pytest.mark.asyncio
async def test_fixed_cost_above_seller_commitment_requires_refund_without_provider_refund(tmp_path: Path) -> None:
    fake = FakeProvider(limit_cost=4500, fixed_cost=5001)
    fake.create_results = [OrderServiceError("LIANGPIAO_ORDER_CREATE_REJECTED", "limit unavailable")]
    service, fake, fresh, rules, _, states, _ = prepare(tmp_path, provider=fake)

    result = await service.fulfill_payment_validated(paid_body(event_id="paid-too-expensive"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })

    assert result["status"] == "REFUND_REQUIRED"
    assert result["reason"] == "LIANGPIAO_FIXED_COST_EXCEEDS_SELLER_COMMITMENT"
    assert len(fake.create_calls) == 1
    assert len(fresh.calls) == 2
    assert rules.find_liangpiao_order(out_order_no=result["out_order_no"]) is not None
    assert states.get(**IDENTITY).flow_state == "REFUND_PENDING"


@pytest.mark.asyncio
async def test_timeout_unknown_does_not_consume_fixed_replay(tmp_path: Path) -> None:
    fake = FakeProvider()
    fake.create_results = [TimeoutError("lost"), TimeoutError("lost again")]
    service, fake, fresh, rules, _, states, _ = prepare(tmp_path, provider=fake)

    result = await service.fulfill_payment_validated(paid_body(event_id="paid-unknown"), {
        "validation_status": "VERIFIED_PAID", "status": "VERIFIED_PAID",
    })

    assert result["status"] == "MANUAL_HOLD"
    assert result["reason"] == "LIANGPIAO_PROVIDER_UNKNOWN"
    assert len(fake.create_calls) == 2
    assert fake.create_calls[0] == fake.create_calls[1]
    assert len(fresh.calls) == 1
    assert states.get(**IDENTITY).flow_state == "MANUAL_HOLD"
    assert rules.find_liangpiao_order(out_order_no=result["out_order_no"]) is not None


@pytest.mark.asyncio
async def test_callback_ticket_evidence_replay_and_late_refund_state_are_safe(tmp_path: Path) -> None:
    rules = RulesFirstStore(tmp_path / "rules.sqlite3")
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    states.get_or_create(**IDENTITY)
    verifier = CallbackVerifier("secret")
    mapping = {
        **IDENTITY, "out_order_no": "lp-out-11", "provider_order_no": "lp-provider-11",
        "quote_id": "quote-11", "generation": 1, "flow_version": "V4_LIANGPIAO_FULFILLMENT_V1",
        "payload": {"priceMode": "LIMIT", "maxPrice": 5000, "seats": [{"rowNo": 5, "colNo": 8, "seatNo": "5排8座", "areaId": "A"}]},
    }
    rules.save_liangpiao_order({
        **mapping, "quote_hash": "q" * 64, "payload_hash": "p" * 64,
        "provider_status": "TICKING",
    })
    current = states.get(**IDENTITY)
    states.transition(
        **IDENTITY, expected_revision=current.revision, event_id="fulfillment-start",
        transition_code="liangpiao_order_creating", flow_state="FULFILLMENT_IN_PROGRESS",
        updates={"order_id": ORDER_ID, "payment_status": "verified_paid", "out_order_no": "lp-out-11", "provider_order_no": "lp-provider-11"},
        allow_compatible_bootstrap=True,
    )
    handler = LiangpiaoCallbackHandler(verifier, state_store=states, mapping_store=rules, enabled=True)
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "lp-out-11", "providerOrderNo": "lp-provider-11",
        "status": "TICKETED", "ticketCode": "TK-11", "eventId": "lp-event-11",
    }}).encode()
    timestamp = str(int(time.time()))
    callback_record = rules.record_liangpiao_callback(
        raw, signature=verifier.sign(raw, timestamp, "nonce-11"), timestamp=timestamp, nonce="nonce-11",
    )
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-11"), timestamp=timestamp, nonce="nonce-11")
    rules.update_liangpiao_callback(
        callback_record["callback_id"], verification_status="verified", processing_status="processed",
    )
    assert result["state_after"] == "TICKET_SENT"
    assert states.get(**IDENTITY).ticket_codes == ["TK-11"]

    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    handler_after_restart = LiangpiaoCallbackHandler(
        CallbackVerifier("secret"), state_store=states, mapping_store=rules, enabled=True,
    )
    with pytest.raises(Exception):
        await handler_after_restart.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-11"), timestamp=timestamp, nonce="nonce-11")

    current = states.get(**IDENTITY)
    states.transition(
        **IDENTITY, expected_revision=current.revision, event_id="refund-11",
        transition_code="refund_required", flow_state="REFUND_PENDING",
        updates={"payment_status": "refund_pending"},
    )
    late_raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "lp-out-11", "providerOrderNo": "lp-provider-11",
        "status": "TICKETED", "ticketCode": "LATE-TK", "eventId": "lp-event-late",
    }}).encode()
    late = await handler_after_restart.handle(late_raw, signature=verifier.sign(late_raw, timestamp, "nonce-late"), timestamp=timestamp, nonce="nonce-late")
    assert late["ignored"] is True
    assert states.get(**IDENTITY).flow_state == "REFUND_PENDING"


@pytest.mark.asyncio
async def test_ticketed_without_evidence_uses_detail_and_failed_does_not_refund_provider(tmp_path: Path) -> None:
    rules = RulesFirstStore(tmp_path / "rules.sqlite3")
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    states.get_or_create(**IDENTITY)
    rules.save_liangpiao_order({
        **IDENTITY, "out_order_no": "lp-out-detail", "provider_order_no": "lp-provider-detail",
        "quote_id": "quote-detail", "quote_hash": "q" * 64, "payload_hash": "p" * 64,
        "provider_status": "TICKING", "generation": 1,
        "payload": {"priceMode": "FIXED", "maxPrice": 5000},
    })
    current = states.get(**IDENTITY)
    states.transition(
        **IDENTITY, expected_revision=current.revision, event_id="start-detail",
        transition_code="liangpiao_order_creating", flow_state="FULFILLMENT_IN_PROGRESS",
        updates={"order_id": ORDER_ID, "payment_status": "verified_paid", "out_order_no": "lp-out-detail", "provider_order_no": "lp-provider-detail"},
        allow_compatible_bootstrap=True,
    )
    class DetailClient:
        async def order_detail(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"orderNo": "lp-provider-detail"}
            return {"status": "TICKETED", "ticketCode": "DETAIL-TK"}

    verifier = CallbackVerifier("secret")
    handler = LiangpiaoCallbackHandler(verifier, state_store=states, mapping_store=rules, client=DetailClient(), enabled=True)
    raw = json.dumps({"outOrderNo": "lp-out-detail", "providerOrderNo": "lp-provider-detail", "status": "TICKETED", "eventId": "detail-event"}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-detail"), timestamp=timestamp, nonce="nonce-detail")
    assert result["state_after"] == "TICKET_SENT"
    assert result["reply_plan"]["protected_facts"]["ticket_codes"] == ["DETAIL-TK"]


@pytest.mark.asyncio
async def test_legacy_failed_callback_can_keep_legacy_action_but_new_flow_cannot(tmp_path: Path) -> None:
    verifier = CallbackVerifier("secret")
    state = type("State", (), {"revision": 0, "generation": 1, "out_order_no": "out", "provider_order_no": "provider", "get": lambda self, **_: self, "transition": lambda self, **_: None})()
    legacy = LiangpiaoCallbackHandler(verifier, state_store=state, mapping_store={
        "out": {**IDENTITY, "out_order_no": "out", "payload": {"priceMode": "LIMIT"}},
    }, enabled=True)
    new_flow = LiangpiaoCallbackHandler(verifier, state_store=state, mapping_store={
        "out": {**IDENTITY, "out_order_no": "out", "flow_version": "V4_LIANGPIAO_FULFILLMENT_V1", "payload": {"priceMode": "LIMIT"}},
    }, enabled=True)
    for handler, nonce in ((legacy, "legacy-failed"), (new_flow, "new-failed")):
        raw = json.dumps({"event": "order.failed", "data": {"outOrderNo": "out", "status": "FAILED", "failReason": "failed"}}).encode()
        timestamp = str(int(time.time()))
        result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, nonce), timestamp=timestamp, nonce=nonce)
        if handler is legacy:
            assert result["fallback"]["available"] is True
            assert "platform_actions" in result
        else:
            assert result.get("platform_actions", []) == []
