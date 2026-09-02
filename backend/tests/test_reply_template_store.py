from __future__ import annotations

from pathlib import Path

import pytest

from app.reply_template_store import ReplyTemplates, ReplyTemplateStore, render_template


def test_template_reader_ignores_forward_compatible_unknown_top_level_keys() -> None:
    templates = ReplyTemplates.model_validate({
        "guidance_template": "请发送截图。",
        "future_rule_catalog": {"version": 2},
    })

    assert templates.guidance_template == "请发送截图。"
    assert not hasattr(templates, "future_rule_catalog")


def test_liangpiao_fulfillment_templates_are_configurable() -> None:
    templates = ReplyTemplates(
        liangpiao_ticketed_template="出票成功：{取票码}\n{取票链接}",
        liangpiao_failed_template="出票失败：{失败原因}",
    )
    assert templates.liangpiao_ticketed_template == "出票成功：{取票码}\n{取票链接}"
    assert templates.liangpiao_failed_template == "出票失败：{失败原因}"


def test_reply_templates_use_editable_chinese_variables_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "reply-templates.json"
    store = ReplyTemplateStore(path)
    current = store.current()

    assert "正在识别" in current.recognition_waiting_template
    assert "{失败原因}" in current.recognition_failure_other_template
    assert "{影片}" in current.recognition_template
    values = current.model_dump()
    values["recognition_template"] = "{城市} | {影院}\n{影片}\n{报价单价}一张"
    values["exact_quote_template"] = "{城市}|{影院}|{影片}|{日期}|{场次}\n{逐座报价}"
    saved_common = store.save(values)
    assert saved_common.recognition_template.endswith("{报价单价}一张")
    assert saved_common.exact_quote_template.startswith("{城市}|{影院}")
    assert "{城市}" in current.recognition_template
    assert "{影院}" in current.recognition_template
    assert "{报价金额}" in current.quote_above_fan_price_template
    assert "{粉丝自购价}" in current.quote_above_fan_price_template
    assert "支付成功" in current.payment_success_pending_ticket_template
    assert "{报价金额}" in current.order_pending_with_quote_template
    assert "报价记录" in current.order_pending_without_quote_template
    assert "{订单金额}" in current.price_change_confirmation_template
    assert "{报价金额}" in current.post_order_recognition_reprice_template
    assert "{失败原因}" in current.price_change_failure_template
    assert "{不可选座位}" in current.same_type_unavailable_template
    assert "{同类型参考价}" in current.same_type_unavailable_template

    saved = store.save({
        **store.current().model_dump(),
        "quote_unavailable_template": "暂时无法报价：{失败原因}\n请重新发送完整截图。",
        "same_type_unavailable_template": "换座：{不可选座位}，参考{同类型参考价}元/张，共{张数}张{同类型参考总价}元。",
    })

    assert saved.revision == 2
    loaded = ReplyTemplateStore(path).current()
    assert loaded.quote_unavailable_template.startswith("暂时无法报价")
    assert loaded.same_type_unavailable_template.startswith("换座：")


def test_render_template_preserves_intentional_blank_lines_between_sections() -> None:
    template = (
        "※{城市} | {影院}\n"
        "影片：{影片}\n"
        "日期：{日期}\n"
        "场次：{场次}\n\n"
        "{报价单价}一张合计 {报价合计}元\n"
        "座位以实时可售和最终出票为准\n\n"
        "请核对城市、影院和场次是否一致。"
    )

    rendered = render_template(template, {
        "城市": "合肥", "影院": "合肥天鹅湖万达广场店", "影片": "奥德赛",
        "日期": "08月27日（周四）", "场次": "19:00–21:52",
        "报价单价": "¥67.50", "报价合计": "¥67.50",
    })

    assert rendered == (
        "※合肥 | 合肥天鹅湖万达广场店\n"
        "影片：奥德赛\n"
        "日期：08月27日（周四）\n"
        "场次：19:00–21:52\n\n"
        "¥67.50一张合计 ¥67.50元\n"
        "座位以实时可售和最终出票为准\n\n"
        "请核对城市、影院和场次是否一致。"
    )


def test_legacy_wplus_marker_templates_migrate_to_concise_action(tmp_path: Path) -> None:
    path = tmp_path / "reply-templates.json"
    path.write_text(ReplyTemplates().model_copy(update={
        "wplus_marker_confirmation_template": (
            "这张图按W+座位处理，请确认图片中是否已经用画笔圈出了座位位置？"
            "如果没有标记，请圈好后重新发送截图，人工会按照标记的位置出票。"
        ),
        "wplus_marker_missing_template": (
            "好的，请用画笔圈好想要的座位位置后重新发送截图，人工会按照标记的位置出票。"
        ),
    }).model_dump_json(), encoding="utf-8")

    current = ReplyTemplateStore(path).current()

    expected = "请把需要出票的位置在座位图上圈好后，重新发送一张标记好的截图给我。"
    assert current.wplus_marker_confirmation_template == expected
    assert current.wplus_marker_missing_template == expected
    assert "不能直接选择" not in expected


def test_legacy_ticket_count_prompt_is_shortened_to_a_direct_question(tmp_path: Path) -> None:
    path = tmp_path / "reply-templates.json"
    path.write_text(ReplyTemplates().model_copy(update={
        "quote_ticket_count_request_template": (
            "好的，请先告诉我需要几张；确认数量后我再引导您提交订单。"
        ),
    }).model_dump_json(), encoding="utf-8")

    current = ReplyTemplateStore(path).current()

    assert current.quote_ticket_count_request_template == "请问需要几张？"


def test_legacy_confirmation_phrase_is_replaced_without_requiring_magic_words(tmp_path: Path) -> None:
    path = tmp_path / "reply-templates.json"
    path.write_text(
        ReplyTemplates(
            quote_confirmation_clarify_template=(
                "为避免误改价，请明确回复“确认报价”，或按当前报价张数直接拍下订单。"
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )

    current = ReplyTemplateStore(path).current()

    assert "确认报价" not in current.quote_confirmation_clarify_template
    assert "需要几张" in current.quote_confirmation_clarify_template
    assert "先不要付款" in current.quote_confirmation_clarify_template


def test_legacy_listing_quantity_instructions_migrate_to_quantity_independent_order_submission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reply-templates.json"
    old_quantity_guidance = (
        "已确认需要{张数}张，{报价单价}一张，合计{报价合计}元。\n"
        "{座位说明}\n"
        "请提交{张数}张订单，拍下后先不要付款；系统会将订单金额调整为{报价合计}元，"
        "收到“改价已完成，可以付款”的通知后再付款。"
    )
    path.write_text(ReplyTemplates(
        quote_confirmation_clarify_template=(
            "请直接告诉我需要几张；确认数量后可按当前张数拍下订单，拍下后先不要付款。"
        ),
        quote_quantity_order_guidance_template=old_quantity_guidance,
        order_submit_unpaid_template=(
            "请按{张数}张提交订单，拍下后先不要付款；"
            "收到“改价已完成，可以付款”的通知后再付款。"
        ),
    ).model_dump_json(), encoding="utf-8")

    current = ReplyTemplateStore(path).current()

    assert "直接提交订单" in current.quote_confirmation_clarify_template
    assert "请提交{张数}张订单" not in current.quote_quantity_order_guidance_template
    assert "{下单引导}" in current.quote_quantity_order_guidance_template
    assert "请按{张数}张提交订单" not in current.order_submit_unpaid_template


def test_transaction_flow_templates_are_saved_with_the_reply_catalog(tmp_path: Path) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")
    saved = store.save({
        "order_submit_unpaid_template": "请拍{张数}张，暂时别付款。",
        "quote_quantity_order_guidance_template": (
            "{报价单价}一张合计{报价合计}元，共{张数}张。\n{座位说明}\n{下单引导}"
        ),
        "quote_quantity_marked_seat_template": "后台图片位置说明。",
        "quote_quantity_flexible_seat_template": "后台实时座位说明。",
        "quote_quantity_default_seat_template": "后台通用座位说明。",
        "payment_manual_review_template": "已付款但金额待人工核验，暂停出票。",
    })

    assert saved.order_submit_unpaid_template == "请拍{张数}张，暂时别付款。"
    assert "{报价合计}" in saved.quote_quantity_order_guidance_template
    assert saved.quote_quantity_default_seat_template == "后台通用座位说明。"
    assert store.current().payment_manual_review_template == "已付款但金额待人工核验，暂停出票。"


def test_order_detected_hold_payment_template_can_be_blank_to_disable_that_intermediate_reply(
    tmp_path: Path,
) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")

    saved = store.save({"order_detected_hold_payment_template": ""})

    assert saved.order_detected_hold_payment_template == ""
    assert store.current().order_detected_hold_payment_template == ""


def test_hold_template_rejects_payment_instruction_without_waiting_guard(tmp_path: Path) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")

    with pytest.raises(
        ValueError,
        match="required_safety_phrase_missing:order_detected_hold_payment_template:等待改价期间",
    ):
        store.save({
            "order_detected_hold_payment_template": (
                "特惠票付款后不支持退票或改签，请核对影院、场次、张数和金额无误后在闲鱼订单内付款。"
            ),
        })


def test_hold_template_can_include_final_payment_warning_while_still_blocking_payment(
    tmp_path: Path,
) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")
    template = (
        "已看到订单，正在核验并改价；完成前请勿付款。"
        "特惠票付款后不支持退票或改签，改价完成并核对无误后再在闲鱼订单内付款。"
    )

    saved = store.save({"order_detected_hold_payment_template": template})

    assert saved.order_detected_hold_payment_template == template


def test_custom_keyword_replies_persist_and_partial_template_updates_preserve_them(tmp_path: Path) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")
    saved = store.save({
        "keyword_replies": [{
            "id": "wplus", "keywords": ["w+怎么买", "W+代订"],
            "match_mode": "contains", "reply": "支持万达W+座位代订，请发送场次截图。",
            "enabled": True, "priority": 200,
        }],
    })

    updated = store.save({"guidance_template": "请发送截图。"})

    assert saved.keyword_replies[0].keywords == ["w+怎么买", "W+代订"]
    assert updated.keyword_replies[0].reply.startswith("支持万达W+")
    assert updated.guidance_template == "请发送截图。"


def test_custom_keyword_replies_cannot_claim_a_price(tmp_path: Path) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")
    with pytest.raises(ValueError, match="keyword_reply_price_claim_unsupported"):
        store.save({
            "keyword_replies": [{
                "id": "unsafe-price", "keywords": ["多少钱"],
                "reply": "固定价格29.9元", "match_mode": "exact",
            }],
        })


def test_reply_templates_reject_unknown_or_english_variables(tmp_path: Path) -> None:
    store = ReplyTemplateStore(tmp_path / "reply-templates.json")
    values = store.current().model_dump()
    values["recognition_template"] = "Movie: {movie_name} {未知变量}"

    with pytest.raises(ValueError, match=r"unsupported_template_variable: 识图主体不支持变量.*movie_name.*未知变量"):
        store.save(values)
