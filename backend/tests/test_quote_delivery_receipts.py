from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import _quote_delivery_record_id, create_app
from app.quote_record_store import QuoteRecordStore
from app.rules_first_store import RulesFirstStore


class CommandResultRuntime:
    def __init__(self, store: RulesFirstStore) -> None:
        self._store = store

    def record_command_result(self, *, command_id: str, lease_token: str, result: dict[str, object]) -> dict[str, object]:
        return self._store.record_command_result(command_id, lease_token, result)


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def test_quote_delivery_action_id_variants_require_explicit_record_id() -> None:
    for action_id in (
        "event-1:reply",
        "event-1:reply:0",
        "event-1:reply:1",
        "event-1:canonical-reply",
        "event-1:canonical-reply:0",
        "event-1:canonical-reply:1",
        "event-1:repriced-quote",
    ):
        assert _quote_delivery_record_id(
            {"id": action_id, "type": "send_message", "quote_record_id": "record-1"},
            event_id="event-1",
        ) == "record-1"

    assert _quote_delivery_record_id(
        {"id": "event-1:reply:1", "type": "send_message"},
        event_id="event-1",
    ) is None
    assert _quote_delivery_record_id(
        {"id": "event-1:other", "type": "send_message", "quote_record_id": "record-1"},
        event_id="event-1",
    ) is None
    assert _quote_delivery_record_id(
        {"id": "event-1:reply:summary", "type": "send_message", "quote_record_id": "record-1"},
        event_id="event-1",
    ) is None


def test_command_result_records_delivery_on_explicit_record_id_for_indexed_reply(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    quote_store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    quote_store.save({
        "record_id": "record-1", "quote_id": "quote-1", "event_id": "event-1",
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1",
        "status": "succeeded", "created_at": datetime(2026, 9, 10, tzinfo=timezone.utc).isoformat(),
    })
    rules = RulesFirstStore(tmp_path / "rules.sqlite3", protector=PlainProtector())
    event = {
        "envelope": {
            "id": "event-1", "tenantId": "tenant-1", "event": "im.message.received",
            "payload": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1", "messageId": "in-1"},
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
    }
    rules.enqueue_event(event)
    claimed = rules.claim_event(tenant_id="tenant-1", event_id="event-1")
    assert claimed is not None
    rules.complete_event(
        claimed["inbox_id"], claimed["lease_token"], state_revision=0,
        commands=[{
            "id": "event-1:canonical-reply:0", "type": "send_message", "text": "报价",
            "quote_record_id": "record-1", "dedupe_key": "reply:event-1:0",
        }],
    )
    command = rules.claim_commands(limit=1)[0]
    client = TestClient(create_app(
        service=None, quote_record_store=quote_store, rules_first_store=rules,
        rules_first_runtime=CommandResultRuntime(rules),
    ))

    response = client.post(
        f"/api/wanda-ai-v2/plugin/commands/{command['command_id']}/result",
        json={"lease_token": command["lease_token"], "result": {"status": "succeeded", "sentMessageId": "out-1"}},
        headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
    )

    assert response.status_code == 200
    delivered = quote_store.get_record(tenant_id="tenant-1", record_id="record-1")
    assert delivered is not None
    assert delivered["delivery_state"] == "delivered"
    assert delivered["delivery_message_id"] == "out-1"


def test_mark_delivered_does_not_match_event_or_quote_id(tmp_path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    store.save({
        "record_id": "record-1",
        "quote_id": "quote-1",
        "event_id": "event-1",
        "tenant_id": "tenant-1",
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
        "status": "succeeded",
        "created_at": datetime(2026, 9, 10, tzinfo=timezone.utc).isoformat(),
    })

    assert store.mark_delivered(
        tenant_id="tenant-1", record_id="event-1",
        delivered_at=datetime(2026, 9, 10, tzinfo=timezone.utc), message_id="msg-1",
    ) is None
    assert store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-1",
        delivered_at=datetime(2026, 9, 10, tzinfo=timezone.utc), message_id="msg-1",
    ) is None

    delivered = store.mark_delivered(
        tenant_id="tenant-1", record_id="record-1",
        delivered_at=datetime(2026, 9, 10, tzinfo=timezone.utc), message_id="msg-1",
    )
    assert delivered is not None
    assert delivered["delivery_state"] == "delivered"
