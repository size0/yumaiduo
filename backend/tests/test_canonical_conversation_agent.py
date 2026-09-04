from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.canonical_conversation_agent import (
    AGENT_TOOL_SCHEMAS,
    AgentContextBuilder,
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
async def test_agent_does_not_treat_tool_or_model_output_as_authority():
    model = FakeModel([{"reply": "系统显示这两个座位都可售，价格是99元。"}])
    agent = CanonicalConversationAgent(AgentContextBuilder(), model)
    result = await agent.process(body(text="左边那两个呢"))
    assert result["status"] == "AGENT_REPLY_READY"
    assert result["reply"] == "系统显示这两个座位都可售，价格是99元。"
    assert result["context"]["confirmed_facts"] == {}


def test_tool_surface_is_high_level_and_excludes_legacy_intent_rules():
    names = {item["function"]["name"] for item in AGENT_TOOL_SCHEMAS}
    assert names == {
        "get_current_context", "get_quote", "update_quote_request", "select_quote",
        "get_order", "get_transaction", "get_show_options", "get_seat_status",
    }
    assert "keyword" not in str(AGENT_TOOL_SCHEMAS).lower()
    assert "regex" not in str(AGENT_TOOL_SCHEMAS).lower()
