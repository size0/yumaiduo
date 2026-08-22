from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app.conversation_agent import ConversationAgentService, SYSTEM_PROMPT, classify_agent_scene
from app.main import create_app
from app.schemas import AgentPlan, AgentTurnRequest


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


def test_agent_common_ticket_text_uses_fast_bounded_planning() -> None:
    common_payload = request_payload().model_dump(mode="json")
    common_payload.update({"latest_message": "两张多少钱", "history": [{"role": "buyer", "content": "两张多少钱", "source": "buyer"}]})

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 400
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "intent": "票价咨询", "confidence": 0.98, "goal": "取得截图", "action": "ask_for_image",
            "arguments": {}, "missing_fields": ["完整选座页截图"], "reply": "请发送完整选座页截图。", "needs_human": False, "reason": "缺少图片",
        }, ensure_ascii=False)}}]})

    result = asyncio.run(ConversationAgentService(httpx.MockTransport(handler)).plan(AgentTurnRequest.model_validate(common_payload), {
        "base_url": "https://model.example/v1", "model": "flash", "api_key": "secret", "temperature": 0, "max_tokens": 2000,
    }))
    assert result.action == "ask_for_image"


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
