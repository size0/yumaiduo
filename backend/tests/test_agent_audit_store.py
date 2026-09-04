from __future__ import annotations

import base64
from pathlib import Path

from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return "enc:" + base64.b64encode(value.encode()).decode("ascii")

    def unprotect(self, value: str) -> str:
        return base64.b64decode(value.removeprefix("enc:")).decode()


def _store(tmp_path: Path) -> RulesFirstStore:
    return RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())


def _event(event_id: str = "agent-event-1") -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id,
            "tenantId": "tenant-1",
            "event": "im.message.received",
            "payload": {
                "accountUnb": "shop-1",
                "peerUnb": "buyer-1",
                "chatId": "chat-1",
                "remoteMessageId": "buyer-message-1",
                "messageType": 1,
                "content": "2张",
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
    }


def test_agent_run_and_tool_trace_are_idempotent_and_protected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_agent_run(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        event_id="agent-event-1", context={"confirmed_facts": {"movie": "示例"}},
    )
    duplicate = store.create_agent_run(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        event_id="agent-event-1", status="failed", context={"should_not_replace": True},
    )
    assert duplicate["run_id"] == run["run_id"]
    assert duplicate["status"] == "running"
    assert duplicate["context"] == {"confirmed_facts": {"movie": "示例"}}

    first = store.append_agent_tool_call(
        run["run_id"], tool_name="get_quote", arguments={},
        result={"status": "success", "quote": None}, sequence=0,
    )
    same = store.append_agent_tool_call(
        run["run_id"], tool_name="get_quote", arguments={"ignored": True},
        result={"different": True}, sequence=0,
    )
    assert same == first
    store.append_agent_tool_call(
        run["run_id"], tool_name="update_quote_request", arguments={"ticket_count": 2},
        result={"status": "success"}, status="succeeded",
    )
    updated = store.update_agent_run(
        run["run_id"], status="reply_ready", reply_origin="canonical_conversation_agent",
    )
    loaded = store.get_agent_run(run["run_id"])
    assert updated["status"] == "reply_ready"
    assert loaded is not None
    assert [item["tool_name"] for item in loaded["tool_calls"]] == [
        "get_quote", "update_quote_request",
    ]
    assert loaded["tool_trace"] == loaded["tool_calls"]


def test_command_outbox_result_and_agent_delivery_survive_store_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.create_agent_run(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        event_id="agent-event-1",
    )
    store.enqueue_event(_event())
    claimed_event = store.claim_event()
    assert claimed_event is not None
    created = store.complete_event(
        claimed_event["inbox_id"], claimed_event["lease_token"],
        commands=[{"id": "reply-1", "type": "send_message", "text": "已记下2张"}],
        state_revision=1,
    )
    command = store.claim_commands()[0]
    store.record_command_result(
        command["command_id"], command["lease_token"],
        {"status": "succeeded", "message_id": "platform-message-1"},
    )
    persisted_command = store.get_command(created[0]["command_id"])
    assert persisted_command is not None
    assert persisted_command["result"]["message_id"] == "platform-message-1"
    assert persisted_command["sent_message_id"] == "platform-message-1"
    store.record_agent_run_delivery(
        run["run_id"], command_id=persisted_command["command_id"],
        sent_message_id=persisted_command["sent_message_id"],
    )

    reopened = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    loaded = reopened.get_agent_run_for_event("tenant-1", "agent-event-1")
    assert loaded is not None
    assert loaded["command_id"] == created[0]["command_id"]
    assert loaded["sent_message_id"] == "platform-message-1"

