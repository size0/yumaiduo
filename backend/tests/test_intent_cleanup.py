from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.models import MovieImageInfo, RealQuote
from app.plugin_automation import PluginAutomation


@dataclass
class _NeverRecognizer:
    calls: int = 0

    async def recognize(self, *_args: Any, **_kwargs: Any) -> MovieImageInfo:
        self.calls += 1
        raise AssertionError("text-only regression must not invoke image recognition")


@dataclass
class _NeverQuoter:
    calls: int = 0

    async def quote(self, *_args: Any, **_kwargs: Any) -> RealQuote:
        self.calls += 1
        raise AssertionError("text-only regression must not invoke quoting")


class _CapturingChat:
    def __init__(self, reply: str = "我来帮您解答这个问题。") -> None:
        self.reply_text = reply
        self.calls: list[tuple[str, str]] = []

    def sync_platform_history(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def reply(self, text: str, conversation_id: str) -> str:
        self.calls.append((text, conversation_id))
        return self.reply_text


def _text_event(content: str, *, seller_quote: bool = False) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    if seller_quote:
        messages.append({
            "direction": "seller",
            "messageType": 1,
            "content": "中间区域报价 74 元/张，共 148 元",
            "messageId": "seller-quote",
            "sentAtMs": 1_787_579_998_000,
        })
    messages.append({
        "direction": "inbound",
        "messageType": 1,
        "content": content,
        "messageId": "buyer-current",
        "sentAtMs": 1_787_579_999_000,
        "imageUrls": [],
    })
    return {
        "envelope": {
            "id": "event-intent-cleanup",
            "tenantId": "tenant-1",
            "event": "im.message.received",
            "timestamp": 1_787_580_000_000,
            "payload": {
                "messageType": 1,
                "remoteMessageId": "buyer-current",
                "content": content,
                "imageUrls": [],
                "accountUnb": "shop-1",
                "chatId": "chat-1",
                "peerUnb": "buyer-1",
            },
        },
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "order": None,
        "recent_messages": messages,
    }


def _automation(chat: _CapturingChat, recognizer: _NeverRecognizer | None = None) -> PluginAutomation:
    return PluginAutomation(
        recognizer or _NeverRecognizer(),
        _NeverQuoter(),
        mode="auto",
        image_loader=None,
        chat_service=chat,
    )


@pytest.mark.asyncio
async def test_ambiguous_seat_question_is_not_treated_as_quote_confirmation() -> None:
    """“可以不” must reach AI instead of a purchase-guide rule reply."""
    chat = _CapturingChat("可以帮您确认座位信息，请稍等。")
    result = await _automation(chat).process_event(
        _text_event("这两个蓝色的可以不", seller_quote=True),
    )

    decision = result["decision"]
    assert chat.calls == [("这两个蓝色的可以不", "tenant-1:shop-1:chat-1")]
    assert decision["reason"] == "automatic_reply_ready"
    actions = decision["actions"]
    assert len(actions) == 1
    assert actions[0]["text"] == "可以帮您确认座位信息，请稍等。"
    assert actions[0].get("rule_governed") is not True
    assert not any(action["type"] in {"change_order_price", "create_order", "submit_order"} for action in actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["好的", "可以", "嗯嗯", "确认"])
async def test_bare_acknowledgements_do_not_trigger_quote_template(
    message: str,
) -> None:
    """Bare acknowledgements are not transaction authorization, even after a quote."""
    chat = _CapturingChat("如果需要下单，请按平台订单流程操作。")
    result = await _automation(chat).process_event(
        _text_event(message, seller_quote=True),
    )

    decision = result["decision"]
    assert chat.calls == [(message, "tenant-1:shop-1:chat-1")]
    assert decision["reason"] == "automatic_reply_ready"
    assert decision["actions"][0]["text"] == "如果需要下单，请按平台订单流程操作。"
    assert decision["actions"][0].get("rule_governed") is not True


@pytest.mark.asyncio
async def test_confirmation_with_count_without_quote_cannot_create_trade_action() -> None:
    """A confirmation-looking text has no write authority without a durable quote."""
    chat = _CapturingChat("请先发送选座截图，我们再为您核价。")
    result = await _automation(chat).process_event(_text_event("确认2张"))

    decision = result["decision"]
    assert chat.calls == [("确认2张", "tenant-1:shop-1:chat-1")]
    assert decision["reason"] == "automatic_reply_ready"
    assert not any(
        action["type"] in {"change_order_price", "create_order", "submit_order"}
        for action in decision["actions"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "多少钱一张", "怎么买", "有优惠吗",
    "城市：北京；影院：北京怀柔万达广场；影片：奥德赛；日期：明天；场次：18:55；张数：2张",
])
async def test_all_buyer_text_queries_enter_agent_without_local_quote_routing(message: str) -> None:
    chat = _CapturingChat("我先帮您核对当前信息。")
    result = await _automation(chat).process_event(_text_event(message))

    assert chat.calls == [(message, "tenant-1:shop-1:chat-1")]
    assert result["decision"]["reason"] == "automatic_reply_ready"
    assert result["decision"]["ai_called"] is True
    assert result["decision"]["reply_route"] == "agent"
    assert result["decision"]["actions"][0]["text"] == "我先帮您核对当前信息。"


@pytest.mark.asyncio
async def test_non_transaction_faq_is_sent_to_chat_ai() -> None:
    chat = _CapturingChat("我们每天都会营业，具体以店铺页面为准。")
    result = await _automation(chat).process_event(_text_event("你们几点营业？"))

    assert result["decision"]["reason"] == "automatic_reply_ready"
    assert chat.calls == [("你们几点营业？", "tenant-1:shop-1:chat-1")]
    assert result["decision"]["actions"][0]["text"] == "我们每天都会营业，具体以店铺页面为准。"


@pytest.mark.asyncio
async def test_order_event_remains_rule_guarded_and_never_enters_ai() -> None:
    chat = _CapturingChat()
    body = _text_event("确认2张")
    body["envelope"] = {
        **body["envelope"],
        "event": "order.created",
        "payload": {
            "orderId": "order-1",
            "accountUnb": "shop-1",
            "chatId": "chat-1",
            "peerUnb": "buyer-1",
        },
    }
    body["order"] = {
        "orderId": "order-1",
        "tenantId": "tenant-1",
        "accountUnb": "shop-1",
        "buyerUnb": "buyer-1",
        "chatId": "chat-1",
        "orderStatus": "created",
        "payment": 1_000,
    }

    result = await _automation(chat).process_event(body)

    assert chat.calls == []
    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"
