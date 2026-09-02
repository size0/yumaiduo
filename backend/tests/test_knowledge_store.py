from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge_store import KnowledgeStore


def test_seed_contains_user_faq_catalog_and_only_enabled_entries_reach_prompt(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.json")
    current = store.current()

    assert len(current.entries) == 18
    assert any(item.title == "退改退款投诉和订单异常" for item in current.entries)
    assert any(item.title == "W+未标记位置只要求重发截图" for item in current.entries)
    assert any(item.title == "多张图片场次冲突处理" for item in current.entries)
    disabled = current.entries[0].model_copy(update={"enabled": False})
    store.save([disabled, *current.entries[1:]])

    active = store.active_for_prompt()
    assert len(active) == 17
    assert disabled.id not in {item.id for item in active}


def test_seed_does_not_reintroduce_keyword_confirmation_or_blanket_handoff(tmp_path: Path) -> None:
    entries = KnowledgeStore(tmp_path / "knowledge.json").current().entries
    combined_rules = "\n".join(item.handling_rules for item in entries)

    assert "必须回复“正确”" not in combined_rules
    assert "未收到“正确”" not in combined_rules
    assert "一律转人工" not in combined_rules
    assert "接口失败" not in combined_rules or "重试" in combined_rules


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


def test_active_for_prompt_filters_entries_by_conversation_stage(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.json")

    quotation = store.active_for_prompt("quotation")
    assert quotation
    assert all(item.category in {"报价规则", "截图识别", "异常处理", "常见问题"} for item in quotation)
    assert not any(item.category == "售后人工" for item in quotation)

    after_sales = store.active_for_prompt("after_sales")
    assert after_sales
    assert all(item.category in {"售后人工", "异常处理", "订单出票"} for item in after_sales)

    # Unknown stages fail open to the explicit general catalogue rather than
    # silently dropping all safety guidance.
    assert len(store.active_for_prompt("unknown-stage")) == len(store.active_for_prompt("general"))


@pytest.mark.parametrize(("runtime_stage", "expected_categories"), [
    ("order_pending", {"下单确认", "订单出票", "异常处理", "报价规则"}),
    ("payment", {"订单出票", "异常处理", "常见问题"}),
    ("shipping_refund", {"售后人工", "异常处理", "订单出票"}),
])
def test_active_for_prompt_maps_runtime_business_stages(
    tmp_path: Path, runtime_stage: str, expected_categories: set[str],
) -> None:
    store = KnowledgeStore(tmp_path / "knowledge.json")

    active = store.active_for_prompt(runtime_stage)

    assert active
    assert {entry.category for entry in active}.issubset(expected_categories)
