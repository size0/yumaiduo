from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


class Engine:
    async def process_event(self, _: object) -> dict[str, object]:
        return {"decision": {
            "mode": "auto", "reason": "confirmed_quote_record_bound_to_order",
            "actions": [{
                "id": "event-1:change-order-price", "type": "change_order_price",
                "quote_snapshot": {
                    "quote_record_id": "quote-1", "confirmation_version": "confirm-1",
                    "confirmation_source": "buyer_message", "confirmed_ticket_count": 2,
                    "order_id": "order-1", "target_amount_cents": 8_800,
                },
            }],
        }}

    def process_action_result(self, body: object) -> dict[str, object]:
        assert isinstance(body, dict)
        return {"ok": True, "actions": [{
            "id": "event-1:price-result", "type": "send_message",
            "text": "订单金额已核验为88.00元，核对无误后可付款。",
        }]}


def body() -> dict[str, object]:
    return {
        "envelope": {
            "id": "event-1", "tenantId": "tenant-1", "event": "order.created",
            "payload": {"orderId": "order-1", "accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": {
            "orderId": "order-1", "accountUnb": "shop-1", "buyerUnb": "buyer-1",
            "chatId": "chat-1", "orderStatus": 1, "quantity": 2,
        },
        "recent_messages": [],
    }


def test_read_only_write_fuse_cancels_transaction_commands_but_keeps_chat_replies(tmp_path: Path) -> None:
    path = tmp_path / "read-only.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    runtime = RulesFirstRuntime(outbox, Engine(), RuleStateCoordinator(states), states)

    runtime.accept(body())
    claimed = outbox.claim_event()
    assert claimed is not None
    outbox.complete_event(
        claimed["inbox_id"], claimed["lease_token"],
        commands=[
            {"id": "event-1:reply", "type": "send_message", "text": "报价结果"},
            {
                "id": "event-1:price", "type": "change_order_price",
                "quote_snapshot": {
                    "order_id": "order-1", "quote_record_id": "quote-1",
                    "confirmation_version": "confirm-1", "target_amount_cents": 8_800,
                },
            },
        ],
        state_revision=0,
    )

    assert runtime.open_transaction_write_fuse() == 1
    remaining = runtime.claim_commands()
    assert [item["command_type"] for item in remaining] == ["send_message"]
    assert outbox.get_command("cmd-" + "0" * 40) is None


@pytest.mark.asyncio
async def test_runtime_accepts_before_reduction_and_exposes_only_durable_commands(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    runtime = RulesFirstRuntime(outbox, Engine(), RuleStateCoordinator(states), states)

    assert runtime.accept(body()) == {"event_id": "event-1", "accepted": True, "duplicate": False}
    assert runtime.claim_commands() == []
    assert await runtime.drain_once() is True
    audit = outbox.list_event_audits("tenant-1")[0]
    assert audit["reply_route"] == "rule"
    assert audit["ai_called"] is False
    assert audit["action_types"] == ["change_order_price"]
    assert audit["suppressed_reason"] is None

    commands = runtime.claim_commands()
    assert {command["command_type"] for command in commands} == {"change_order_price"}
    price_command = next(command for command in commands if command["command_type"] == "change_order_price")
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "PRICE_CHANGING"

    recorded = runtime.record_command_result(
        command_id=price_command["command_id"], lease_token=price_command["lease_token"],
        result={
            "status": "succeeded", "order_id": "order-1",
            "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
        },
    )
    assert recorded["actions_created"] == 1
    reply = runtime.claim_commands()[0]
    assert reply["command_type"] == "send_message"
    assert "88.00" in reply["action"]["text"]
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "WAITING_PAYMENT"


def test_platform_cancel_result_persists_fixed_offer_only_after_closed_readback(tmp_path: Path) -> None:
    class NoFollowupEngine(Engine):
        def process_action_result(self, _: object) -> dict[str, object]:
            return {"actions": []}

    path = tmp_path / "rules.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1",
        "buyer_id": "buyer-1", "chat_id": "chat-1",
    }
    seeded = states.transition(
        **identity, expected_revision=0, event_id="bootstrap",
        transition_code="compat.bootstrap.refund-pending", flow_state="MANUAL_HOLD",
        updates={
            "fixed_switch_status": "pending",
            "fixed_switch_expires_at": "2099-01-01T00:00:00+00:00",
            "fixed_switch_source_order_no": "out-limit",
            "fixed_switch_source_order_status": "refund_pending",
            "fixed_switch_source_platform_order_id": "order-1",
            "order_id": "order-1", "order_status": "refund_pending",
            "payment_status": "refund_pending", "fulfillment_status": "failed",
        }, allow_compatible_bootstrap=True,
    )
    action = {
        "id": "liangpiao:out-limit:cancel-source-order",
        "type": "cancel_failed_liangpiao_source_order", "order_id": "order-1",
        "dedupe_key": "liangpiao:out-limit:order-1:cancel-source-order:g1",
    }
    outbox.append_system_commands(
        tenant_id="tenant-1", event_id="liangpiao-callback-1",
        session={"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        commands=[action], state_revision=seeded.revision,
    )
    runtime = RulesFirstRuntime(
        outbox, NoFollowupEngine(), RuleStateCoordinator(states), states,
    )
    command = runtime.claim_commands()[0]

    recorded = runtime.record_command_result(
        command_id=command["command_id"], lease_token=command["lease_token"],
        result={
            "status": "succeeded", "order_id": "order-1", "order_closed": True,
            "cancel_attempted": True, "source_out_order_no": "out-limit",
        },
    )

    assert recorded["actions_created"] == 1
    follow_up = runtime.claim_commands()[0]
    assert follow_up["command_type"] == "send_message"
    assert "一口价" in follow_up["action"]["text"]
    current = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert current is not None
    assert current.fixed_switch_source_order_status == "closed"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_runtime_processes_different_buyer_sessions_concurrently(tmp_path: Path) -> None:
    class SlowEngine:
        async def process_event(self, _: object) -> dict[str, object]:
            await asyncio.sleep(0.1)
            return {"decision": {"mode": "auto", "actions": [], "reason": "no_transition"}}

        def process_action_result(self, _: object) -> dict[str, object]:
            return {"actions": []}

    path = tmp_path / "concurrent.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    runtime = RulesFirstRuntime(
        outbox, SlowEngine(), RuleStateCoordinator(states), states,
        max_concurrent_events=8,
    )
    for index in range(8):
        value = body()
        value["envelope"] = {**value["envelope"], "id": f"event-{index}"}
        value["envelope"]["payload"] = {
            **value["envelope"]["payload"], "peerUnb": f"buyer-{index}",
        }
        value["session"] = {**value["session"], "peerUnb": f"buyer-{index}"}
        assert runtime.accept(value)["accepted"] is True

    started = time.perf_counter()
    await runtime.start()
    await asyncio.sleep(0.2)
    await runtime.stop()
    assert time.perf_counter() - started < 0.35
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from event_inbox where status='completed'").fetchone()[0] == 8


@pytest.mark.asyncio
async def test_passwordless_payment_result_never_emits_cancel_or_fulfillment_command(tmp_path: Path) -> None:
    class PaidEngine(Engine):
        def process_action_result(self, body: Mapping[str, Any]) -> dict[str, object]:
            if not str(body.get("action_id") or "").endswith(":change-order-price"):
                return {"actions": []}
            return {"actions": [{
                "id": f"{body['event_id']}:unsafe-cancel", "type": "cancel_paid_amount_mismatch",
                "order_id": "order-1",
            }]}

    path = tmp_path / "rules.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    runtime = RulesFirstRuntime(outbox, PaidEngine(), RuleStateCoordinator(states), states)
    runtime.accept(body())
    assert await runtime.drain_once() is True
    commands = runtime.claim_commands()
    price = next(item for item in commands if item["command_type"] == "change_order_price")

    recorded = runtime.record_command_result(
        command_id=price["command_id"], lease_token=price["lease_token"],
        result={
            "status": "skipped", "reason_code": "order_already_paid", "order_id": "order-1",
            "target_amount_cents": 8_800, "verified_amount_cents": 9_900,
        },
    )

    assert recorded["actions_created"] == 0
    assert runtime.claim_commands() == []
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "MANUAL_HOLD"
    assert state.fulfillment_status == "none"
    assert outbox.list_manual_tasks("tenant-1")[0]["reason"] == "payment_before_amount_verification"


@pytest.mark.asyncio
async def test_external_write_fuse_cancels_pending_command_and_creates_manual_hold(tmp_path: Path) -> None:
    path = tmp_path / "rules.sqlite3"
    protected = PlainProtector()
    outbox = RulesFirstStore(path, protector=protected)
    states = SqliteTransactionStateStore(path, protector=protected)
    runtime = RulesFirstRuntime(outbox, Engine(), RuleStateCoordinator(states), states)
    runtime.accept(body())
    assert await runtime.drain_once() is True

    assert runtime.open_write_fuse() == 1
    assert runtime.claim_commands() == []
    state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert state is not None and state.flow_state == "MANUAL_HOLD"
    tasks = outbox.list_manual_tasks("tenant-1")
    assert tasks[0]["reason"] == "external_write_fuse_open"
