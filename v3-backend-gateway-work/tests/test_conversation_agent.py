from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.conversation_agent import ConversationAgentService, NATIVE_SYSTEM_PROMPT, SYSTEM_PROMPT, classify_agent_scene
from app.main import create_app
from app.schemas import AgentCompletionRequest, AgentPlan, AgentTurnRequest, QuoteTextFactExtractRequest, QuoteTextFacts


def request_payload() -> AgentTurnRequest:
    return AgentTurnRequest.model_validate({
        "event_id": "event-1",
        "tenant_id": "tenant-1",
        "latest_message": "我有一个比较特殊的情况想咨询",
        "history": [{"role": "buyer", "content": "我有一个比较特殊的情况想咨询"}],
        "state": {"facts": {"city": "郑州"}, "stage": "collecting_information"},
        "observations": [],
        "has_image": False,
    })


def test_agent_service_returns_a_typed_bounded_plan() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is True
        assert body["max_tokens"] == 800
        assert "不能直接改价" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "选座核价",
            "confidence": 0.94,
            "goal": "获得实时价格",
            "action": "ask_for_image",
            "arguments": {},
            "missing_fields": ["完整选座页截图"],
            "reply": "请发送完整的万达选座页截图，我会继续处理。",
            "needs_human": False,
            "reason": "缺少实时座位证据",
        }, ensure_ascii=False)}}]})

    service = ConversationAgentService(httpx.MockTransport(handler))
    result = asyncio.run(service.plan(request_payload(), {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret",
        "temperature": 0.2, "max_tokens": 1000,
    }, ["只处理万达电影票"] ))
    assert result.action == "ask_for_image"
    assert result.missing_fields == ["完整选座页截图"]
    assert result.reply.startswith("请发送")


def test_native_agent_completion_uses_standard_messages_tools_and_reasoning_without_json_planner() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(200, headers={"x-request-id": "model-request-1"}, json={
            "id": "completion-1", "model": "reasoning-model",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_active_quote", "arguments": "{}"},
                }],
            }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    request = AgentCompletionRequest.model_validate({
        "tenant_id": "tenant-1", "conversation_id": "tenant-1:chat-1", "run_id": "shadow:tenant-1:run-1",
        "messages": [
            {"role": "user", "content": "多少钱"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "old-call", "type": "function", "function": {"name": "recognize_image", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "old-call", "content": "{\"status\":\"success\"}"},
        ],
        "available_tools": ["recognize_image", "read_active_quote", "create_manual_task"],
        "knowledge_snapshot": {"version": "", "entries": []},
        "reasoning": {"enabled": True, "effort": "high", "max_output_tokens": 1600},
    })
    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).complete(
        request,
        {"base_url": "https://model.example/v1", "model": "reasoning-model", "api_key": "secret", "temperature": 0.2},
        request.knowledge_snapshot.model_copy(update={"version": "kb-server", "entries": ["server knowledge"]}),
    ))

    assert captured["enable_thinking"] is True
    assert captured["reasoning_effort"] == "high"
    assert captured["max_tokens"] == 1600
    assert captured["tool_choice"] == "auto"
    assert "response_format" not in captured
    assert captured["messages"][0]["content"].startswith(NATIVE_SYSTEM_PROMPT)
    assert [item["role"] for item in captured["messages"][1:]] == ["user", "assistant", "tool"]
    assert [item["function"]["name"] for item in captured["tools"]] == ["recognize_image", "read_active_quote", "create_manual_task"]
    assert result.assistant.tool_calls[0].function.name == "read_active_quote"
    assert result.finish_reason == "tool_calls"
    assert result.versions.knowledge == "kb-server"
    assert result.request_id == "model-request-1"
    assert result.usage.total_tokens == 120
    assert result.reasoning.requested is True
    assert result.reasoning.applied is True


def test_native_reasoning_fallback_is_explicit_in_the_response() -> None:
    calls = 0
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": "unsupported reasoning"})
        return httpx.Response(200, json={"id": "c2", "model": "compat-model", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "您好"}}]})
    request = AgentCompletionRequest.model_validate({
        "tenant_id": "tenant-1", "conversation_id": "tenant-1:chat", "run_id": "shadow:tenant-1:run",
        "messages": [{"role": "user", "content": "你好"}], "available_tools": [],
        "knowledge_snapshot": {"version": "", "entries": []}, "reasoning": {"enabled": True},
    })
    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).complete(request, {
        "base_url": "https://model.example/v1", "model": "compat-model", "api_key": "secret",
    }))
    assert calls == 2
    assert result.reasoning.requested is True
    assert result.reasoning.applied is False
    assert result.reasoning.fallback_reason == "provider_rejected_reasoning_parameters"


def test_agent_service_extracts_typed_quote_facts_without_transaction_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 400
        system = body["messages"][0]["content"]
        assert "只抽取买家明确表达" in system
        assert "价格" in system and "库存" in system
        user = json.loads(body["messages"][1]["content"])
        assert user["reference_date"] == "2026-08-23"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "quote_intent": True, "city": "济南", "cinema": "济南世贸万达影城", "movie": "奥德赛", "date": "2026-08-23",
            "showtime": "12:35", "hall": None, "ticket_count": 2, "seat_numbers": [], "requested_row": None,
            "refers_to_image_positions": True, "confidence": 0.99,
        }, ensure_ascii=False)}}]})

    request = QuoteTextFactExtractRequest(
        event_id="event-jinan", tenant_id="107",
        message_text="您好 请问济南世贸万达影城今日12:35开场的奥德赛这两个位置还有票吗？",
        observed_at=1787452278221,
    )
    facts = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).extract_quote_facts(request, {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 1200,
    }))
    assert facts.city == "济南"
    assert facts.cinema == "济南世贸万达影城"
    assert facts.movie == "奥德赛"
    assert facts.date.isoformat() == "2026-08-23"
    assert facts.ticket_count == 2
    assert facts.refers_to_image_positions is True
    assert facts.seat_numbers == []


def test_agent_scene_classification_is_deterministic_and_prefers_transaction_stage() -> None:
    base = request_payload().model_dump(mode="json")
    cases = [
        ({"latest_message": "你好在吗"}, "general"),
        ({"latest_message": "我发选座截图，两张多少钱", "has_image": True}, "intake"),
        ({"latest_message": "这个报价还能优惠吗", "state": {"stage": "quoted", "facts": {"quote_total_cents": 10000}}}, "quote_followup"),
        ({"latest_message": "订单改好价格了吗", "state": {"stage": "waiting_payment", "facts": {"has_linked_order": True}}}, "order"),
        ({"latest_message": "人工处理进度怎么样了"}, "order"),
        ({"latest_message": "什么时候出票", "state": {"stage": "paid_manual_delivery", "facts": {"paid": True}}}, "fulfillment"),
        ({"latest_message": "我要申请退款售后"}, "aftersale"),
    ]
    for patch, expected in cases:
        payload = {**base, **patch}
        assert classify_agent_scene(AgentTurnRequest.model_validate(payload)) == expected


def test_agent_common_ticket_text_keeps_reasoning_enabled_for_semantic_routing() -> None:
    common_payload = request_payload().model_dump(mode="json")
    common_payload.update({"latest_message": "两张多少钱", "history": [{"role": "buyer", "content": "两张多少钱", "source": "buyer"}]})

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        assert body["enable_thinking"] is True
        assert body["max_tokens"] == 800
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "票价咨询", "confidence": 0.98, "goal": "取得截图", "action": "ask_for_image",
            "arguments": {}, "missing_fields": ["完整选座页截图"], "reply": "请发送完整选座页截图。", "needs_human": False, "reason": "缺少图片",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(AgentTurnRequest.model_validate(common_payload), {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 2000,
    }))
    assert result.action == "ask_for_image"


def test_agent_contextual_acknowledgement_is_not_forced_into_the_low_risk_fast_path() -> None:
    payload = request_payload().model_dump(mode="json")
    payload.update({
        "latest_message": "可以的",
        "history": [
            {"role": "seller", "content": "当前报价59元一张，是否接受？", "source": "external_seller"},
            {"role": "buyer", "content": "可以的", "source": "buyer"},
        ],
        "state": {"stage": "quoted", "facts": {"quote_total_cents": 5900}},
    })

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        assert body["enable_thinking"] is True
        assert body["max_tokens"] == 800
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "补充信息", "confidence": 0.98, "goal": "读取有效报价后确认", "action": "confirm_quote",
            "arguments": {}, "missing_fields": [], "reply": "", "needs_human": False, "reason": "承接上一轮报价",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(AgentTurnRequest.model_validate(payload), {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 2000,
    }))
    assert result.action == "confirm_quote"


def test_agent_image_first_step_disables_thinking_because_the_only_safe_action_is_recognition() -> None:
    image_payload = request_payload().model_dump(mode="json")
    image_payload.update({"latest_message": "[图片或非文本消息]", "has_image": True})
    image_payload["history"] = [{"role": "buyer", "content": "[图片或非文本消息]", "source": "buyer"}]

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 400
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "选座核价", "confidence": 0.99, "goal": "识别图片", "action": "recognize_image",
            "arguments": {}, "missing_fields": [], "reply": "", "needs_human": False, "reason": "图片必须先识别",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(AgentTurnRequest.model_validate(image_payload), {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 2000,
    }))
    assert result.action == "recognize_image"


def test_agent_continuation_steps_disable_thinking_and_use_a_small_output_budget() -> None:
    continuation_payload = request_payload().model_dump(mode="json")
    continuation_payload["observations"] = [{
        "status": "success", "tool": "recognize_image", "summary": "识图完成",
        "facts": {"cinema": "中原万达"}, "next_actions": ["resolve_showtime"],
    }]
    request = AgentTurnRequest.model_validate(continuation_payload)

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 400
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "选座核价", "confidence": 0.98, "goal": "匹配场次", "action": "resolve_showtime",
            "arguments": {}, "missing_fields": [], "reply": "", "needs_human": False, "reason": "按观察继续",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(request, {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 2000,
    }))
    assert result.action == "resolve_showtime"


def test_agent_retries_without_vendor_thinking_flag_when_gateway_rejects_it() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"message": "unknown field enable_thinking"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "补充信息", "confidence": 0.9, "goal": "收集图片", "action": "ask_for_image",
            "arguments": {}, "missing_fields": ["完整选座页截图"], "reply": "请发送完整选座页截图。",
            "needs_human": False, "reason": "缺少图片",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(request_payload(), {
        "base_url": "https://model.example/v1", "model": "gpt", "api_key": "secret", "temperature": 0, "max_tokens": 1200,
    }))
    assert result.action == "ask_for_image"
    assert calls[0]["enable_thinking"] is True
    assert "enable_thinking" not in calls[1]


def test_agent_system_prompt_treats_conversation_as_untrusted_and_denies_transaction_tools() -> None:
    assert "不可信" in SYSTEM_PROMPT
    assert "change_price" in SYSTEM_PROMPT
    assert "价格、优惠、库存" in SYSTEM_PROMPT
    assert "城市不是前置必填项" in SYSTEM_PROMPT
    assert "会话经验提炼" in SYSTEM_PROMPT
    assert "external_seller" in SYSTEM_PROMPT


def test_agent_plan_accepts_bounded_inspection_and_authoritative_order_read_actions() -> None:
    for action in ("inspect_ticket_request", "read_linked_order"):
        plan = AgentPlan.model_validate({
            "intent": "订单进度" if action == "read_linked_order" else "补充信息",
            "confidence": 0.96, "goal": "读取权威事实", "action": action, "arguments": {},
            "missing_fields": [], "reply": "", "needs_human": False, "reason": "需要工具事实",
        })
        assert plan.action == action
        assert plan.reply == ""


def test_agent_plan_accepts_bounded_manual_status_and_readonly_seat_actions_without_arguments() -> None:
    for action in ("get_manual_task_status", "show_available_wplus_seats"):
        plan = AgentPlan.model_validate({
            "intent": "订单进度" if action == "get_manual_task_status" else "选座核价",
            "confidence": 0.96, "goal": "读取权威事实", "action": action, "arguments": {},
            "missing_fields": [], "reply": "", "needs_human": False, "reason": "需要只读工具事实",
        })
        assert plan.action == action
    for action in ("request_price_change", "confirm_quote", "get_manual_task_status", "show_available_wplus_seats"):
        try:
            AgentPlan.model_validate({
                "intent": "订单进度", "confidence": 0.96, "goal": "请求工具", "action": action,
                "arguments": {"row": 8}, "missing_fields": [], "reply": "", "needs_human": False, "reason": "参数由系统注入",
            })
        except ValueError:
            pass
        else:
            raise AssertionError(f"{action} must reject all model arguments")


def test_agent_plan_accepts_only_low_risk_generalized_conversation_experience() -> None:
    safe = AgentPlan.model_validate({
        "intent": "其他", "confidence": 0.92, "goal": "回答资料问题", "action": "respond", "arguments": {},
        "missing_fields": [], "reply": "请发送完整选座页截图并说明张数。", "needs_human": False, "reason": "普通流程咨询",
        "experience_candidate": {
            "topic": "图片要求", "question_pattern": "买家询问核价前需要提供什么资料",
            "response_guidance": "说明需要完整选座页截图和明确张数。", "example_reply": "请发送完整选座页截图并说明需要的张数。",
            "outcome_signal": "buyer_progressed", "confidence": 0.91,
        },
    })
    assert safe.experience_candidate is not None
    assert safe.experience_candidate.topic == "图片要求"
    for unsafe_text in ("给买家优惠到三十元", "订单一二三四五六已经改价可以付款", "联系微信 abc123"):
        try:
            AgentPlan.model_validate({
                "intent": "其他", "confidence": 0.92, "goal": "回答", "action": "respond", "arguments": {},
                "missing_fields": [], "reply": "好的", "needs_human": False, "reason": "普通咨询",
                "experience_candidate": {
                    "topic": "服务流程", "question_pattern": "买家咨询",
                    "response_guidance": unsafe_text, "example_reply": unsafe_text,
                    "outcome_signal": "buyer_acknowledged", "confidence": 0.9,
                },
            })
        except ValueError:
            pass
        else:
            raise AssertionError("sensitive or transaction-adjacent experience must be rejected")


def test_agent_request_rejects_oversized_or_unknown_state() -> None:
    payload = request_payload().model_dump(mode="json")
    payload["unknown"] = True
    try:
        AgentTurnRequest.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("extra fields must be rejected")


def test_agent_endpoint_requires_ingest_auth_and_returns_only_the_typed_plan(monkeypatch) -> None:
    class FakeAgent:
        async def plan(self, request, model_settings):
            assert request.tenant_id == "tenant-1"
            return AgentPlan.model_validate({
                "intent": "补充信息", "confidence": 0.9, "goal": "补充城市", "action": "ask_for_city",
                "arguments": {}, "missing_fields": ["城市"], "reply": "请问是哪个城市的万达影城？", "needs_human": False, "reason": "影院不唯一",
            })

    monkeypatch.setenv("WANDA_PREVIEW_INGEST_KEY", "test-preview-key")
    client = TestClient(create_app(conversation_agent_service=FakeAgent()))
    payload = request_payload().model_dump(mode="json")
    assert client.post("/api/agents/turn", json=payload).status_code == 401
    response = client.post("/api/agents/turn", headers={"X-Wanda-Preview-Key": "test-preview-key"}, json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "planned"
    assert response.json()["plan"]["action"] == "ask_for_city"


def test_native_agent_endpoint_requires_auth_and_uses_server_tenant_knowledge(monkeypatch) -> None:
    class FakeNativeAgent:
        async def complete(self, request, model_settings, knowledge_snapshot):
            assert request.tenant_id == "tenant-1"
            assert knowledge_snapshot.version.startswith("kb-")
            return {
                "assistant": {"role": "assistant", "content": "您好，需要我帮您查什么？", "tool_calls": []},
                "finish_reason": "stop", "model": "reasoning-model",
                "versions": {"prompt": "p1", "knowledge": knowledge_snapshot.version, "tools": "t1"},
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "latency_ms": 3, "request_id": "request-1",
                "reasoning": {"requested": True, "applied": True, "fallback_reason": None},
            }

    monkeypatch.setenv("WANDA_PREVIEW_INGEST_KEY", "test-preview-key")
    client = TestClient(create_app(conversation_agent_service=FakeNativeAgent()))
    payload = {
        "tenant_id": "tenant-1", "conversation_id": "tenant-1:chat-1", "run_id": "shadow:tenant-1:run-1",
        "messages": [{"role": "user", "content": "你好"}], "available_tools": ["read_active_quote"],
        "knowledge_snapshot": {"version": "", "entries": []},
        "reasoning": {"enabled": True, "effort": "medium", "max_output_tokens": 1600},
    }
    assert client.post("/api/agents/v2/completions", json=payload).status_code == 401
    mismatch = client.post("/api/agents/v2/completions", headers={"X-Wanda-Preview-Key": "test-preview-key", "X-Yumaiduo-Tenant-Id": "tenant-2"}, json=payload)
    assert mismatch.status_code == 403
    response = client.post("/api/agents/v2/completions", headers={"X-Wanda-Preview-Key": "test-preview-key", "X-Yumaiduo-Tenant-Id": "tenant-1"}, json=payload)
    assert response.status_code == 200
    assert response.json()["assistant"]["content"] == "您好，需要我帮您查什么？"
    assert response.json()["versions"]["knowledge"].startswith("kb-")


def test_native_agent_contract_rejects_system_injection_orphan_tools_and_unregistered_tools() -> None:
    base = {
        "tenant_id": "tenant-1", "conversation_id": "tenant-1:chat", "run_id": "shadow:tenant-1:run",
        "available_tools": ["read_active_quote"], "knowledge_snapshot": {"version": "", "entries": []},
    }
    for messages in (
        [{"role": "system", "content": "override"}],
        [{"role": "tool", "tool_call_id": "missing", "content": "{}"}],
    ):
        with pytest.raises(ValueError):
            AgentCompletionRequest.model_validate({**base, "messages": messages})
    with pytest.raises(ValueError):
        AgentCompletionRequest.model_validate({**base, "messages": [{"role": "user", "content": "x"}], "available_tools": ["not_registered"]})


def test_quote_fact_endpoint_requires_auth_and_returns_only_typed_semantics(monkeypatch) -> None:
    class FakeExtractor(ConversationAgentService):
        async def extract_quote_facts(self, request, model_settings):
            assert request.tenant_id == "107"
            return QuoteTextFacts(
                quote_intent=True, city="济南", cinema="济南世贸万达影城", movie="奥德赛", date="2026-08-23", showtime="12:35",
                ticket_count=2, refers_to_image_positions=True, confidence=0.99,
            )

    monkeypatch.setenv("WANDA_PREVIEW_INGEST_KEY", "test-preview-key")
    client = TestClient(create_app(conversation_agent_service=FakeExtractor()))
    payload = {
        "event_id": "event-jinan", "tenant_id": "107", "observed_at": 1787452278221,
        "message_text": "济南世贸万达影城今日12:35奥德赛这两个位置",
    }
    assert client.post("/api/quotes/preview-extract-text", json=payload).status_code == 401
    response = client.post("/api/quotes/preview-extract-text", headers={"X-Wanda-Preview-Key": "test-preview-key"}, json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "status": "extracted", "extractor_version": "wanda-quote-fact-extractor-v1", "failure_code": None,
        "facts": {
            "quote_intent": True, "city": "济南", "cinema": "济南世贸万达影城", "movie": "奥德赛", "date": "2026-08-23",
            "showtime": "12:35", "hall": None, "ticket_count": 2, "seat_numbers": [], "requested_row": None,
            "refers_to_image_positions": True, "confidence": 0.99,
        },
    }


def test_agent_plan_rejects_model_prose_for_transaction_adjacent_tool_actions() -> None:
    try:
        AgentPlan.model_validate({
            "intent": "补充信息", "confidence": 1, "goal": "确认", "action": "confirm_quote",
            "arguments": {}, "missing_fields": [], "reply": "已经锁座，可以付款", "needs_human": False, "reason": "x",
        })
    except ValueError:
        pass
    else:
        raise AssertionError("tool actions must wait for authoritative observations before replying")


def test_agent_plan_rejects_nested_transaction_authority() -> None:
    try:
        AgentPlan.model_validate({
            "intent": "票价咨询", "confidence": 1, "goal": "报价", "action": "start_quote",
            "arguments": {"nested": {"amount_cents": 1}}, "missing_fields": [], "reply": "", "needs_human": False, "reason": "x",
        })
    except ValueError:
        pass
    else:
        raise AssertionError("nested transaction arguments must be rejected")


def test_agent_service_retries_one_invalid_contract_response() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = "not-json" if calls == 1 else json.dumps({
            "intent": "其他", "confidence": 0.8, "goal": "回答问题", "action": "respond",
            "arguments": {}, "missing_fields": [], "reply": "您好，请问需要了解什么？", "needs_human": False, "reason": "普通咨询",
        }, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(request_payload(), {
        "base_url": "https://model.example", "model": "flash", "api_key": "secret",
        "temperature": 0.2, "max_tokens": 1000,
    }))
    assert result.action == "respond"
    assert calls == 2
