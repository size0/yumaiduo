from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return "enc:" + base64.b64encode(value.encode()).decode("ascii")

    def unprotect(self, value: str) -> str:
        encoded = value.removeprefix("enc:")
        return base64.b64decode(encoded).decode()


def event(event_id: str = "event-1", remote_id: str = "message-1") -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id,
            "tenantId": "tenant-1",
            "event": "im.message.received",
            "ts": 1_787_580_000_000,
            "payload": {
                "accountUnb": "shop-1",
                "peerUnb": "buyer-1",
                "chatId": "chat-1",
                "remoteMessageId": remote_id,
                "messageType": 1,
                "content": "确认报价",
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": None,
        "recent_messages": [],
    }


def store(tmp_path: Path) -> RulesFirstStore:
    return RulesFirstStore(tmp_path / "rules-first.sqlite3", protector=PlainProtector())


def test_event_inbox_uses_wal_and_deduplicates_event_and_platform_message(tmp_path: Path) -> None:
    runtime = store(tmp_path)

    first = runtime.enqueue_event(event())
    duplicate = runtime.enqueue_event(event())
    same_message = runtime.enqueue_event(event("event-2"))

    assert first == {"event_id": "event-1", "accepted": True, "duplicate": False}
    assert duplicate == {"event_id": "event-1", "accepted": True, "duplicate": True}
    assert same_message == {"event_id": "event-1", "accepted": True, "duplicate": True}
    assert runtime.journal_mode().lower() == "wal"
    assert "确认报价".encode() not in (tmp_path / "rules-first.sqlite3").read_bytes()


def test_event_completion_and_command_creation_are_atomic_and_deduplicated(tmp_path: Path) -> None:
    runtime = store(tmp_path)
    runtime.enqueue_event(event())
    claimed = runtime.claim_event(lease_seconds=60)
    assert claimed is not None

    action = {
        "id": "event-1:change-order-price",
        "type": "change_order_price",
        "quote_snapshot": {
            "order_id": "order-1",
            "quote_record_id": "quote-1",
            "confirmation_version": "confirmation-2",
            "target_amount_cents": 8_800,
        },
    }
    created = runtime.complete_event(
        claimed["inbox_id"], claimed["lease_token"], commands=[action],
        state_revision=3,
    )

    assert len(created) == 1
    assert created[0]["dedupe_key"] == "order-1\0quote-1\0confirmation-2\08800"
    assert runtime.complete_event(
        claimed["inbox_id"], claimed["lease_token"], commands=[action], state_revision=3,
    ) == created


def test_callback_system_commands_are_persisted_for_plugin_delivery(tmp_path: Path) -> None:
    store = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    commands = store.append_system_commands(
        tenant_id="tenant-1", event_id="callback-1",
        session={"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        commands=[{"id": "callback-1:reply", "type": "send_message", "text": "出票成功"}],
        state_revision=4,
    )
    assert len(commands) == 1
    assert commands[0]["context"]["system_event"] is True
    assert commands[0]["context"]["session"]["peerUnb"] == "buyer-1"
    assert store.claim_commands(limit=1)[0]["action"]["text"] == "出票成功"


def test_command_lease_is_recovered_and_result_is_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    runtime = store(tmp_path)
    runtime.enqueue_event(event())
    claimed_event = runtime.claim_event(now=now, lease_seconds=60)
    assert claimed_event is not None
    commands = runtime.complete_event(
        claimed_event["inbox_id"], claimed_event["lease_token"],
        commands=[{"id": "reply-1", "type": "send_message", "text": "请发送截图"}],
        state_revision=1,
    )

    first = runtime.claim_commands(now=now, lease_seconds=60)
    assert [item["command_id"] for item in first] == [commands[0]["command_id"]]
    assert runtime.claim_commands(now=now + timedelta(seconds=30), lease_seconds=60) == []
    reclaimed = runtime.claim_commands(now=now + timedelta(seconds=61), lease_seconds=60)
    assert reclaimed[0]["command_id"] == first[0]["command_id"]
    assert reclaimed[0]["lease_token"] != first[0]["lease_token"]

    result = {"status": "succeeded", "message_id": "platform-message-1"}
    assert runtime.record_command_result(
        reclaimed[0]["command_id"], reclaimed[0]["lease_token"], result,
    )["status"] == "succeeded"
    assert runtime.record_command_result(
        reclaimed[0]["command_id"], reclaimed[0]["lease_token"], result,
    )["status"] == "succeeded"


def test_completed_event_audits_are_tenant_scoped_and_expose_route_fields(tmp_path: Path) -> None:
    runtime = store(tmp_path)
    named_event = event()
    named_event["session"]["shopName"] = "万达电影票旗舰店"
    named_event["recent_messages"] = [{
        "direction": "inbound", "senderNick": "小鱼买家", "content": "确认报价",
    }]
    runtime.enqueue_event(named_event)
    claimed = runtime.claim_event()
    assert claimed is not None
    runtime.complete_event(
        claimed["inbox_id"], claimed["lease_token"], commands=[], state_revision=3,
        result={
            "reply_route": "agent", "rule_code": "consultation_agent_reply",
            "ai_called": True, "order_state": "collecting", "quote_state": "missing",
            "suppressed_reason": None, "action_types": ["send_message"],
        },
    )

    assert runtime.list_event_audits("tenant-2") == []
    audit = runtime.list_event_audits("tenant-1")[0]
    assert audit["reply_route"] == "agent"
    assert audit["ai_called"] is True
    assert audit["quote_state"] == "missing"
    assert audit["shop_id"] == "shop-1"
    assert audit["buyer_id"] == "buyer-1"
    assert audit["chat_id"] == "chat-1"
    assert audit["shop_name"] == "万达电影票旗舰店"
    assert audit["buyer_name"] == "小鱼买家"
    assert runtime.identity_labels("tenant-1", "shop-1", "buyer-1", "chat-1") == {
        "shop_name": "万达电影票旗舰店", "buyer_name": "小鱼买家",
    }
    assert "payload" not in audit


def test_unknown_price_change_is_reconciled_by_read_only_reclaims(tmp_path: Path) -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    runtime = store(tmp_path)
    runtime.enqueue_event(event())
    claimed_event = runtime.claim_event(now=now)
    assert claimed_event is not None
    runtime.complete_event(
        claimed_event["inbox_id"], claimed_event["lease_token"], commands=[{
            "id": "price-1", "type": "change_order_price",
            "quote_snapshot": {
                "order_id": "order-1", "quote_record_id": "quote-1",
                "confirmation_version": "confirmation-1", "target_amount_cents": 9_900,
            },
        }], state_revision=2,
    )
    command = runtime.claim_commands(now=now)[0]

    unknown = runtime.record_command_result(
        command["command_id"], command["lease_token"],
        {"status": "unknown", "order_id": "order-1"}, now=now,
    )

    assert unknown["status"] == "reconciling"
    assert unknown["next_attempt_at"] == (now + timedelta(seconds=5)).isoformat()
    assert runtime.claim_commands(now=now + timedelta(seconds=4)) == []
    retry = runtime.claim_commands(now=now + timedelta(seconds=5))[0]
    assert retry["reconciliation_only"] is True


def test_unknown_liangpiao_source_cancel_is_reclaimed_for_read_only_close_check(tmp_path: Path) -> None:
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    runtime = store(tmp_path)
    commands = runtime.append_system_commands(
        tenant_id="tenant-1", event_id="liangpiao-callback-1",
        session={"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        commands=[{
            "id": "liangpiao:out-1:cancel-source-order",
            "type": "cancel_failed_liangpiao_source_order", "order_id": "order-1",
        }], state_revision=2,
    )
    command = runtime.claim_commands(now=now)[0]

    unknown = runtime.record_command_result(
        command["command_id"], command["lease_token"],
        {"status": "unknown", "order_id": "order-1", "order_closed": False}, now=now,
    )

    assert unknown["status"] == "reconciling"
    assert runtime.claim_commands(now=now + timedelta(seconds=4)) == []
    retry = runtime.claim_commands(now=now + timedelta(seconds=5))[0]
    assert retry["command_id"] == commands[0]["command_id"]
    assert retry["reconciliation_only"] is True


def test_trusted_state_transition_resolves_only_compatible_manual_tasks(tmp_path: Path) -> None:
    runtime = store(tmp_path)
    common = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "transaction_id": "transaction-1", "transaction_revision": 4,
    }
    general = runtime.create_manual_task(**common, reason="order_unverified")
    fulfillment = runtime.create_manual_task(**common, reason="fulfillment_required")

    changed = runtime.resolve_manual_tasks_after_state_transition(
        tenant_id="tenant-1", transaction_id="transaction-1", state_revision=5,
        state_after="WAITING_PAYMENT", event_id="event-payment-ready",
    )
    tasks = {item["task_id"]: item for item in runtime.list_manual_tasks("tenant-1")}

    assert changed == 1
    assert tasks[general["task_id"]]["status"] == "completed"
    assert tasks[fulfillment["task_id"]]["status"] == "pending"

    changed = runtime.resolve_manual_tasks_after_state_transition(
        tenant_id="tenant-1", transaction_id="transaction-1", state_revision=6,
        state_after="TICKET_SENT", event_id="event-ticketed",
    )
    tasks = {item["task_id"]: item for item in runtime.list_manual_tasks("tenant-1")}
    assert changed == 1
    assert tasks[fulfillment["task_id"]]["status"] == "completed"


def test_manual_tasks_are_tenant_scoped_and_require_revision_to_resume(tmp_path: Path) -> None:
    runtime = store(tmp_path)
    task = runtime.create_manual_task(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        transaction_id="transaction-1", transaction_revision=4,
        reason="multiple_pending_orders",
    )

    assert runtime.list_manual_tasks("tenant-2") == []
    assert runtime.list_manual_tasks("tenant-1")[0]["task_id"] == task["task_id"]
    claimed = runtime.claim_manual_task(
        "tenant-1", task["task_id"], expected_revision=4, operator_id="seller-1",
    )
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "seller-1"
    try:
        runtime.claim_manual_task(
            "tenant-1", task["task_id"], expected_revision=4, operator_id="seller-2",
        )
    except ValueError as error:
        assert str(error) == "manual_task_already_claimed"
    else:
        raise AssertionError("an active manual lease must exclude another seller")
    resumed = runtime.complete_manual_task(
        "tenant-1", task["task_id"], expected_revision=4,
        lease_token=claimed["lease_token"], resolution="resume",
    )
    assert resumed["status"] == "completed"
