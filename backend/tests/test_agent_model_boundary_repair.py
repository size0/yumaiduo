import json

import httpx
import pytest

from app.canonical_conversation_agent import (
    AgentContextBuilder, CanonicalConversationAgent, OpenAICompatibleAgentModel,
)


def event():
    return {"envelope": {"id": "isolated-text", "tenantId": "test", "payload": {
        "accountUnb": "shop", "peerUnb": "buyer", "chatId": "chat",
        "content": "2张", "messageType": 1,
    }}}


def model(handler, key="test-secret"):
    return OpenAICompatibleAgentModel(api_key=key, base_url="https://fixture.invalid/v1",
        model="fixture", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_last_tool_result_gets_final_text_round():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = {"content": None, "tool_calls": [{"id": "call-context",
                "type": "function", "function": {"name": "get_current_context", "arguments": "{}"}}]}
        else:
            assert not body.get("tools")
            assert body["messages"][-1]["tool_call_id"] == "call-context"
            assert json.loads(body["messages"][-1]["content"])["status"] == "success"
            message = {"content": "请补充影院信息。"}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})

    result = await CanonicalConversationAgent(AgentContextBuilder(), model(handler), max_tool_rounds=1).process(event())
    assert result["status"] == "AGENT_REPLY_READY"
    assert len(requests) == 2
    assert result["reply"] == "请补充影院信息。"


@pytest.mark.asyncio
async def test_missing_key_is_not_successful_agent_reply():
    def forbidden(_):
        raise AssertionError("no HTTP request permitted without key")
    result = await CanonicalConversationAgent(AgentContextBuilder(), model(forbidden, key="")).process(event())
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert result["model_diagnostic"]["stage"] == "configuration"
    assert result["actions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message,finish,stage", [
    ({"content": None}, "stop", "empty_response"),
    ({"content": "不完整报价"}, "length", "truncated"),
    ({"content": None, "refusal": "refused"}, "stop", "refusal"),
])
async def test_response_failures_are_distinguishable(message, finish, stage):
    client = model(lambda _: httpx.Response(200, headers={"x-request-id": "fixture-request"},
        json={"choices": [{"message": message, "finish_reason": finish}]}))
    result = await CanonicalConversationAgent(AgentContextBuilder(), client).process(event())
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert result["model_diagnostic"]["stage"] == stage
    assert result["model_diagnostic"]["request_id"] == "fixture-request"
    assert result["actions"] == []


@pytest.mark.asyncio
async def test_http_failure_has_safe_evidence():
    client = model(lambda _: httpx.Response(403, headers={"x-request-id": "fixture-denied"},
        json={"error": {"code": "permission_denied", "message": "secret test-secret private payload"}}))
    result = await CanonicalConversationAgent(AgentContextBuilder(), client).process(event())
    diagnostic = result["model_diagnostic"]
    assert diagnostic["http_status"] == 403
    assert diagnostic["stage"] == "http"
    assert diagnostic["request_id"] == "fixture-denied"
    assert "test-secret" not in json.dumps(result)
    assert "private payload" not in json.dumps(result)


@pytest.mark.asyncio
async def test_excess_tools_do_not_execute_after_final_round():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": None,
            "tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "get_current_context", "arguments": "{}"}}]}}]})
    result = await CanonicalConversationAgent(AgentContextBuilder(), model(handler), max_tool_rounds=1).process(event())
    assert len(calls) == 2
    assert len(result["tool_trace"]) == 1
    assert result["reason"] == "agent_tool_round_limit"
    assert result["actions"] == []


@pytest.mark.asyncio
async def test_transport_timeout_has_no_sensitive_message():
    def handler(request):
        raise httpx.ReadTimeout("secret test-secret and private prompt", request=request)
    result = await CanonicalConversationAgent(AgentContextBuilder(), model(handler)).process(event())
    assert result["model_diagnostic"]["stage"] == "timeout"
    assert result["model_diagnostic"]["exception_type"] == "ReadTimeout"
    assert "test-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_diagnostic_is_persisted_in_existing_audit_context():
    class Audit:
        def __init__(self):
            self.updates = []
        def create_agent_run(self, **kwargs):
            return {"run_id": "audit-test"}
        def update_agent_run(self, run_id, **kwargs):
            self.updates.append(kwargs)
    audit = Audit()
    result = await CanonicalConversationAgent(AgentContextBuilder(), model(lambda _: httpx.Response(403)),
        audit_store=audit).process(event())
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert audit.updates[-1]["context"]["model_diagnostic"]["http_status"] == 403
