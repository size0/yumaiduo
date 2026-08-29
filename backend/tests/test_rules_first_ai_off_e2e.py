from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.reply_template_store import ReplyTemplates
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


class Quotes:
    def get_record(self, *, tenant_id: str, record_id: str) -> dict[str, Any] | None:
        assert tenant_id == "tenant-1"
        if record_id != "quote-event":
            return None
        return {
            "record_id": record_id, "status": "succeeded", "cinema_name": "测试影院",
            "movie_title": "测试电影", "showtime_start": "19:30",
            "ticket_count": 2, "total_quote_cents": 8_800,
        }


class AiOffRuleEngine:
    async def process_event(self, body: Mapping[str, Any]) -> dict[str, object]:
        event_id = body["envelope"]["id"]
        if event_id == "quote-event":
            return {"decision": {"mode": "auto", "reason": "official_text_quote_ready", "actions": [
                {"id": "quote-event:reply", "type": "send_message", "text": "candidate"},
            ]}}
        if event_id == "confirm-event":
            return {"decision": {"mode": "auto", "reason": "order_submission_guidance_ready", "actions": []}}
        if event_id == "order-event":
            return {"decision": {"mode": "auto", "reason": "confirmed_quote_record_bound_to_order", "actions": [{
                "id": "order-event:change-order-price", "type": "change_order_price",
                "quote_snapshot": {
                    "quote_record_id": "quote-event", "confirmation_version": "confirm-v1",
                    "confirmation_source": "buyer_message", "confirmed_ticket_count": 2,
                    "order_id": "order-1", "target_amount_cents": 8_800,
                },
            }]}}
        reason = "authoritative_payment_confirmation_ready" if event_id == "paid-event" else "automation_inert_for_event"
        return {"decision": {"mode": "auto", "reason": reason, "actions": []}}

    def process_action_result(self, body: Mapping[str, Any]) -> dict[str, object]:
        if str(body.get("action_id") or "").endswith(":change-order-price"):
            return {"actions": [{
                "id": f"{body['event_id']}:price-verified", "type": "send_message",
                "text": "改价已完成，订单金额已调整为88.00元，请在订单页核对后付款。",
                "rule_governed": True,
            }]}
        return {"actions": []}


def event(event_id: str, event_type: str, *, order_status: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "envelope": {
            "id": event_id, "tenantId": "tenant-1", "event": event_type,
            "payload": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [],
    }
    if order_status is not None:
        value["envelope"]["payload"]["orderId"] = "order-1"
        value["order"] = {
            "orderId": "order-1", "accountUnb": "shop-1", "buyerUnb": "buyer-1",
            "chatId": "chat-1", "orderStatus": order_status, "quantity": 2,
        }
        if order_status == 2:
            value["order"]["payment"] = 8_800
    return value


async def drain_event(runtime: RulesFirstRuntime, value: dict[str, Any]) -> list[dict[str, Any]]:
    runtime.accept(value)
    assert await runtime.drain_once() is True
    return runtime.claim_commands()


def acknowledge_messages(runtime: RulesFirstRuntime, commands: list[dict[str, Any]]) -> None:
    for command in commands:
        if command["command_type"] == "send_message":
            runtime.record_command_result(
                command_id=command["command_id"], lease_token=command["lease_token"],
                result={"status": "succeeded", "message_id": f"message:{command['command_id']}"},
            )


@pytest.mark.asyncio
async def test_ai_off_full_transaction_reaches_completed_through_durable_commands_and_manual_fulfillment(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rules.sqlite3"
    protection = PlainProtector()
    outbox = RulesFirstStore(path, protector=protection)
    states = SqliteTransactionStateStore(path, protector=protection)
    runtime = RulesFirstRuntime(
        outbox, AiOffRuleEngine(), RuleStateCoordinator(states, quote_store=Quotes()), states,
    )

    quote_commands = await drain_event(runtime, event("quote-event", "im.message.received"))
    assert quote_commands[0]["action"]["text"] == "candidate"
    acknowledge_messages(runtime, quote_commands)

    confirm_commands = await drain_event(runtime, event("confirm-event", "im.message.received"))
    assert confirm_commands[0]["action"]["text"] == ReplyTemplates().order_submit_unpaid_template.replace("{张数}", "2")
    acknowledge_messages(runtime, confirm_commands)

    order_commands = await drain_event(runtime, event("order-event", "order.created", order_status=1))
    acknowledge_messages(runtime, order_commands)
    price = next(command for command in order_commands if command["command_type"] == "change_order_price")
    result = runtime.record_command_result(
        command_id=price["command_id"], lease_token=price["lease_token"],
        result={
            "status": "succeeded", "order_id": "order-1",
            "target_amount_cents": 8_800, "verified_amount_cents": 8_800,
        },
    )
    assert result["actions_created"] == 1
    verified = runtime.claim_commands()
    assert verified[0]["action"]["text"] == "改价已完成，订单金额已调整为88.00元，请在订单页核对后付款。"
    acknowledge_messages(runtime, verified)

    paid_commands = await drain_event(runtime, event("paid-event", "order.paid", order_status=2))
    assert paid_commands[0]["action"]["text"] == ReplyTemplates().payment_success_pending_ticket_template
    acknowledge_messages(runtime, paid_commands)
    duplicate_paid = await drain_event(runtime, event("paid-event-duplicate", "order.paid", order_status=2))
    assert duplicate_paid == []
    paid_state = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert paid_state is not None and paid_state.flow_state == "PAID_WAITING_FULFILLMENT"
    assert outbox.list_manual_tasks("tenant-1") == []

    # Seller sends ticket information directly in chat, then clicks FishMore's existing ship action.
    shipped_commands = await drain_event(runtime, event("shipped-event", "order.shipped", order_status=3))
    assert shipped_commands[0]["action"]["text"] == ReplyTemplates().order_shipped_template
    acknowledge_messages(runtime, shipped_commands)
    assert states.find_by_order(tenant_id="tenant-1", order_id="order-1").flow_state == "TICKET_SENT"
    assert await drain_event(runtime, event("completed-event", "order.finished", order_status=4)) == []
    completed = states.find_by_order(tenant_id="tenant-1", order_id="order-1")
    assert completed is not None and completed.flow_state == "COMPLETED"
