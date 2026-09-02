from __future__ import annotations

from app.rules_first_store import RulesFirstStore


def test_agent_tool_audit_is_encrypted_and_tenant_scoped(tmp_path) -> None:
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    saved = store.record_agent_tool_call({
        "call_id": "call-1", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "buyer_id": "buyer-a", "chat_id": "chat-a", "event_id": "event-a",
        "tool_name": "get_quote", "round_index": 1, "status": "succeeded",
        "arguments": {"city": "深圳"},
        "result": {"ok": True, "quote_id": "quote-1"},
    })

    assert saved["call_id"] == "call-1"
    records = store.list_agent_tool_calls("tenant-a")
    assert records[0]["tool_name"] == "get_quote"
    assert records[0]["result"]["quote_id"] == "quote-1"
    assert store.list_agent_tool_calls("tenant-b") == []


def test_agent_tool_audit_duplicate_call_is_idempotent(tmp_path) -> None:
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    payload = {
        "call_id": "call-1", "tenant_id": "tenant-a", "shop_id": "shop-a",
        "buyer_id": "buyer-a", "chat_id": "chat-a", "event_id": "event-a",
        "tool_name": "get_order_state", "status": "failed", "result": {"error": "x"},
    }
    first = store.record_agent_tool_call(payload)
    second = store.record_agent_tool_call({**payload, "status": "succeeded"})

    assert first == second
    assert len(store.list_agent_tool_calls("tenant-a")) == 1
