from __future__ import annotations

import json
import time

import pytest

from app.liangpiao_callbacks import CallbackError, CallbackVerifier, LiangpiaoCallbackHandler
from app.rule_contracts import ReplyPlan
from app.rule_templates import validate_reply_plan
from app.rules_first_store import RulesFirstStore
from app.transaction_state_store import TransactionStateStore


def test_callback_records_are_persisted_redacted_and_tenant_scoped(tmp_path) -> None:
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    raw = json.dumps({
        "outOrderNo": "out-1", "providerOrderNo": "provider-1", "eventId": "event-1",
        "status": "ticket_sent", "ticketCode": "SECRET-TICKET",
    }).encode()
    record = store.record_liangpiao_callback(
        raw, signature="signature", timestamp="123", nonce="nonce-1",
    )
    assert record["out_order_no"] == "out-1"
    assert record["provider_order_no"] == "provider-1"
    assert record["event_id"] == "event-1"
    assert "ticketCode" not in record
    updated = store.update_liangpiao_callback(
        int(record["callback_id"]), tenant_id="tenant-1", verification_status="verified",
        processing_status="applied", result_code="LIANGPIAO_CALLBACK_APPLIED",
    )
    assert updated["processing_status"] == "applied"
    assert store.list_liangpiao_callbacks("tenant-1")[0]["callback_id"] == record["callback_id"]
    assert store.list_liangpiao_callbacks("tenant-2") == []


class FakeState:
    def __init__(
        self, *, generation: int = 1, out_order_no: str | None = None,
        provider_order_no: str | None = None,
    ) -> None:
        self.revision = 0
        self.generation = generation
        self.out_order_no = out_order_no
        self.provider_order_no = provider_order_no
        self.calls: list[dict[str, object]] = []

    def get(self, **_: object) -> object:
        return self

    def transition(self, **kwargs: object) -> None:
        self.calls.append(dict(kwargs))
        self.revision += 1


@pytest.mark.asyncio
async def test_old_generation_ticketed_callback_is_ignored_without_reply_plan() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState(
        generation=2, out_order_no="out-new", provider_order_no="provider-new",
    )
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-old": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-old", "provider_order_no": "provider-old",
            "quote_id": "quote-old", "generation": 1,
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "out-old", "providerOrderNo": "provider-old",
        "status": "TICKETED", "ticketCode": "OLD-TICKET-CODE",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-old-ticketed"),
        timestamp=timestamp, nonce="nonce-old-ticketed",
    )

    assert result == {
        "status": "ok", "code": "LIANGPIAO_CALLBACK_STALE_GENERATION", "ignored": True,
    }
    assert state.calls == []
    assert "reply_plan" not in result


@pytest.mark.asyncio
async def test_old_generation_failed_callback_is_ignored_without_fallback_reply() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState(
        generation=2, out_order_no="out-new", provider_order_no="provider-new",
    )
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-old": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-old", "provider_order_no": "provider-old",
            "quote_id": "quote-old", "generation": 1,
            "payload": {"priceMode": "LIMIT"},
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.failed", "data": {
        "outOrderNo": "out-old", "providerOrderNo": "provider-old",
        "status": "FAILED", "failReason": "旧订单迟到失败回调",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-old-failed"),
        timestamp=timestamp, nonce="nonce-old-failed",
    )

    assert result == {
        "status": "ok", "code": "LIANGPIAO_CALLBACK_STALE_GENERATION", "ignored": True,
    }
    assert state.calls == []
    assert "reply_plan" not in result
    assert "fallback" not in result


@pytest.mark.asyncio
async def test_current_generation_ticketed_callback_still_applies() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState(
        generation=2, out_order_no="out-new", provider_order_no="provider-new",
    )
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-new": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-new", "provider_order_no": "provider-new",
            "quote_id": "quote-new", "generation": 2,
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "out-new", "providerOrderNo": "provider-new",
        "status": "TICKETED", "ticketCode": "CURRENT-TICKET-CODE",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-current-ticketed"),
        timestamp=timestamp, nonce="nonce-current-ticketed",
    )

    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert result["reply_plan"]["variables"]["取票码"] == "CURRENT-TICKET-CODE"
    assert len(state.calls) == 1
    assert state.calls[0]["flow_state"] == "TICKET_SENT"


@pytest.mark.asyncio
async def test_persisted_quote_generation_is_used_when_order_snapshot_omits_generation() -> None:
    class MappingStore:
        def __init__(self) -> None:
            self.updated = False

        def find_liangpiao_order(self, **_: object) -> dict[str, object]:
            return {
                "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
                "out_order_no": "out-old", "provider_order_no": "provider-old",
                "quote_id": "quote-old",
            }

        def get_selected_seat_quote(self, quote_id: str) -> dict[str, object]:
            assert quote_id == "quote-old"
            return {"quote_id": quote_id, "generation": 1}

        def update_liangpiao_order(self, *_: object, **__: object) -> None:
            self.updated = True

    verifier = CallbackVerifier("secret")
    state = FakeState(
        generation=2, out_order_no="out-new", provider_order_no="provider-new",
    )
    mappings = MappingStore()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state, mapping_store=mappings, enabled=True,
    )
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "out-old", "providerOrderNo": "provider-old",
        "status": "TICKETED", "ticketCode": "OLD-TICKET-CODE",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-persisted-generation"),
        timestamp=timestamp, nonce="nonce-persisted-generation",
    )

    assert result["code"] == "LIANGPIAO_CALLBACK_STALE_GENERATION"
    assert result["ignored"] is True
    assert state.calls == []
    assert mappings.updated is False
    assert "reply_plan" not in result


@pytest.mark.asyncio
async def test_generation_change_between_preflight_and_transition_is_ignored() -> None:
    class AdvancingStateStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.reads = 0

        def get(self, **_: object) -> FakeState:
            self.reads += 1
            if self.reads == 1:
                return FakeState(
                    generation=2, out_order_no="out-current",
                    provider_order_no="provider-current",
                )
            return FakeState(
                generation=3, out_order_no="out-newer",
                provider_order_no="provider-newer",
            )

        def transition(self, **kwargs: object) -> None:
            self.calls.append(dict(kwargs))

    verifier = CallbackVerifier("secret")
    state = AdvancingStateStore()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-current": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-current", "provider_order_no": "provider-current",
            "quote_id": "quote-current", "generation": 2,
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "out-current", "providerOrderNo": "provider-current",
        "status": "TICKETED", "ticketCode": "RACING-OLD-TICKET",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-generation-race"),
        timestamp=timestamp, nonce="nonce-generation-race",
    )

    assert result["code"] == "LIANGPIAO_CALLBACK_STALE_GENERATION"
    assert result["ignored"] is True
    assert "reply_plan" not in result
    assert state.calls == []


@pytest.mark.asyncio
async def test_callback_signature_replay_and_state_progression() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-1": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "out_order_no": "out-1"}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-1", "status": "ticket_sent", "ticketCode": "TK-1"}).encode()
    timestamp = str(int(time.time()))
    signature = verifier.sign(raw, timestamp, "nonce-1")
    result = await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-1")
    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert result["reply_plan"]["template_key"] == "flow.fulfillment.liangpiao_ticketed"
    assert validate_reply_plan(ReplyPlan.model_validate(result["reply_plan"]), state="TICKET_SENT")
    assert state.calls[0]["flow_state"] == "TICKET_SENT"
    with pytest.raises(CallbackError) as error:
        await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-1")
    assert error.value.code == "LIANGPIAO_CALLBACK_REPLAY"


@pytest.mark.asyncio
async def test_callback_missing_ticket_evidence_goes_manual_hold() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-2": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "out_order_no": "out-2"}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-2", "status": "ticket_sent"}).encode()
    timestamp = str(int(time.time()))
    signature = verifier.sign(raw, timestamp, "nonce-2")
    result = await handler.handle(raw, signature=signature, timestamp=timestamp, nonce="nonce-2")
    assert result["code"] == "LIANGPIAO_TICKET_EVIDENCE_MISSING"
    assert state.calls[0]["flow_state"] == "MANUAL_HOLD"


@pytest.mark.asyncio
async def test_ticket_reconciliation_uses_documented_order_no_only() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def order_detail(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(dict(kwargs))
            return {"orderNo": "provider-2", "status": "ticket_sent", "ticketCode": "CODE-2"}

    client = Client()
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state, client=client,
        mapping_store={"out-2": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-2", "provider_order_no": "provider-2",
        }}, enabled=True,
    )
    raw = json.dumps({
        "outOrderNo": "out-2", "providerOrderNo": "provider-2", "status": "ticket_sent",
    }).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-order-no"),
        timestamp=timestamp, nonce="nonce-order-no",
    )

    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert client.calls == [{"orderNo": "provider-2"}]


@pytest.mark.asyncio
async def test_ticket_reconciliation_never_queries_detail_by_out_order_no() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def order_detail(self, **_: object) -> dict[str, object]:
            self.calls += 1
            raise AssertionError("outOrderNo is not accepted by documented order detail API")

    client = Client()
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state, client=client,
        mapping_store={"out-2": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-2",
        }}, enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-2", "status": "ticket_sent"}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-no-provider-id"),
        timestamp=timestamp, nonce="nonce-no-provider-id",
    )

    assert result["code"] == "LIANGPIAO_TICKET_EVIDENCE_MISSING"
    assert client.calls == 0


@pytest.mark.asyncio
async def test_callback_amount_mismatch_goes_manual_hold() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-3": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
                                 "out_order_no": "out-3", "payload": {"maxPrice": 8800}}},
        enabled=True,
    )
    raw = json.dumps({"outOrderNo": "out-3", "status": "paid", "amountFen": 7700}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-3"), timestamp=timestamp, nonce="nonce-3")
    assert result["code"] == "LIANGPIAO_CALLBACK_AMOUNT_MISMATCH"
    assert state.calls[0]["flow_state"] == "MANUAL_HOLD"


@pytest.mark.asyncio
async def test_callback_ticketed_nested_payload_contains_ticket_reply_plan() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-4": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
                                 "out_order_no": "out-4"}},
        enabled=True,
    )
    raw = json.dumps({"event": "order.ticketed", "data": {
        "outOrderNo": "out-4", "orderNo": "lp-4", "status": "TICKETED",
        "pickupUrl": "https://t-v3.liangpiao.net.cn/pickup/4",
        "tickets": [{"seatNo": "5排8座", "ticketCode": "A-001"}],
    }}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-4"), timestamp=timestamp, nonce="nonce-4")
    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert result["state_after"] == "TICKET_SENT"
    plan = result["reply_plan"]
    assert plan["template_key"] == "flow.fulfillment.liangpiao_ticketed"
    assert plan["variables"]["取票码"] == "A-001"
    assert plan["variables"]["取票链接"].endswith("/4")
    assert state.calls[0]["updates"]["provider_status"] == "ticket_sent"


@pytest.mark.asyncio
async def test_callback_failed_contains_failure_reply_plan_and_holds() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-5": {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
                                 "out_order_no": "out-5"}},
        enabled=True,
    )
    raw = json.dumps({"event": "order.failed", "data": {
        "outOrderNo": "out-5", "orderNo": "lp-5", "failReason": "所选座位已售",
        "status": "FAILED", "refundAmount": "8800",
    }}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-5"), timestamp=timestamp, nonce="nonce-5")
    assert result["code"] == "LIANGPIAO_CALLBACK_APPLIED"
    assert result["state_after"] == "MANUAL_HOLD"
    assert result["reply_plan"]["template_key"] == "flow.fulfillment.liangpiao_fixed_failed"
    assert result["reply_plan"]["variables"]["失败原因"] == "所选座位已售"
    assert state.calls[0]["updates"]["provider_status"] == "failed"


@pytest.mark.asyncio
async def test_limit_failed_offers_fixed_fallback_without_refund_action() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-limit": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-limit", "payload": {"priceMode": "LIMIT"},
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.failed", "data": {
        "outOrderNo": "out-limit", "status": "FAILED", "failReason": "供应商无座",
        "refundAmount": 8800, "lossAmount": 0,
    }}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-limit"), timestamp=timestamp, nonce="nonce-limit")
    assert result["fallback"]["available"] is True
    assert result["fallback"]["to_price_mode"] == "FIXED"
    assert result["fallback"]["refund_api_allowed"] is False
    assert result["reply_plan"]["protected_facts"]["refund_api_allowed"] is False
    assert state.calls[0]["updates"]["fixed_switch_status"] == "pending"
    assert state.calls[0]["updates"]["fixed_switch_source_order_no"] == "out-limit"
    assert state.calls[0]["updates"]["fixed_switch_expires_at"]


@pytest.mark.asyncio
async def test_current_limit_failure_authorizes_only_platform_source_order_cancel(tmp_path) -> None:
    class Protector:
        def protect(self, value: str) -> str:
            return "sealed:" + value[::-1]

        def unprotect(self, value: str) -> str:
            return value.removeprefix("sealed:")[::-1]

    states = TransactionStateStore(tmp_path / "states.json", protector=Protector())

    class ProviderClient:
        def __init__(self) -> None:
            self.refund_calls = 0

        async def order_refund(self, *_: object, **__: object) -> None:
            self.refund_calls += 1
            raise AssertionError("FAILED Liangpiao orders must never call provider refund")

    provider = ProviderClient()
    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c"}
    states.transition(
        **identity, expected_revision=0, event_id="bootstrap",
        transition_code="compat.bootstrap.fulfillment", flow_state="FULFILLMENT_IN_PROGRESS",
        updates={
            "order_id": "fish-paid-1", "order_status": "paid",
            "payment_status": "verified_paid", "fulfillment_status": "claimed",
            "out_order_no": "out-limit", "provider_order_no": "provider-limit",
            "provider_status": "processing",
        }, allow_compatible_bootstrap=True,
    )
    verifier = CallbackVerifier("secret")
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=states, client=provider,
        mapping_store={"out-limit": {
            **identity, "out_order_no": "out-limit", "provider_order_no": "provider-limit",
            "generation": 1, "payload": {"priceMode": "LIMIT"},
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.failed", "data": {
        "eventId": "lp-failed-2", "outOrderNo": "out-limit",
        "providerOrderNo": "provider-limit", "status": "FAILED",
        "failReason": "供应商无座",
    }}).encode()
    timestamp = str(int(time.time()))

    result = await handler.handle(
        raw, signature=verifier.sign(raw, timestamp, "nonce-current-limit"),
        timestamp=timestamp, nonce="nonce-current-limit",
    )

    assert result["fallback"]["refund_api_allowed"] is False
    assert result["reply_plan"]["template_key"] == "flow.fulfillment.liangpiao_limit_refund_pending"
    assert result["platform_actions"] == [{
        "id": "liangpiao:out-limit:cancel-source-order",
        "type": "cancel_failed_liangpiao_source_order",
        "order_id": "fish-paid-1", "tenant_id": "t", "shop_id": "s",
        "buyer_id": "b", "chat_id": "c", "source_out_order_no": "out-limit",
        "source_generation": 1, "source_provider_status": "failed",
        "source_price_mode": "LIMIT", "callback_verified": True,
        "refund_authorization": "verified_current_limit_failure",
        "dedupe_key": "liangpiao:out-limit:fish-paid-1:cancel-source-order:g1",
    }]
    current = states.get(**identity)
    assert current is not None
    assert current.fixed_switch_source_order_status == "refund_pending"
    assert current.fixed_switch_source_platform_order_id == "fish-paid-1"
    assert current.order_status == "refund_pending"
    assert current.payment_status == "refund_pending"
    assert provider.refund_calls == 0


@pytest.mark.asyncio
async def test_fixed_failed_does_not_offer_third_channel() -> None:
    verifier = CallbackVerifier("secret")
    state = FakeState()
    handler = LiangpiaoCallbackHandler(
        verifier, state_store=state,
        mapping_store={"out-fixed": {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "out_order_no": "out-fixed", "payload": {"priceMode": "FIXED"},
        }}, enabled=True,
    )
    raw = json.dumps({"event": "order.failed", "data": {
        "outOrderNo": "out-fixed", "status": "FAILED", "failReason": "出票失败",
    }}).encode()
    timestamp = str(int(time.time()))
    result = await handler.handle(raw, signature=verifier.sign(raw, timestamp, "nonce-fixed"), timestamp=timestamp, nonce="nonce-fixed")
    assert result["fallback"]["available"] is False
    assert result["fallback"]["to_price_mode"] is None
    assert result["fallback"]["refund_api_allowed"] is False
