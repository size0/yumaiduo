from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.knowledge_base_store import KnowledgeBaseStore
from app.main import create_app


SAFE_CANDIDATE = {
    "topic": "图片要求",
    "question_pattern": "买家询问核价前需要提供什么资料",
    "response_guidance": "说明需要完整选座页截图和明确张数。",
    "example_reply": "请发送完整选座页截图并说明需要的张数。",
    "outcome_signal": "buyer_progressed",
    "confidence": 0.91,
}


def test_conversation_experience_is_tenant_scoped_deduplicated_and_never_auto_enabled(tmp_path: Path) -> None:
    store = KnowledgeBaseStore(tmp_path / "knowledge.json")
    created = store.record_experience("tenant-a", SAFE_CANDIDATE)
    repeated = store.record_experience("tenant-a", SAFE_CANDIDATE)
    other_tenant = store.record_experience("tenant-b", SAFE_CANDIDATE)

    assert created["status"] == "draft"
    assert created["enabled"] is False
    assert created["source"] == "conversation_experience"
    assert repeated["id"] == created["id"]
    assert repeated["evidence_count"] == 2
    assert other_tenant["id"] != created["id"]
    assert len(store.list("tenant-a")) == 1
    assert len(store.list("tenant-b")) == 1
    assert store.active("reply", "tenant-a") == []

    approved = store.update(created["id"], {"status": "approved", "enabled": True}, tenant_id="tenant-a")
    assert approved["enabled"] is True
    assert len(store.active("reply", "tenant-a")) == 1
    assert store.active("reply", "tenant-b") == []


def test_agent_knowledge_is_scene_scoped_bounded_and_general_rules_remain_available(tmp_path: Path) -> None:
    store = KnowledgeBaseStore(tmp_path / "knowledge.json")
    entries = [
        store.create({"category": "reply", "scene": "general", "title": "通用语气", "content": "回复保持简短礼貌。", "sort_order": 10}, "tenant-a"),
        store.create({"category": "reply", "scene": "intake", "title": "截图要求", "content": "询价时请买家提供完整选座页。", "sort_order": 1}, "tenant-a"),
        store.create({"category": "reply", "scene": "order", "title": "订单说明", "content": "订单问题先读取权威订单状态。", "sort_order": 1}, "tenant-a"),
        store.create({"category": "reply", "scene": "aftersale", "title": "售后说明", "content": "售后争议转人工处理。", "sort_order": 1}, "tenant-a"),
    ]
    for entry in entries:
        store.update(str(entry["id"]), {"status": "approved", "enabled": True}, "tenant-a")

    assert store.active_for_agent("intake", "tenant-a") == ["询价时请买家提供完整选座页。", "回复保持简短礼貌。"]
    assert store.active_for_agent("order", "tenant-a") == ["订单问题先读取权威订单状态。", "回复保持简短礼貌。"]
    assert "售后争议转人工处理。" not in store.active_for_agent("intake", "tenant-a")
    assert len(store.active_for_agent("intake", "tenant-a", max_rules=1)) == 1


def test_legacy_knowledge_entries_default_to_general_scene(tmp_path: Path) -> None:
    store = KnowledgeBaseStore(tmp_path / "knowledge.json")
    entry = store.create({"category": "reply", "title": "旧条目", "content": "旧条目仍可作为通用知识。", "sort_order": 0}, "tenant-a")
    assert entry["scene"] == "general"


def test_operator_correction_stays_tenant_scoped_and_only_enters_active_knowledge_after_review(tmp_path: Path) -> None:
    store = KnowledgeBaseStore(tmp_path / "knowledge.json")
    payload = {
        "conversation_id": "tenant-a:shop:chat:buyer", "run_id": "active:event-1", "event_id": "tenant-a:event-1",
        "buyer_message": "这个场次还能买吗", "assistant_reply": "请转人工", "corrected_answer": "可以先查询当前场次实时状态。",
        "handling_guidance": "买家追问已有场次时，结合当前会话事实回答，不重复索要已经提供的信息。",
        "tool_trajectory": [{"tool": "resolve_showtime", "status": "completed"}],
        "versions": {"model": "reasoning-model", "prompt": "p1", "tools": "t1"},
    }
    correction = store.record_correction("tenant-a", payload)
    assert correction["status"] == "draft"
    assert store.active("reply", "tenant-a") == []
    assert store.list_corrections("tenant-b") == []

    approved = store.review_correction("tenant-a", str(correction["id"]), {"status": "approved", "scene": "quote_followup", "reviewer": "operator-1", "revision": 1})
    knowledge_entry_id = approved["knowledge_entry_id"]
    assert knowledge_entry_id
    assert store.active("reply", "tenant-a") == [payload["handling_guidance"]]
    assert store.active("reply", "tenant-b") == []

    rejected = store.review_correction("tenant-a", str(correction["id"]), {"status": "rejected", "scene": "quote_followup", "reviewer": "operator-2", "revision": 2})
    assert rejected["knowledge_entry_id"] == knowledge_entry_id
    assert store.active("reply", "tenant-a") == []
    reapproved = store.review_correction("tenant-a", str(correction["id"]), {"status": "approved", "scene": "order", "reviewer": "operator-3", "revision": 3})
    assert reapproved["knowledge_entry_id"] == knowledge_entry_id
    assert len([entry for entry in store.list("tenant-a") if entry.get("source_correction_id") == correction["id"]]) == 1
    assert store.active("reply", "tenant-a") == [payload["handling_guidance"]]


def test_conversation_experience_endpoint_requires_bridge_auth_and_rejects_sensitive_content(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_PLUGIN_BRIDGE_KEY", "bridge-secret")
    monkeypatch.setenv("WANDA_KNOWLEDGE_BASE_PATH", str(tmp_path / "knowledge.json"))
    client = TestClient(create_app())
    payload = {"tenant_id": "tenant-a", "event_id": "event-1", "candidate": SAFE_CANDIDATE}

    assert client.post("/api/xianyu-plugin/bridge/conversation-experiences", json=payload).status_code == 401
    response = client.post(
        "/api/xianyu-plugin/bridge/conversation-experiences",
        headers={"X-Plugin-Bridge-Key": "bridge-secret", "X-Yumaiduo-Tenant-Id": "tenant-a"},
        json=payload,
    )
    assert response.status_code == 201
    assert response.json()["status"] == "draft_created"
    assert response.json()["entry"]["enabled"] is False

    unsafe = {**SAFE_CANDIDATE, "example_reply": "已经改价，可以付款"}
    rejected = client.post(
        "/api/xianyu-plugin/bridge/conversation-experiences",
        headers={"X-Plugin-Bridge-Key": "bridge-secret", "X-Yumaiduo-Tenant-Id": "tenant-a"},
        json={**payload, "candidate": unsafe},
    )
    assert rejected.status_code == 422
