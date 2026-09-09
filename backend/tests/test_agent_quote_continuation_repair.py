import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.canonical_conversation_agent import AgentContextBuilder, CanonicalAgentToolBackend, CanonicalConversationAgent, OpenAICompatibleAgentModel


def fixture_module():
    path = Path(__file__).resolve().parents[2] / "audit" / "quote_fixtures.py"
    spec = importlib.util.spec_from_file_location("quote_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_image_preview_model_quantity_tool_real_store(tmp_path):
    fixtures = fixture_module()
    runtime = fixtures.runtime(tmp_path)
    class ImageProvider:
        async def recognize(self, *_args, **_kwargs):
            return fixtures.recognition()
    runtime._recognition = ImageProvider()
    event = {"envelope": {"id": "image", "tenantId": "isolated", "event": "im.message.received",
        "payload": {"accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat", "itemId": "context",
                    "imageUrls": ["https://fixture.invalid/image"], "messageType": 2}}}
    preview = await runtime.process_image_event(event)
    assert preview["status"] == "QUOTED"
    assert preview["quote"]["quote_state"] == "PREVIEW"
    assert preview["quote"]["unit_sell_price_fen"] == 4490
    event["envelope"]["id"] = "two"
    event["envelope"]["payload"].update(content="2张", imageUrls=[], messageType=1)
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message = {"content": None, "tool_calls": [{"type": "function", "id": "call-quote",
                "function": {"name": "update_purchase_request", "arguments": '{"ticket_count":2}'}}]}
        else:
            result = json.loads(payload["messages"][-1]["content"])
            assert result["status"] == "QUOTED", result
            quote = result["quote"]
            assert quote["ticket_count"] == 2
            assert quote["total_sell_price_fen"] == 8980
            assert quote["quote_state"] == "TRANSACTION_READY"
            assert payload["messages"][-1]["tool_call_id"] == "call-quote"
            message = {"content": "广州测试万达，测试电影，13:35，44.90元/张，2张合计89.80元。"}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})
    model = OpenAICompatibleAgentModel(api_key="fixture", base_url="https://fixture.invalid/v1",model="fixture",
        transport=httpx.MockTransport(handler))
    store = runtime._quotes.store
    result = await CanonicalConversationAgent(AgentContextBuilder(quote_store=store),model,
        tool_backend=CanonicalAgentToolBackend(quote_runtime=runtime,quote_store=store),max_tool_rounds=1).process(event)
    assert result["status"] == "AGENT_REPLY_READY", result
    records = store.list("isolated")
    assert len(records) == 2
    successor = next(q for q in records if q["ticket_count"] == 2)
    assert successor["supersedes_quote_id"] == preview["quote"]["quote_id"]
    assert successor["purchase_context_id"] == "context"
    assert result["actions"][0]["text"] == result["reply"]
    assert len(requests) == 2
