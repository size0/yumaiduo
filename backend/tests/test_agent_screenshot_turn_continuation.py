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
    "city_text": "北京",
    "cinema_text": "万达影城（怀柔万达广场IMAX店）",
    "cinema_address": "怀柔万达广场",
    "movie": "八仙！",
    "show_date": "2026-09-12",
    "start_time": "08:40",
    "hall": "IMAX厅",
    "dimension": "IMAX",
    "show_options": [
        {"showtime_start": "08:40", "hall": "IMAX厅", "dimension": "IMAX"},
        {"showtime_start": "11:20", "hall": "2号厅", "dimension": "2D"},
        {"showtime_start": "13:10", "hall": "3号厅", "dimension": "2D"},
    ],
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
            "show_options": [
                {"showtime_start": "08:40", "hall": "IMAX厅", "dimension": "IMAX"},
                {"showtime_start": "11:20", "hall": "2号厅", "dimension": "2D"},
                {"showtime_start": "13:10", "hall": "3号厅", "dimension": "2D"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_screenshot_facts_are_explicit_carry_forward_context() -> None:
    context = await AgentContextBuilder().build(body("13点10分那场"))

    inherited = context.to_dict()["inherited_screenshot_context"]
    assert inherited["source"] == "recent_canonical_screenshot"
    assert inherited["fact_tier"] == "candidate"
    assert inherited["facts"] == {
        "city": "北京",
        "cinema": "万达影城（怀柔万达广场IMAX店）",
        "cinema_address": "怀柔万达广场",
        "movie": "八仙！",
        "quote_date": "2026-09-12",
        "showtime_start": "08:40",
        "hall": "IMAX厅",
        "dimension": "IMAX",
        "show_options": RECOGNITION["show_options"],
    }
    assert inherited["show_options"] == RECOGNITION["show_options"]
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
    assert request.city == "北京"
    assert request.cinema == "万达影城（怀柔万达广场IMAX店）"
    assert request.movie == "八仙！"
    assert request.quote_date == "2026-09-12"
    assert request.showtime_start == "13:10"
    assert request.ticket_count is None
    context_json = model.messages[0][1]["content"]
    assert "inherited_screenshot_context" in context_json
    assert "万达影城（怀柔万达广场IMAX店）" in context_json
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
    assert facts["city"] == "北京"
    assert facts["cinema"] == "万达影城（怀柔万达广场IMAX店）"
    assert facts["movie"] == "八仙！"
    assert facts["quote_date"] == "2026-09-12"
    assert facts["showtime_start"] == "08:40"
    assert facts["dimension"] == "IMAX"


class MustNotCallModel:
    calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        raise AssertionError("high-confidence follow-up must not fall back to the LLM")


@dataclass
class QuoteResultStub:
    requests: list[object]
    result: dict[str, object]

    async def quote_structured(self, request):
        self.requests.append(request)
        return dict(self.result)


@pytest.mark.asyncio
async def test_explicit_text_seat_selection_does_not_require_manual_mark() -> None:
    runtime = QuoteResultStub(
        [], {"status": "QUOTED", "current_runtime_reply": "已取得权威报价。"},
    )

    class SeatFollowupModel:
        async def complete(self, messages, tools):
            if any(item.get("role") == "tool" for item in messages):
                return {"reply": "权威报价已取得。"}
            return {"tool_calls": [{
                "name": "update_purchase_request",
                "arguments": {"selected_seats": ["4排7座"]},
            }]}

    result = await CanonicalConversationAgent(
        AgentContextBuilder(), SeatFollowupModel(),
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime),
        max_tool_rounds=1,
    ).process(body("4排7座"))

    assert result["status"] == "AGENT_REPLY_READY", result
    assert runtime.requests[0].seat_request_type == "EXACT_SEATS"
    assert runtime.requests[0].selected_seats == ["4排7座"]
    assert runtime.requests[0].has_manual_mark is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, expected",
    [
        ("13点10分那场", {"showtime_start": "13:10"}),
        ("第二场", {"showtime_start": "11:20"}),
        ("IMAX那场", {"showtime_start": "08:40", "dimension": "IMAX"}),
        ("两张", {"ticket_count": 2}),
    ],
)
async def test_high_confidence_followup_enters_authoritative_quote_flow(text, expected) -> None:
    runtime = QuoteResultStub(
        [],
        {
            "status": "QUOTED",
            "current_runtime_reply": "权威报价已取得，单张39.90元。",
            "quote": {"unit_sell_price_fen": 3990, "total_sell_price_fen": 3990},
        },
    )
    model = MustNotCallModel()
    result = await CanonicalConversationAgent(
        AgentContextBuilder(), model,
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime),
    ).process(body(text))

    assert result["status"] == "AGENT_REPLY_READY", result
    assert model.calls == 0
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.city == "北京"
    assert request.cinema == "万达影城（怀柔万达广场IMAX店）"
    assert request.movie == "八仙！"
    assert request.quote_date == "2026-09-12"
    for field, value in expected.items():
        assert getattr(request, field) == value
    assert result["tool_trace"][0]["tool"] == "update_purchase_request"
    assert result["tool_trace"][0]["result"]["status"] == "QUOTED"


@pytest.mark.asyncio
async def test_show_not_found_reports_only_show_failure() -> None:
    runtime = QuoteResultStub(
        [], {"status": "SHOW_UNRESOLVED", "reason": "WANDA_SHOW_NOT_FOUND"},
    )
    result = await CanonicalConversationAgent(
        AgentContextBuilder(), MustNotCallModel(),
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime),
    ).process(body("13点10分那场"))

    assert result["status"] == "AGENT_REPLY_READY", result
    assert "13:10" in result["reply"]
    assert all(field not in result["reply"] for field in ("城市", "影院", "影片", "日期"))


@pytest.mark.asyncio
async def test_latency_trace_contains_ordered_agent_stages() -> None:
    trace = {"marks": {}}
    event = body("13点10分那场")
    event["_canonical_latency_trace"] = trace
    runtime = QuoteResultStub(
        [], {"status": "SHOW_UNRESOLVED", "reason": "WANDA_SHOW_NOT_FOUND"},
    )
    result = await CanonicalConversationAgent(
        AgentContextBuilder(), MustNotCallModel(),
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime),
    ).process(event)

    report = result["context"]["latency_trace"]
    assert report["marks"]
    assert report["total_ms"] >= 0
    assert "T1" in report["marks"] and "T2" in report["marks"]
    assert report["slowest"]
