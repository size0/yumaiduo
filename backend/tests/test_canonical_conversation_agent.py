from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.canonical_conversation_agent import (
    AGENT_TOOL_SCHEMAS,
    AgentContextBuilder,
    CanonicalAgentToolBackend,
    CanonicalConversationAgent,
)
from app.quote_record_store import QuoteRecordStore
from app.transaction_state_store import TransactionStateStore


IDENTITY = {
    "tenant_id": "107", "shop_id": "2313315754", "buyer_id": "2217098857081",
    "chat_id": "66166718230", "purchase_context_id": "ctx-image",
}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


@dataclass
class UpdateBackend:
    context: dict
    updates: list[dict]

    async def update_quote_request(self, updates, context):
        self.updates.append(dict(updates))
        return {
            "status": "QUOTE_UPDATED",
            "quote": {
                **context["current_quote"],
                "ticket_count": updates["ticket_count"],
                "total_sell_price_fen": 7820,
                "quote_state": "TRANSACTION_READY",
            },
        }


@dataclass
class QuoteRuntimeStub:
    requests: list[object]

    async def quote_structured(self, request):
        self.requests.append(request)
        return {
            "status": "QUOTE_UPDATED",
            "quote": {"ticket_count": request.ticket_count, "total_sell_price_fen": 5600},
        }


@pytest.fixture
def context_builder(tmp_path: Path):
    return AgentContextBuilder(
        quote_store=QuoteRecordStore(tmp_path / "quotes.json"),
        transaction_store=TransactionStateStore(tmp_path / "transactions.json"),
    )


def body(*, text="价钱多少", context=None):
    return {
        "envelope": {
            "id": "event-text-1", "tenantId": IDENTITY["tenant_id"],
            "event": "im.message.received", "timestamp": 1788490000000,
            "payload": {
                "accountUnb": IDENTITY["shop_id"], "peerUnb": IDENTITY["buyer_id"],
                "chatId": IDENTITY["chat_id"], "remoteMessageId": "buyer-text-1",
                "content": text, "messageType": 1,
            },
        },
        "session": {
            "accountUnb": IDENTITY["shop_id"], "peerUnb": IDENTITY["buyer_id"],
            "chatId": IDENTITY["chat_id"],
        },
        "recent_messages": [
            {"direction": "inbound", "messageType": 2, "content": "[image]", "messageId": "img-1"},
            {"direction": "inbound", "messageType": 1, "content": text, "messageId": "buyer-text-1"},
            {"direction": "outbound", "messageType": 1, "content": "已按当前截图记录场次", "agent_generated": True, "messageId": "self-1"},
            {"direction": "outbound", "messageType": 1, "content": "人工补充信息", "agent_generated": False, "messageId": "human-1"},
        ],
        "canonical_recognition": {
            "city": "牡丹江", "cinema": "万达影城", "movie": "坠落2：死点",
            "quote_date": "2026-09-05", "showtime_start": "19:55", "hall": "3号激光厅",
            "selected_seats": ["8排7座", "8排8座"], "status": "candidate",
        },
        "current_purchase_context": context or {"id": "ctx-image", "status": "collecting"},
        "payment_validation_evidence": {"status": "UNPAID"},
        "provider_fulfillment_state": {"status": "NONE"},
        "order_binding": {"status": "UNBOUND"},
        "authoritative_order": None,
    }


@pytest.mark.asyncio
async def test_context_builder_aggregates_authorities_without_second_database(context_builder):
    context = await context_builder.build(body())
    view = context.to_dict()
    assert view["fishmore_im_history"][0]["direction"] == "buyer"
    assert any(item["text"] == "价钱多少" for item in view["buyer_raw_messages"])
    assert view["candidate_facts"]["movie"] == "坠落2：死点"
    assert view["confirmed_facts"] == {}
    assert view["human_manual_context"][0]["text"] == "人工补充信息"
    assert view["transaction_state"]["status"] == "absent"
    assert view["payment_validation_evidence"]["status"] == "UNPAID"


@pytest.mark.asyncio
async def test_agent_uses_high_level_quote_tool_and_returns_durable_reply():
    model = FakeModel([
        {"tool_calls": [{"name": "update_quote_request", "arguments": {"ticket_count": 2}}]},
        {"reply": "已记下2张，合计78.2元。"},
    ])
    builder = AgentContextBuilder()
    backend = UpdateBackend({}, [])
    agent = CanonicalConversationAgent(builder, model, tool_backend=backend)
    result = await agent.process(body(text="2张", context={
        "id": "ctx-wplus", "status": "quoted",
        "current_quote": {"unit_sell_price_fen": 3910, "quote_state": "PREVIEW"},
    }))
    assert result["status"] == "AGENT_REPLY_READY"
    assert result["reply"] == "已记下2张，合计78.2元。"
    assert backend.updates == [{"ticket_count": 2}]
    assert result["actions"] == [{
        "type": "send_message", "text": "已记下2张，合计78.2元。",
        "source": "canonical_conversation_agent", "rule_governed": True,
    }]
    assert [call[1] for call in model.calls] == [AGENT_TOOL_SCHEMAS, AGENT_TOOL_SCHEMAS]


@pytest.mark.asyncio
async def test_agent_accepts_openai_nested_function_tool_arguments():
    model = FakeModel([
        {"tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "update_quote_request", "arguments": '{"ticket_count": 2}',
        }}]},
        {"reply": "已记录2张。"},
    ])
    runtime = QuoteRuntimeStub([])
    backend = CanonicalAgentToolBackend(quote_runtime=runtime)
    result = await CanonicalConversationAgent(
        AgentContextBuilder(), model, tool_backend=backend,
    ).process(body(text="2张"))
    assert result["status"] == "AGENT_REPLY_READY"
    assert runtime.requests[0].ticket_count == 2


@pytest.mark.asyncio
async def test_update_quote_request_builds_structured_request_from_context_without_price():
    model = FakeModel([
        {"tool_calls": [{"name": "update_quote_request", "arguments": {"ticket_count": 2}}]},
        {"reply": "已按当前场次记录2张。"},
    ])
    runtime = QuoteRuntimeStub([])
    backend = CanonicalAgentToolBackend(quote_runtime=runtime)
    agent = CanonicalConversationAgent(AgentContextBuilder(), model, tool_backend=backend)
    result = await agent.process(body(text="2张", context={
        "id": "ctx-wplus", "status": "quoted", "seat_request_type": "WPLUS_AREA",
        "current_quote": {"unit_sell_price_fen": 2800, "quote_state": "PREVIEW"},
    }))

    assert result["status"] == "AGENT_REPLY_READY"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.ticket_count == 2
    assert request.city == "牡丹江"
    assert request.cinema == "万达影城"
    assert request.movie == "坠落2：死点"
    assert request.seat_request_type == "WPLUS_AREA"
    assert not hasattr(request, "price_fen")
    assert not hasattr(request, "total_sell_price_fen")


@pytest.mark.asyncio
async def test_quote_request_id_follows_each_inbound_event():
    runtime = QuoteRuntimeStub([])
    backend = CanonicalAgentToolBackend(quote_runtime=runtime)
    context = {"id": "ctx-wplus", "status": "quoted", "seat_request_type": "WPLUS_AREA"}
    for event_id, text_value in (("event-count-2", "2张"), ("event-count-3", "3张")):
        model = FakeModel([
            {"tool_calls": [{"name": "update_quote_request", "arguments": {"ticket_count": int(text_value[0])}}]},
            {"reply": f"已记录{text_value}。"},
        ])
        payload = body(text=text_value, context=context)
        payload["envelope"]["id"] = event_id
        result = await CanonicalConversationAgent(AgentContextBuilder(), model, tool_backend=backend).process(payload)
        assert result["status"] == "AGENT_REPLY_READY"
    assert [request.request_id for request in runtime.requests] == ["event-count-2", "event-count-3"]


@pytest.mark.asyncio
async def test_acceptance_fixture_buyer_1606372904_wplus_preview_then_quantity():
    payload = body(text="2张", context={
        "id": "wplus-preview-1606372904", "status": "quoted",
        "seat_request_type": "WPLUS_AREA", "unit_sell_price_fen": 2800,
        "current_quote": {"unit_sell_price_fen": 2800, "quote_state": "PREVIEW"},
    })
    payload["envelope"]["payload"]["peerUnb"] = "1606372904"
    payload["session"]["peerUnb"] = "1606372904"
    runtime = QuoteRuntimeStub([])
    backend = CanonicalAgentToolBackend(quote_runtime=runtime)
    model = FakeModel([
        {"tool_calls": [{"name": "update_quote_request", "arguments": {"ticket_count": 2}}]},
        {"reply": "W+ 28/张，共2张56元。"},
    ])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model, tool_backend=backend).process(payload)
    assert result["status"] == "AGENT_REPLY_READY"
    assert runtime.requests[0].ticket_count == 2
    assert result["context"]["identity"]["buyer_id"] == "1606372904"


@pytest.mark.asyncio
async def test_acceptance_fixture_buyer_2217098857081_price_and_context_reference():
    payload = body(text="价钱多少", context={
        "id": "exact-context-2217098857081", "status": "quoted",
        "current_quote": {"total_sell_price_fen": 7820, "quote_state": "TRANSACTION_READY"},
        "selected_seats": ["8排7座", "8排8座"],
    })
    model = FakeModel([
        {"tool_calls": [{"name": "get_quote", "arguments": {}}]},
        {"reply": "当前这场是78.20元。"},
    ])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model).process(payload)
    assert result["status"] == "AGENT_REPLY_READY"
    assert result["context"]["identity"]["buyer_id"] == "2217098857081"
    assert "影院" not in result["reply"]


@pytest.mark.asyncio
async def test_acceptance_fixture_buyer_2464035965_reference_is_preview_only():
    payload = body(text="原来的座位没了怎么办", context={
        "id": "seat-unavailable-2464035965", "status": "seat_unavailable",
        "current_quote": {"seat_available": False, "quote_state": "SEAT_UNAVAILABLE"},
        "same_type_reference": {"unit_sell_price_fen": 4800, "reference_only": True},
    })
    payload["envelope"]["payload"]["peerUnb"] = "2464035965"
    payload["session"]["peerUnb"] = "2464035965"
    model = FakeModel([{"reply": "原座位已经没有了，同类型参考价48元/张，请重新选座。"}])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model).process(payload)
    assert result["status"] == "AGENT_REPLY_READY"
    assert result["context"]["same_type_reference_quote"]["reference_only"] is True
    assert result["context"]["current_quote"]["quote_state"] == "SEAT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_human_manual_context_is_visible_without_overriding_manual_quote():
    payload = body(text="老板刚才说48", context={
        "id": "manual-quote", "status": "quoted", "quote_source": "manual",
        "current_quote": {"unit_sell_price_fen": 4800, "quote_state": "TRANSACTION_READY"},
    })
    payload["recent_messages"].append({
        "direction": "outbound", "agent_generated": False, "messageType": 1,
        "content": "老板刚才说48元", "messageId": "human-price-1",
    })
    model = FakeModel([{"reply": "按刚才人工确认的48元执行。"}])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model).process(payload)
    assert result["status"] == "AGENT_REPLY_READY"
    assert any(item["message_id"] == "human-price-1" for item in result["context"]["human_manual_context"])
    assert result["context"]["current_quote"]["unit_sell_price_fen"] == 4800


@pytest.mark.asyncio
async def test_agent_does_not_treat_tool_or_model_output_as_authority():
    model = FakeModel([{"reply": "系统显示这两个座位都可售，价格是99元。"}])
    agent = CanonicalConversationAgent(AgentContextBuilder(), model)
    result = await agent.process(body(text="左边那两个呢"))
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert result["reason"] == "reply_fact_unverified"
    assert "unverified_price" in result["reply_guard"]["violations"]
    assert result["actions"] == []
    assert result["context"]["confirmed_facts"] == {}


@pytest.mark.asyncio
async def test_agent_reply_guard_rejects_definitive_claim_hidden_by_uncertainty():
    model = FakeModel([{"reply": "暂时无法确认，但已支付成功。"}])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model).process(body())
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert "payment_without_evidence" in result["reply_guard"]["violations"]


@pytest.mark.asyncio
async def test_context_tool_cannot_promote_candidate_price_to_authority():
    model = FakeModel([
        {"tool_calls": [{"name": "get_current_context", "arguments": {}}]},
        {"reply": "当前报价是99元。"},
    ])
    result = await CanonicalConversationAgent(AgentContextBuilder(), model).process(body(
        context={"id": "ctx-candidate", "status": "collecting"},
    ))
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert "unverified_price" in result["reply_guard"]["violations"]


@pytest.mark.asyncio
async def test_agent_reply_guard_allows_price_returned_by_quote_tool():
    model = FakeModel([
        {"tool_calls": [{"name": "get_quote", "arguments": {}}]},
        {"reply": "当前报价是78.20元。"},
    ])
    agent = CanonicalConversationAgent(
        AgentContextBuilder(), model,
    )
    result = await agent.process(body(context={
        "id": "ctx-quote", "status": "quoted",
        "current_quote": {"total_sell_price_fen": 7820, "quote_state": "PREVIEW"},
    }))
    assert result["status"] == "AGENT_REPLY_READY"
    assert result["reply"] == "当前报价是78.20元。"


@pytest.mark.asyncio
async def test_agent_model_failure_is_fail_closed_without_send_action():
    class BrokenModel:
        async def complete(self, messages, tools):
            raise RuntimeError("provider unavailable")

    result = await CanonicalConversationAgent(AgentContextBuilder(), BrokenModel()).process(body())
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert result["reason"] == "agent_model_failed"
    assert result["reply"] == ""
    assert result["actions"] == []


def test_tool_surface_is_high_level_and_excludes_legacy_intent_rules():
    names = {item["function"]["name"] for item in AGENT_TOOL_SCHEMAS}
    assert names == {
        "get_current_context", "get_quote", "update_quote_request", "select_quote",
        "get_order", "get_transaction", "get_show_options", "get_seat_status",
    }
    assert "keyword" not in str(AGENT_TOOL_SCHEMAS).lower()
    assert "regex" not in str(AGENT_TOOL_SCHEMAS).lower()
