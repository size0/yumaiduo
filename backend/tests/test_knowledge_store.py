from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge_store import KnowledgeStore


def test_seed_contains_user_faq_catalog_and_only_enabled_entries_reach_prompt(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.json")
    current = store.current()

    assert len(current.entries) == 16
    assert any(item.title == "退改退款投诉和订单异常" for item in current.entries)
    disabled = current.entries[0].model_copy(update={"enabled": False})
    store.save([disabled, *current.entries[1:]])

    active = store.active_for_prompt()
    assert len(active) == 15
    assert disabled.id not in {item.id for item in active}


def test_knowledge_entry_can_be_edited_and_deleted(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.json")
    created = store.create({
        "title": "测试问题", "category": "常见问题", "common_questions": "怎么测试？",
        "reply_guidance": "请稍候。", "handling_rules": "无法确认时转人工。",
    })
    updated = store.update(created.id, {"enabled": False, "category": "异常处理"})
    assert updated.enabled is False
    assert updated.category == "异常处理"

    store.delete(created.id)
    with pytest.raises(KeyError):
        store.delete(created.id)
