from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chat import _safe_quote_failure_reply
from app.chat_service import CustomerServiceChatService
from app.config import Settings
from app.knowledge_store import KnowledgeEntry
from app.main import create_app
from app.models import MovieImageInfo, RealQuote
from app.reply_template_store import ReplyTemplates
from app.settings_store import PersistentSettingsStore


class ReversibleProtector:
    def protect(self, value: str) -> str:
        return "enc:" + value[::-1]

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")[::-1]


def test_cinema_brand_is_not_rejected_before_authoritative_match() -> None:
    recognition = MovieImageInfo(
        cinema_name="北京寰映影城合生汇店", movie_name="奥德赛",
        date_text="今天 8月25日", showtime_start="19:30",
    )

    reply = _safe_quote_failure_reply(recognition, "影院无法在万达官方影院缓存中匹配", ReplyTemplates())

    assert reply is not None
    assert "目前仅支持万达影城" not in reply
    assert "无法唯一匹配官方影院" in reply


class StubRecognitionService:
    async def recognize(self, _image: bytes, _content_type: str, _buyer_message: str = "", *, prior_recognitions: list[MovieImageInfo] | None = None) -> MovieImageInfo:
        return MovieImageInfo(selected_count_visible=0)


@pytest.mark.asyncio
async def test_workbench_simulation_exposes_write_tools_without_running_platform_writes() -> None:
    calls: list[str] = []
    responses = iter([
        {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "action": "tool_call", "tool": "order.create", "arguments": {"count": 2},
        })}}]},
        {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "action": "reply", "message": "模拟下单流程已完成。",
        })}}]},
    ])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async def executor(name: str, _arguments: dict[str, object]) -> dict[str, object]:
        calls.append(name)
        return {"ok": True, "simulation": True, "status": "simulated"}

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1")
    schemas = [{"type": "function", "function": {
        "name": "order.create", "description": "create", "parameters": {"type": "object"},
    }}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client, tool_executor=executor,
            tool_schemas=[], simulation_tool_schemas=schemas,
        )
        reply = await service.reply(
            "帮我下单", "workbench-simulation",
            runtime_context={"_simulation_mode": True},
        )

    assert reply == "模拟下单流程已完成。"
    assert calls == ["order.create"]


@pytest.mark.asyncio
async def test_customer_service_calls_selected_gpt_with_thinking_disabled() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chat-text-1",
            "model": "gpt-5.5",
            "choices": [{"message": {"role": "assistant", "content": "可以，请先发送电影票截图。"}}],
            "usage": {"total_tokens": 30},
        })

    settings = Settings(
        api_key="vision-key",
        base_url="https://vision.example/v1",
        model="qwen3.5-flash-2026-02-23",
        chat_api_key="relay-key",
        chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5",
        enable_thinking=False,
        chat_prompt="你是万达电影票客服。",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await CustomerServiceChatService(settings, client=client).reply("怎么买票？", "conversation-1")

    assert reply == "可以，请先发送电影票截图。"
    assert captured["url"] == "https://airelvo.cc/v1/chat/completions"
    assert captured["authorization"] == "Bearer relay-key"
    assert captured["model"] == "gpt-5.5"
    assert captured["reasoning_effort"] == "none"
    assert captured["max_completion_tokens"] == 3000
    assert captured["messages"][0]["role"] == "system"
    assert "【AI扩展回答权限】" in captured["messages"][0]["content"]
    assert captured["messages"][1] == {"role": "user", "content": "怎么买票？"}
    assert captured["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_customer_service_injects_policy_and_enabled_knowledge_into_ai_prompt() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "请发送完整截图。"}}]})

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1", chat_prompt="基础提示词")
    policy = SimpleNamespace(
        agent_persona="耐心客服", business_background="万达电影票代订", reply_style="简短自然",
        human_service_hours="每日 09:00-24:00", customer_service_knowledge="争议转人工",
    )
    knowledge = KnowledgeEntry(
        id="kb-000000000001", title="测试问答", category="常见问题",
        common_questions="怎么确认？", reply_guidance="请稍候。", handling_rules="过期重新询价。",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CustomerServiceChatService(
            settings, client=client, conversation_policy_provider=lambda: policy,
            knowledge_provider=lambda: [knowledge],
        ).reply("怎么买？", "conversation-policy")

    prompt_text = "\n".join(item["content"] for item in captured["messages"] if item["role"] == "system")
    assert "客服人设：耐心客服" in prompt_text
    assert "人工客服时间：每日 09:00-24:00" in prompt_text
    assert "[常见问题] 测试问答" in prompt_text
    assert "回复口径：请稍候。" in prompt_text
    assert "处理规则：过期重新询价。" not in prompt_text


@pytest.mark.asyncio
async def test_customer_service_injects_context_and_stage_policy_into_ai_prompt() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "已收到"}}]})

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1", chat_prompt="基础提示词")
    policy = SimpleNamespace(
        agent_persona="耐心客服", business_background="万达电影票代订", reply_style="简短自然",
        human_service_hours="每日 09:00-24:00", customer_service_knowledge="",
        memory_hours=12, memory_depth=20, stage_gate_enabled=True,
        intervention_start="consultation", intervention_end="payment",
        human_takeover_delay_seconds=45,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CustomerServiceChatService(
            settings, client=client, conversation_policy_provider=lambda: policy,
        ).reply("这场多少钱？", "conversation-policy-fields")

    prompt_text = "\n".join(item["content"] for item in captured["messages"] if item["role"] == "system")
    assert "会话记忆：最近 20 条文字消息，最长 12 小时。" in prompt_text
    assert "阶段门禁：开启；允许介入范围 consultation 至 payment。" in prompt_text
    assert "人工接管：人工回复后暂停 Agent 45 秒。" in prompt_text


@pytest.mark.asyncio
async def test_customer_service_requests_stage_specific_knowledge() -> None:
    stages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    entry = KnowledgeEntry(
        id="kb-000000000002", title="报价问答", category="报价规则",
        common_questions="多少钱？", reply_guidance="查询实时价格。", handling_rules="不得估算。",
    )

    def provider(stage: str) -> list[KnowledgeEntry]:
        stages.append(stage)
        return [entry]

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1", chat_prompt="客服")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CustomerServiceChatService(settings, client=client, knowledge_provider=provider).reply(
            "这次多少钱？", "conversation-stage",
        )

    assert stages == ["consultation"]


@pytest.mark.asyncio
async def test_customer_service_routes_knowledge_from_runtime_stage_not_image_presence() -> None:
    stages: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    def provider(stage: str) -> list[KnowledgeEntry]:
        stages.append(stage)
        return []

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1", chat_prompt="客服")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CustomerServiceChatService(settings, client=client, knowledge_provider=provider).reply(
            "怎么退款？", "conversation-after-sales",
            runtime_context={"current_stage": "shipping_refund"},
        )

    assert stages == ["after_sales"]


@pytest.mark.asyncio
async def test_customer_service_reuses_structured_quote_and_recent_text_context() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        number = len(requests)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": f"客服回复{number}"}}],
        })

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是万达电影票客服。",
    )
    recognition = MovieImageInfo(
        cinema_name="北京寰映影城合生汇店", movie_name="蜘蛛侠：崭新之日",
        date_text="今天 8月25日", showtime_start="10:40",
        selected_count_visible=0, confidence=0.9,
    )
    quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=6030,
        needs_ticket_count=True, pricing_source="万达临时锁座 W+会员专享优惠",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.remember_image_context("conversation-1", recognition, quote, None)
        assert await service.reply("这个多少钱？", "conversation-1") == "客服回复1"
        assert await service.reply("是哪家影院？", "conversation-1") == "客服回复2"

    first_messages = requests[0]["messages"]
    assert first_messages[0]["role"] == "system"
    assert "【AI扩展回答权限】" in first_messages[0]["content"]
    assert "北京寰映影城合生汇店" in first_messages[1]["content"]
    assert '"unit_quote_cents":6030' in first_messages[1]["content"]
    assert "不得重复索要截图或已知字段" in first_messages[1]["content"]
    assert first_messages[-1] == {"role": "user", "content": "这个多少钱？"}
    second_messages = requests[1]["messages"]
    assert any(
        item["role"] == "system" and "只回答当前这条最新完整问题" in item["content"]
        for item in second_messages
    )
    assert {"role": "user", "content": "这个多少钱？"} in second_messages
    assert {"role": "assistant", "content": "客服回复1"} in second_messages
    assert second_messages[-1] == {"role": "user", "content": "是哪家影院？"}


@pytest.mark.asyncio
async def test_customer_service_uses_recent_platform_buyer_and_manual_seller_history() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "好的，按您刚才确认的7排中间两个位置处理。"}}],
        })

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    recent = [
        {"direction": "inbound", "messageId": "buyer-1", "sentAtMs": 1000, "messageType": 1, "content": "7排中间两个位置能买吗"},
        {"direction": "seller", "messageId": "seller-1", "sentAtMs": 2000, "messageType": 1, "content": "可以买，我给您安排7排17、18座"},
        {"direction": "inbound", "messageId": "buyer-current", "sentAtMs": 3000, "messageType": 1, "content": "那就这样"},
        {"direction": "inbound", "messageId": "image-1", "sentAtMs": 500, "messageType": 2, "content": "https://img.example/secret.jpg"},
        {"direction": "inbound", "messageId": "status-1", "sentAtMs": 2500, "messageType": 26, "content": "我已付款"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.sync_platform_history("conversation-1", recent, current_message_id="buyer-current")
        reply = await service.reply("那就这样", "conversation-1")

    assert reply == "好的，按您刚才确认的7排中间两个位置处理。"
    assert {"role": "user", "content": "7排中间两个位置能买吗"} in captured["messages"]
    assert {"role": "assistant", "content": "可以买，我给您安排7排17、18座"} in captured["messages"]
    assert captured["messages"].count({"role": "user", "content": "那就这样"}) == 1
    assert all("img.example" not in item["content"] for item in captured["messages"])
    assert all("我已付款" not in item["content"] for item in captured["messages"])


@pytest.mark.asyncio
async def test_platform_history_explicitly_marks_manual_seller_messages_for_agent() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到，我按人工客服刚才的说明继续。"}}]})

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    recent = [
        {"direction": "seller", "messageId": "human-1", "sentAtMs": 2000,
         "messageType": 1, "content": "先把选座截图发我，我来核价。"},
        {"direction": "seller", "messageId": "human-image-1", "sentAtMs": 2500,
         "messageType": 2,
         "content": "https://img.alicdn.com/human-seat-map.jpg",
         "imageUrls": ["https://img.alicdn.com/human-seat-map.jpg"]},
        {"direction": "seller", "messageId": "agent-1", "sentAtMs": 3000,
         "messageType": 1, "agent_generated": True, "content": "请稍等。"},
        {"direction": "inbound", "messageId": "buyer-current", "sentAtMs": 4000,
         "messageType": 1, "content": "好的"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.sync_platform_history("conversation-1", recent, current_message_id="buyer-current")
        await service.reply("好的", "conversation-1")

    system_messages = [
        item["content"] for item in captured["messages"]
        if item["role"] == "system" and "人工客服历史" in item["content"]
    ]
    assert system_messages
    assert "先把选座截图发我，我来核价。" in system_messages[0]
    assert "https://img.alicdn.com/human-seat-map.jpg" in system_messages[0]
    assert "调用 recognize_screenshot" in system_messages[0]
    assert "请稍等。" not in system_messages[0]
    assert {"role": "assistant", "content": "先把选座截图发我，我来核价。"} in captured["messages"]
    assert {"role": "assistant", "content": "请稍等。"} in captured["messages"]


@pytest.mark.asyncio
async def test_customer_service_caps_history_for_model_latency() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到。"}}]})

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。", chat_context_messages=4,
    )
    recent = [
        {"direction": "inbound" if index % 2 == 0 else "seller", "messageId": f"m-{index}",
         "sentAtMs": index + 1, "messageType": 1, "content": f"历史消息{index}"}
        for index in range(8)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.sync_platform_history("conversation-1", recent)
        await service.reply("当前问题", "conversation-1")

    model_messages = captured["messages"]
    history_text = [item["content"] for item in model_messages if item["role"] in {"user", "assistant"}]
    assert history_text[-1] == "当前问题"
    assert len(history_text) == 5
    assert "历史消息0" not in history_text
    assert "历史消息7" in history_text


@pytest.mark.asyncio
async def test_platform_history_excludes_messages_older_than_policy_window() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到。"}}]})

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    reference_time_ms = 1_800_000_000_000
    recent = [
        {"direction": "seller", "messageId": "old", "sentAtMs": reference_time_ms - 2 * 86_400_000, "messageType": 1, "content": "前几天没及时回复"},
        {"direction": "inbound", "messageId": "recent", "sentAtMs": reference_time_ms - 10_000, "messageType": 1, "content": "今天的新问题"},
        {"direction": "inbound", "messageId": "current", "sentAtMs": reference_time_ms, "messageType": 1, "content": "在么"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(
            settings, client=client,
            conversation_policy_provider=lambda: SimpleNamespace(ttl_seconds=86_400, memory_depth=50),
        )
        service.sync_platform_history(
            "conversation-1", recent, current_message_id="current", reference_time_ms=reference_time_ms,
        )
        await service.reply("在么", "conversation-1")

    assert {"role": "user", "content": "今天的新问题"} in captured["messages"]
    assert all("前几天没及时回复" not in item["content"] for item in captured["messages"])


@pytest.mark.asyncio
async def test_customer_service_never_reasks_count_declared_as_two_people() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "好的，请发送当前场次的选座截图，并告诉我需要买几张票，我帮您核对。"}}],
        })

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await CustomerServiceChatService(settings, client=client).reply(
            "明天19:30奥德赛，两人，可以代买吗？", "conversation-1",
        )

    assert "已记下需要2张票" in reply
    assert "几张" not in reply
    assert "选座截图" in reply
    assert any("已经明确需要2张票" in item["content"] for item in captured["messages"])


@pytest.mark.asyncio
async def test_customer_service_does_not_infer_quantity_from_unrelated_history() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "请问这次需要几张？"}}],
        })

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    history = [
        {"direction": "inbound", "messageId": "old-1", "sentAtMs": 1000,
         "messageType": 1, "content": "上次要两张"},
        {"direction": "seller", "messageId": "old-2", "sentAtMs": 2000,
         "messageType": 1, "content": "好的，已处理"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.sync_platform_history("conversation-1", history)
        reply = await service.reply("这次这个场次多少钱？", "conversation-1")

    assert reply == "请问这次需要几张？"
    assert "已记下需要2张票" not in reply
    assert not any("已经明确需要2张票" in item.get("content", "") for item in captured["messages"])


@pytest.mark.asyncio
async def test_customer_service_replaces_redundant_screenshot_and_count_followup() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "请重新发送选座截图，并告诉我需要几张，我再人工核对价格。"}}],
        })

    settings = Settings(
        chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1",
        chat_model="gpt-5.5", chat_prompt="你是客服。",
    )
    recognition = MovieImageInfo.model_validate({
        "cinema_name": "北京寰映影城合生汇店", "movie_name": "蜘蛛侠：崭新之日",
        "date_text": "今天 8月25日", "showtime_start": "10:40",
        "selected_seats": [{"seat_number": "7排10座"}],
        "selected_count_visible": 1, "confidence": 0.95,
    })
    quote = RealQuote.model_validate({
        "quote_scope": "exact_seats", "seat_zone_type": "优选区",
        "member_unit_price_cents": 5731, "unit_quote_cents": 5830,
        "total_quote_cents": 5830, "ticket_count": 1,
        "seat_quotes": [{
            "seat_number": "7排10座", "seat_zone_type": "优选区",
            "original_price_cents": 6690, "member_price_cents": 5731,
            "unit_quote_cents": 5830,
        }],
        "pricing_source": "万达临时锁座 W+会员专享优惠 + 后台报价规则",
        "pricing_rule_version": "pricing-test",
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CustomerServiceChatService(settings, client=client)
        service.remember_image_context("conversation-1", recognition, quote, None)
        reply = await service.reply("前面一排的会员座多少钱？", "conversation-1")

    # The relative request must not reuse the old 7排10座 quote or ask the
    # buyer to select an unselectable W+ seat. Without a successful seat/quote
    # tool round this fails closed and waits for a safe retry.
    assert reply == "系统暂时没有完成核验，请稍后重试。"
    assert "选座截图" not in reply
    assert "58.30元/张" not in reply
    assert "几张" not in reply


def test_image_chat_adds_structured_context_to_ai_conversation(tmp_path: Path) -> None:
    remembered: list[tuple[str, str, object, object]] = []

    class StubChatService:
        def remember_image_context(self, conversation_id, recognition, quote, quote_error) -> None:
            remembered.append((conversation_id, recognition.movie_name, quote, quote_error))

        async def reply(self, text: str, conversation_id: str) -> str:
            return "unused"

    class RecognitionWithMovie:
        async def recognize(self, _image, _content_type, _buyer_message="", *, prior_recognitions=None):
            return MovieImageInfo(movie_name="奥德赛", selected_count_visible=0, confidence=0.9)

    store = PersistentSettingsStore(
        tmp_path / "settings.json", protector=ReversibleProtector(),
        environment=Settings(api_key="persisted-key", chat_api_key="persisted-key"),
    )
    client = TestClient(create_app(
        service=RecognitionWithMovie(), settings_store=store,
        chat_reply_service=StubChatService(),
    ))
    response = client.post(
        "/api/chat/image-messages",
        data={"conversation_id": "conversation-1", "message_text": "帮我看看"},
        files={"image": ("seat.jpg", b"\xff\xd8\xfffixture", "image/jpeg")},
    )
    assert response.status_code == 200
    assert remembered == [("conversation-1", "奥德赛", None, None)]


def test_text_chat_endpoint_uses_ai_service_when_persisted_key_exists(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class StubChatService:
        async def reply(self, text: str, conversation_id: str) -> str:
            calls.append((text, conversation_id))
            return "这是模型生成的客服回复。"

    store = PersistentSettingsStore(
        tmp_path / "settings.json",
        protector=ReversibleProtector(),
        environment=Settings(api_key="persisted-key", chat_api_key="persisted-key"),
    )
    client = TestClient(create_app(
        service=StubRecognitionService(),
        settings_store=store,
        chat_reply_service=StubChatService(),
    ))
    response = client.post(
        "/api/chat/text-messages",
        json={"conversation_id": "conversation-1", "text": "你好，怎么买票？"},
    )

    assert response.status_code == 200
    assert response.json()["message"]["message_type"] == "ai_reply"
    assert response.json()["message"]["text"] == "这是模型生成的客服回复。"
    assert calls == [("你好，怎么买票？", "conversation-1")]


def test_public_text_chat_cannot_query_an_arbitrary_liangpiao_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    provider_calls: list[dict[str, object]] = []
    model_payloads: list[dict[str, object]] = []

    class LiangpiaoStub:
        async def order_detail(self, **kwargs: object) -> dict[str, object]:
            provider_calls.append(dict(kwargs))
            return {
                "orderNo": "other-buyers-order",
                "tickets": [{"ticketCode": "SECRET-TICKET-CODE"}],
            }

        async def aclose(self) -> None:
            return None

    async def fake_post_chat(
        _self: CustomerServiceChatService, *, url: str,
        settings: Settings, payload: dict[str, object],
    ) -> dict[str, object]:
        del url, settings
        model_payloads.append(dict(payload))
        has_tool_result = any(
            isinstance(item, dict) and item.get("role") == "tool"
            for item in payload.get("messages", [])
        )
        content = (
            json.dumps({"action": "reply", "message": "已返回其他买家的票码。"})
            if has_tool_result
            else json.dumps({
                "action": "tool_call",
                "tool": "order.detail",
                "arguments": {"orderNo": "other-buyers-order"},
            })
        )
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr(CustomerServiceChatService, "_post_chat", fake_post_chat)
    store = PersistentSettingsStore(
        tmp_path / "settings.json",
        protector=ReversibleProtector(),
        environment=Settings(api_key="vision-key", chat_api_key="chat-key"),
    )
    client = TestClient(create_app(settings_store=store, liangpiao_client=LiangpiaoStub()))

    response = client.post(
        "/api/chat/text-messages",
        json={"conversation_id": "public-debug-chat", "text": "查这个订单的票码"},
    )

    assert response.status_code == 200
    assert provider_calls == []
    assert "SECRET-TICKET-CODE" not in response.text
    registered_tools = {
        item["function"]["name"]
        for item in model_payloads[0].get("tools", [])
    }
    assert "order_detail" not in registered_tools
    assert "get_order_state" not in registered_tools
    assert all(
        item["function"]["parameters"]["additionalProperties"] is False
        for item in model_payloads[0].get("tools", [])
    )


def test_text_chat_without_key_keeps_safe_guidance_fallback(tmp_path: Path) -> None:
    store = PersistentSettingsStore(
        tmp_path / "settings.json",
        protector=ReversibleProtector(),
        environment=Settings(api_key=""),
    )
    client = TestClient(create_app(service=StubRecognitionService(), settings_store=store))
    response = client.post(
        "/api/chat/text-messages",
        json={"conversation_id": "conversation-1", "text": "你好"},
    )
    assert response.status_code == 200
    assert response.json()["message"]["message_type"] == "guidance"
    assert "上传" in response.json()["message"]["text"]
@pytest.mark.asyncio
async def test_customer_service_injects_runtime_stage_order_facts_quote_and_tools() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    settings = Settings(chat_api_key="relay-key", chat_base_url="https://airelvo.cc/v1", chat_prompt="客服")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await CustomerServiceChatService(settings, client=client).reply(
            "好的", "conversation-runtime",
            runtime_context={
                "current_stage": "quotation", "order_status": "pending",
                "confirmed_facts": {
                    "cinema": "万达影城", "movie": "测试片",
                    "ticket_count": 2, "seats": ["8排7座", "8排8座"],
                },
                "missing_fields": ["showtime_start"],
                "current_quote": {"total_quote_cents": 8000, "ticket_count": 2},
                "allowed_query_tools": ["show.list", "order.preflight"],
                "human_takeover_paused": False,
            },
        )
    prompt_text = "\n".join(item["content"] for item in captured["messages"] if item["role"] == "system")
    assert "current_stage" in prompt_text
    assert "showtime_start" in prompt_text
    assert "show.list" in prompt_text
    assert "total_quote_cents" in prompt_text
    assert "fixed_actions_backend_only=true" in prompt_text
    assert "买家已经明确需要2张票" in prompt_text
    assert "不得再次询问座位、张数或要求重复确认" in prompt_text
