from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.canonical_conversation_agent import (
    AgentContextBuilder,
    CanonicalAgentToolBackend,
    CanonicalConversationAgent,
)


IDENTITY = {
    "tenant_id": "tenant-1",
    "shop_id": "shop-1",
    "buyer_id": "buyer-1",
    "chat_id": "chat-1",
    "purchase_context_id": "purchase-1",
}

RECOGNITION = {
    "source": "LIANGPIAO",
    "city_text": "广州",
    "cinema_text": "广州天河万达影城",
    "cinema_address": "天河路",
    "movie": "测试电影",
    "show_date": "2026-09-11",
    "start_time": "08:40",
    "hall": "IMAX厅",
    "dimension": "IMAX",
}


def body(text: str) -> dict[str, object]:
    return {
        "envelope": {
            "id": "text-13-10",
            "tenantId": IDENTITY["tenant_id"],
            "event": "im.message.received",
            "payload": {
                "accountUnb": IDENTITY["shop_id"],
                "peerUnb": IDENTITY["buyer_id"],
                "chatId": IDENTITY["chat_id"],
                "itemId": IDENTITY["purchase_context_id"],
                "remoteMessageId": "message-13-10",
                "content": text,
                "messageType": 1,
            },
        },
        "session": {
            "accountUnb": IDENTITY["shop_id"],
            "peerUnb": IDENTITY["buyer_id"],
            "chatId": IDENTITY["chat_id"],
        },
        "recent_messages": [
            {"direction": "buyer", "messageType": 1, "content": "八仙的", "messageId": "turn-1"},
            {"direction": "buyer", "messageType": 2, "content": "[截图]", "imageUrls": ["https://example.invalid/movie.png"], "messageId": "turn-2"},
            {"direction": "seller", "messageType": 1, "content": "已收到截图", "agent_generated": True, "messageId": "turn-2-reply"},
        ],
        "canonical_recognition": RECOGNITION,
        "current_purchase_context": {
            "purchase_context_id": IDENTITY["purchase_context_id"],
            "seat_request_type": "WPLUS_AREA",
            "status": "collecting",
        },
    }


@pytest.mark.asyncio
async def test_screenshot_facts_are_explicit_carry_forward_context() -> None:
    context = await AgentContextBuilder().build(body("13点10分那场"))

    inherited = context.to_dict()["inherited_screenshot_context"]
    assert inherited["source"] == "recent_canonical_screenshot"
    assert inherited["fact_tier"] == "candidate"
    assert inherited["facts"] == {
        "city": "广州",
        "cinema": "广州天河万达影城",
        "cinema_address": "天河路",
        "movie": "测试电影",
        "quote_date": "2026-09-11",
        "showtime_start": "08:40",
        "hall": "IMAX厅",
        "dimension": "IMAX",
    }
    assert context.to_dict()["confirmed_facts"] == {}


@dataclass
class QuoteRuntimeStub:
    requests: list[object]

    async def quote_structured(self, request):
        self.requests.append(request)
        return {"status": "QUOTE_UPDATED", "quote": {"quote_state": "PREVIEW"}}


class FollowupModel:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, object]]] = []

    async def complete(self, messages, tools):
        self.messages.append(messages)
        if len(self.messages) == 1:
            return {
                "tool_calls": [{
                    "name": "update_purchase_request",
                    "arguments": {"showtime_start": "13:10"},
                }],
            }
        return {"reply": "已按这张截图切换到13:10场，还需要几张？"}


@pytest.mark.asyncio
async def test_showtime_followup_updates_only_showtime_and_reuses_screenshot_facts() -> None:
    runtime = QuoteRuntimeStub([])
    model = FollowupModel()
    result = await CanonicalConversationAgent(
        AgentContextBuilder(),
        model,
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime),
        max_tool_rounds=1,
    ).process(body("13点10分那场"))

    assert result["status"] == "AGENT_REPLY_READY", result
    request = runtime.requests[0]
    assert request.city == "广州"
    assert request.cinema == "广州天河万达影城"
    assert request.movie == "测试电影"
    assert request.quote_date == "2026-09-11"
    assert request.showtime_start == "13:10"
    assert request.ticket_count is None
    context_json = model.messages[0][1]["content"]
    assert "inherited_screenshot_context" in context_json
    assert "广州天河万达影城" in context_json
    assert "不得重新索要截图" in model.messages[0][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "就这个",
        "这个场次",
        "第二场",
        "IMAX那场",
        "刚才截图那个",
        "两张",
        "还是刚才那个影院",
    ],
)
async def test_reference_followups_keep_all_screenshot_slots(text: str) -> None:
    context = await AgentContextBuilder().build(body(text))
    facts = context.to_dict()["inherited_screenshot_context"]["facts"]
    assert facts["city"] == "广州"
    assert facts["cinema"] == "广州天河万达影城"
    assert facts["movie"] == "测试电影"
    assert facts["quote_date"] == "2026-09-11"
    assert facts["showtime_start"] == "08:40"
    assert facts["dimension"] == "IMAX"
