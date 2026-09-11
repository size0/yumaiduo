from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from itertools import product
import json
import re
from typing import Any
from pathlib import Path


@dataclass(frozen=True)
class CanonicalUtteranceCase:
    case_id: str
    scene: str
    utterance: str
    expected_behavior: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "scene": self.scene,
            "utterance": self.utterance,
            "expected_behavior": self.expected_behavior,
            "source": self.source,
        }


_SCENE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "scene": "price_request",
        "expected_behavior": "ask_for_current_quote_or_screenshot",
        "source": "backend/tests/test_canonical_conversation_agent.py; backend/tests/test_chat_ai.py; RECENT_HUMAN_CONVERSATION_SUMMARY.md",
        "stems": (
            "价钱多少",
            "这个多少钱",
            "前面一排的会员座多少钱",
            "多少钱一张",
            "价格多少",
        ),
        "suffixes": ("", "？", "呀", "呢"),
    },
    {
        "scene": "count_followup",
        "expected_behavior": "ask_for_ticket_count_or_confirm_count",
        "source": "backend/tests/test_canonical_conversation_agent.py; backend/tests/test_chat_ai.py",
        "stems": (
            "2张",
            "买2张",
            "两人",
            "需要两张",
            "一共4张",
        ),
        "suffixes": ("", "可以吗", "行吗", "呀"),
    },
    {
        "scene": "seat_correction",
        "expected_behavior": "preserve_current_context_and_refine_seat_choice",
        "source": "backend/tests/test_canonical_conversation_agent.py; backend/tests/test_chat_ai.py",
        "stems": (
            "左边那两个",
            "换一下相邻位置",
            "右边靠中间的",
            "再往后一点",
            "那排中间",
        ),
        "suffixes": ("", "呢", "可以吗", "怎么办"),
    },
    {
        "scene": "showtime_shift",
        "expected_behavior": "ask_for_later_showtime_or_show_options",
        "source": "backend/tests/test_canonical_conversation_agent.py",
        "stems": (
            "晚一点",
            "有没有更晚的场次",
            "换晚场",
            "再晚一点",
            "之后那场",
        ),
        "suffixes": ("", "呢", "可以吗", "还有吗"),
    },
    {
        "scene": "expired_context",
        "expected_behavior": "require_fresh_confirmation_for_expired_context",
        "source": "backend/tests/test_canonical_conversation_agent.py",
        "stems": (
            "还是昨天那个",
            "昨天那场还在吗",
            "前天那张还能用吗",
            "还是之前那场",
            "上次那个",
        ),
        "suffixes": ("", "呢", "可以吗", "还行吗"),
    },
    {
        "scene": "greeting",
        "expected_behavior": "acknowledge_and_open_conversation",
        "source": "backend/tests/test_chat_ai.py; logs/app.log",
        "stems": (
            "在么",
            "在吗",
            "有人吗",
            "你好",
            "在不在",
        ),
        "suffixes": ("", "呀", "呢", "哈"),
    },
    {
        "scene": "gratitude",
        "expected_behavior": "close_polite_followup_without_new_assertions",
        "source": "backend/tests/test_chat_ai.py; logs/app.log",
        "stems": (
            "好的",
            "谢谢",
            "收到",
            "明白了",
            "好哒",
        ),
        "suffixes": ("", "哈", "呢", "啦"),
    },
    {
        "scene": "paid_status",
        "expected_behavior": "read_authoritative_order_state_before_replying",
        "source": "backend/tests/test_chat_ai.py; logs/app.log",
        "stems": (
            "我已付款",
            "我已经付了",
            "钱已经付了",
            "已经支付成功",
            "付完了",
        ),
        "suffixes": ("", "，出票了吗", "，现在什么状态", "，还在吗"),
    },
    {
        "scene": "wplus_purchase",
        "expected_behavior": "ground_wplus_buying_question_in_authoritative_quote_or_recognition",
        "source": "backend/tests/test_chat_ai.py; CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "stems": (
            "W+座位能代买吗",
            "可以买W+吗",
            "会员座能买吗",
            "Wplus能买吗",
            "会员座可以买么",
        ),
        "suffixes": ("", "呀", "呢", "吗"),
    },
    {
        "scene": "manual_mark",
        "expected_behavior": "treat_hand_drawn_marks_as_hints_not_facts",
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md; CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "stems": (
            "中间那几个灰色的",
            "圈起来的能买吗",
            "箭头指的座位能买吗",
            "手绘圈的那个位置",
            "红圈那几个",
        ),
        "suffixes": ("", "呢", "可以吗", "能买吗"),
    },
    {
        "scene": "reference_only",
        "expected_behavior": "treat_reference_prices_as_reference_only",
        "source": "backend/tests/test_canonical_conversation_agent.py; CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "stems": (
            "老板刚才说48",
            "同类型还能买吗",
            "有没有同类型参考价",
            "换票还能按参考价吗",
            "参考价是多少",
        ),
        "suffixes": ("", "呀", "呢", "可以吗"),
    },
    {
        "scene": "mixed_context",
        "expected_behavior": "merge_parallel_context_and_return_one_answer",
        "source": "backend/tests/fixtures/agent_canonical_legacy_context.json; docs/overnight-20260905/07-e2e.md",
        "stems": (
            "还可以买吗",
            "这两张图帮我看下",
            "刚才那张图还能买吗",
            "麻烦帮我看一下",
            "这场还能不能买",
        ),
        "suffixes": ("", "呢", "呀", "吗"),
    },
)


def _normalized(text: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;:：]+", "", str(text or "").strip()).lower()


def classify_canonical_utterance(utterance: str) -> str:
    text = _normalized(utterance)
    if not text:
        return "unknown"
    if any(marker in text for marker in ("已付款", "已支付", "支付成功", "付款成功", "出票了吗", "出票", "付了", "付完")):
        return "paid_status"
    if any(marker in text for marker in ("刚才说", "老板", "参考价", "同类型", "换票", "上次价格")):
        return "reference_only"
    if any(marker in text for marker in ("灰色", "圈", "箭头", "手绘", "标记")):
        return "manual_mark"
    if any(marker in text for marker in ("w+", "wplus", "会员座")) and any(marker in text for marker in ("买", "代买", "能买吗", "能买", "购买")):
        return "wplus_purchase"
    if any(marker in text for marker in ("晚一点", "更晚", "晚场", "再晚", "之后那场", "换晚场")):
        return "showtime_shift"
    if any(marker in text for marker in ("昨天", "前天", "之前", "过期", "上次那个")):
        return "expired_context"
    if any(marker in text for marker in ("谢谢", "收到", "明白了", "好的", "好哒", "好啦")):
        return "gratitude"
    if any(marker in text for marker in ("在么", "在吗", "有人吗", "在不在", "你好")):
        return "greeting"
    if any(marker in text for marker in ("左边那", "换一下相邻位置", "右边靠中间", "再往后一点", "那排中间")):
        return "seat_correction"
    if any(marker in text for marker in ("2张", "买2张", "两人", "需要两张", "一共4张", "几张")):
        return "count_followup"
    if any(marker in text for marker in ("多少钱", "价钱", "价格", "多钱", "怎么卖")):
        return "price_request"
    return "mixed_context"


def build_canonical_utterance_matrix() -> list[dict[str, Any]]:
    cases: list[CanonicalUtteranceCase] = []
    for spec in _SCENE_SPECS:
        stems = tuple(spec["stems"])
        suffixes = tuple(spec["suffixes"])
        if len(stems) * len(suffixes) != 20:
            raise ValueError(f"scene_case_count_invalid:{spec['scene']}")
        for index, (stem, suffix) in enumerate(product(stems, suffixes), start=1):
            cases.append(CanonicalUtteranceCase(
                case_id=f"{spec['scene']}-{index:02d}",
                scene=spec["scene"],
                utterance=f"{stem}{suffix}",
                expected_behavior=spec["expected_behavior"],
                source=spec["source"],
            ))
    if len(cases) != 240:
        raise ValueError("canonical_utterance_matrix_size_invalid")
    return [case.to_dict() for case in cases]


REAL_IMAGE_URLS: tuple[str, ...] = (
    "https://img.alicdn.com/imgextra/i4/2910518506/O1CN011pvEipHzODH17k4o_!!2910518506-0-xy_chat.jpg",
    "https://img.alicdn.com/imgextra/i1/4012494370/O1CN01JQ9Rh3VCzUJ1klhQ_!!4012494370-0-xy_chat.jpg",
    "https://img.alicdn.com/imgextra/i4/2783164651/O1CN01wKBfNrhhHLB1nHnA_!!2783164651-0-xy_chat.jpg",
    "https://img.alicdn.com/imgextra/i1/919985577/O1CN01gwYToAnuGHE1nHnA_!!919985577-0-xy_chat.jpg",
    "https://img.alicdn.com/imgextra/i1/2208507772485/O1CN01HNxiOEKKvDD28JAJ_!!2208507772485-0-xy_chat.jpg",
    "https://img.alicdn.com/imgextra/i3/3327451135/O1CN01T5BLx0Nd1oE1n9zH_!!3327451135-0-xy_chat.jpg",
)


_OVERNIGHT_THEME_SPECS: tuple[dict[str, Any], ...] = (
    {
        "theme": "price_request",
        "expected_behavior": "ask_for_current_quote_or_screenshot",
        "agent_reply": "我先看实时图再核价哈",
        "openings": ("多少钱", "这个多少钱", "有便宜的吗", "W+多少"),
        "followups": ("2张", "两张", "老板刚才说48", "还有吗"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md; CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "image_url": REAL_IMAGE_URLS[0],
    },
    {
        "theme": "quantity_followup",
        "expected_behavior": "ask_for_ticket_count_or_confirm_count",
        "agent_reply": "你先告诉我张数，我好继续核价哈",
        "openings": ("2张", "两张", "3个人", "4张"),
        "followups": ("这个呢", "还是这个", "那旁边呢", "刚才那个"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
        "image_url": REAL_IMAGE_URLS[1],
    },
    {
        "theme": "exact_seat_reference",
        "expected_behavior": "preserve_current_context_and_refine_seat_choice",
        "agent_reply": "我按你圈的位置继续看",
        "openings": ("左边两个", "右边那个", "这个位置呢", "我圈好了"),
        "followups": ("还是这个", "不是这个场", "发错了", "刚才那个"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
        "image_url": REAL_IMAGE_URLS[2],
    },
    {
        "theme": "unavailable_seat",
        "expected_behavior": "ask_for_alternative_or_refresh_selection",
        "agent_reply": "这个位置我先按实时结果核，别急哈",
        "openings": ("这个位置呢", "那旁边呢", "有没有连座", "普通座呢"),
        "followups": ("还能买吗", "还剩吗", "换一个", "再看一眼"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "image_url": REAL_IMAGE_URLS[3],
    },
    {
        "theme": "same_type_reference",
        "expected_behavior": "treat_same_type_price_as_reference_only",
        "agent_reply": "同类型我只看实时事实，不看口头价哈",
        "openings": ("同类型呢", "同类型参考价", "老板说48", "有参考价吗"),
        "followups": ("这个呢", "还能按这个价吗", "还是这个", "再便宜点"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
        "image_url": REAL_IMAGE_URLS[4],
    },
    {
        "theme": "price_mismatch",
        "expected_behavior": "ground_price_comparisons_in_authoritative_quote",
        "agent_reply": "截图上的优惠只做参考，我按权威报价看",
        "openings": ("截图里不是这个价", "怎么比这个贵", "有更便宜的吗", "便宜点"),
        "followups": ("老板刚才说48", "不是这个价", "还能少点吗", "再看看"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
        "image_url": REAL_IMAGE_URLS[5],
    },
    {
        "theme": "manual_mark",
        "expected_behavior": "treat_hand_drawn_marks_as_hints_not_facts",
        "agent_reply": "圈出来的我先当偏好，不直接当事实",
        "openings": ("我圈好了", "红圈那个", "箭头指的", "手绘标记"),
        "followups": ("这个能买吧", "这两个", "左边两个", "右边那个"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md; CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
    },
    {
        "theme": "missing_city",
        "expected_behavior": "ask_for_missing_city_or_shop",
        "agent_reply": "先补城市，我再接着看影院",
        "openings": ("不是这个城市", "缺城市", "哪个城市", "补城市"),
        "followups": ("万达广场店", "这个影院呢", "发个城市", "不是万达"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
    },
    {
        "theme": "cinema_truncated",
        "expected_behavior": "ask_for_full_cinema_name_when_truncated",
        "agent_reply": "影院信息我先按完整名称核对",
        "openings": ("影院名没显示全", "截断了", "这个影院呢", "哪个万达"),
        "followups": ("发完整点", "再看一下", "补全", "刚才那个"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
    },
    {
        "theme": "show_ambiguity",
        "expected_behavior": "ask_for_show_resolution_or_latest_showtime",
        "agent_reply": "场次我先帮你确认一下哈",
        "openings": ("哪一场", "这个场次", "是不是这场", "不是这个场"),
        "followups": ("晚一点呢", "早一点", "换7点", "最晚一场"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "movie_ambiguity",
        "expected_behavior": "ask_for_full_movie_name_or_fingerprint",
        "agent_reply": "电影名我先按完整片名核对",
        "openings": ("换个电影", "不是这个片", "刚才那个片", "片名有点乱"),
        "followups": ("还能换吗", "再看看", "那个呢", "不是这个"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "time_change",
        "expected_behavior": "ask_for_alternate_showtime_or_find_later_show",
        "agent_reply": "我先找更合适的场次",
        "openings": ("晚一点呢", "早一点", "换7点", "最晚一场"),
        "followups": ("今天", "明天", "周六", "晚上"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "cinema_change",
        "expected_behavior": "ask_for_alternate_cinema_or_switch_shop",
        "agent_reply": "你要换影院我先帮你确认可选项",
        "openings": ("换个厅", "换个影院", "不是万达", "换到那边"),
        "followups": ("还能买", "有吗", "再看看", "还是这个"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
    },
    {
        "theme": "quantity_change",
        "expected_behavior": "recompute_quote_after_quantity_update",
        "agent_reply": "张数变了我再重新核一遍",
        "openings": ("改成4张", "还是2张", "张数变了", "3个人"),
        "followups": ("再算一下", "这个呢", "那两个", "还是这个"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "order_payment",
        "expected_behavior": "read_authoritative_order_state_before_replying",
        "agent_reply": "订单和付款我先按权威状态看",
        "openings": ("付款了", "还没改价", "多久出票", "没收到"),
        "followups": ("订单呢", "现在什么状态", "已经拍了", "还要等吗"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "refund_fulfillment",
        "expected_behavior": "keep_refund_and_fulfillment_facts_deterministic",
        "agent_reply": "退款和履约我先不乱答，等权威状态",
        "openings": ("可以退吗", "能退款吗", "还来得及吗", "取消可以吗"),
        "followups": ("出票没", "已经改价", "已经付了", "怎么办"),
        "source": "CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
    },
    {
        "theme": "greeting_thanks",
        "expected_behavior": "acknowledge_and_open_conversation",
        "agent_reply": "在的，发图我帮你看",
        "openings": ("你好", "在吗", "好的谢谢", "谢谢"),
        "followups": ("发图", "帮我看", "不客气", "先这样"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "rapid_messages",
        "expected_behavior": "merge_rapid_messages_without_duplicate_reply",
        "agent_reply": "连续消息我会合并看，不重复回",
        "openings": ("2张", "这个呢", "还是这个", "晚一点呢"),
        "followups": ("左边两个", "右边那个", "刚才那个", "这个位置呢"),
        "source": "RECENT_HUMAN_CONVERSATION_SUMMARY.md",
    },
    {
        "theme": "history_failure",
        "expected_behavior": "fail_closed_when_context_is_missing",
        "agent_reply": "历史缺失时我先补齐上下文，不硬猜",
        "openings": ("前文没了", "看不到历史", "上下文丢了", "刚刚那句"),
        "followups": ("再发一次", "补图", "补城市", "补场次"),
        "source": "docs/overnight-20260905/07-e2e.md",
    },
    {
        "theme": "model_failure_external_operator",
        "expected_behavior": "safe_reply_without_fallback_to_legacy",
        "agent_reply": "我这边失败了会先安全停一下，不抢答",
        "openings": ("没处理成功", "模型失败了", "别回我", "客服回复了"),
        "followups": ("重发一下", "稍等", "外部接管了", "不用回了"),
        "source": "goal-objective.md",
    },
)


def build_canonical_agent_overnight_fixture() -> dict[str, Any]:
    conversations: list[dict[str, Any]] = []
    utterances: list[dict[str, Any]] = []
    for theme_index, spec in enumerate(_OVERNIGHT_THEME_SPECS, start=1):
        openings = tuple(spec["openings"])
        followups = tuple(spec["followups"])
        for variant_index in range(4):
            conversation_id = f"overnight-{theme_index:02d}-{variant_index + 1}"
            image_url = spec.get("image_url")
            messages = [
                {
                    "turn": 1,
                    "speaker": "buyer",
                    "text": openings[variant_index],
                },
                {
                    "turn": 2,
                    "speaker": "agent",
                    "text": spec["agent_reply"],
                    "expected_behavior": spec["expected_behavior"],
                },
                {
                    "turn": 3,
                    "speaker": "buyer",
                    "text": followups[variant_index],
                },
            ]
            if image_url:
                messages[0]["image_url"] = image_url
                messages[2]["image_url"] = image_url
            conversations.append({
                "conversation_id": conversation_id,
                "theme": spec["theme"],
                "variant": variant_index + 1,
                "rounds": len(messages),
                "image_url": image_url,
                "expected_behavior": spec["expected_behavior"],
                "source": spec["source"],
                "messages": messages,
            })
            for message in messages:
                utterances.append({
                    "conversation_id": conversation_id,
                    "theme": spec["theme"],
                    "variant": variant_index + 1,
                    "turn": message["turn"],
                    "speaker": message["speaker"],
                    "text": message["text"],
                    "expected_behavior": spec["expected_behavior"] if message["speaker"] == "agent" else None,
                    "image_url": message.get("image_url"),
                    "source": spec["source"],
                })
    if len(conversations) != 80:
        raise ValueError("canonical_overnight_conversation_count_invalid")
    if len(utterances) != 240:
        raise ValueError("canonical_overnight_utterance_count_invalid")
    return {
        "fixture_version": "canonical-agent-overnight-240-001",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "seed": "goal-objective.md + RECENT_HUMAN_CONVERSATION_SUMMARY.md + CURRENT_PRODUCTION_KNOWLEDGE_BASE.md",
            "image_urls": list(REAL_IMAGE_URLS),
        },
        "summary": {
            "total_conversations": len(conversations),
            "total_utterances": len(utterances),
            "image_count": len(REAL_IMAGE_URLS),
        },
        "canonical_utterance_matrix": build_canonical_utterance_matrix(),
        "conversations": conversations,
        "utterances": utterances,
    }


def write_canonical_agent_overnight_fixture(path: Path) -> Path:
    payload = build_canonical_agent_overnight_fixture()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
