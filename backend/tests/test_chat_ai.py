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


def test_huanying_cinema_is_not_rejected_as_non_wanda_before_authoritative_match() -> None:
    recognition = MovieImageInfo(
        cinema_name="北京寰映影城合生汇店", movie_name="奥德赛",
        date_text="今天 8月25日", showtime_start="19:30",
    )

    reply = _safe_quote_failure_reply(recognition, "影院无法在万达官方影院缓存中匹配", ReplyTemplates())

    assert reply is not None
    assert "目前仅支持万达影城" not in reply
    assert "无法唯一匹配万达官方门店" in reply


class StubRecognitionService:
    async def recognize(self, _image: bytes, _content_type: str, _buyer_message: str = "", *, prior_recognitions: list[MovieImageInfo] | None = None) -> MovieImageInfo:
        return MovieImageInfo(selected_count_visible=0)


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
    assert captured["max_completion_tokens"] == 500
    assert captured["messages"][0] == {"role": "system", "content": "你是万达电影票客服。"}
    assert captured["messages"][1] == {"role": "user", "content": "怎么买票？"}
    assert "response_format" not in captured


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
        common_questions="怎么确认？", reply_guidance="请回复正确。", handling_rules="过期重新询价。",
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
    assert "处理规则：过期重新询价。" in prompt_text


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
    assert first_messages[0] == {"role": "system", "content": "你是万达电影票客服。"}
    assert "北京寰映影城合生汇店" in first_messages[1]["content"]
    assert '"unit_quote_cents":6030' in first_messages[1]["content"]
    assert "不得重复索要截图或已知字段" in first_messages[1]["content"]
    assert first_messages[-1] == {"role": "user", "content": "这个多少钱？"}
    second_messages = requests[1]["messages"]
    assert {"role": "user", "content": "这个多少钱？"} in second_messages
    assert {"role": "assistant", "content": "客服回复1"} in second_messages
    assert second_messages[-1] == {"role": "user", "content": "是哪家影院？"}


@pytest.mark.asyncio
async def test_wplus_purchase_question_gets_deterministic_supported_reply() -> None:
    service = CustomerServiceChatService(Settings())

    reply = await service.reply("W+座位能代买吗？", "conversation-1")

    assert reply.startswith("可以买，万达W+会员座位支持代订")
    assert "选座截图" in reply
    assert "只凭颜色确认" in reply


@pytest.mark.asyncio
async def test_gray_seat_followup_reuses_verified_wplus_quote_and_known_count() -> None:
    service = CustomerServiceChatService(Settings())
    recognition = MovieImageInfo(
        cinema_name="济南魏家庄万达广场店", movie_name="蜘蛛侠：崭新之日",
        date_text="今天 08月25日", showtime_start="19:45",
        selected_count_visible=0, confidence=0.95,
    )
    quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=3800,
        needs_ticket_count=True, pricing_source="万达临时锁座 W+会员专享优惠",
    )
    service.remember_image_context("conversation-1", recognition, quote, None)

    reply = await service.reply("中间那几个灰色的，买2张", "conversation-1")

    assert reply.startswith("可以买，万达W+会员座位支持代订")
    assert "38.00元/张" in reply
    assert "已记下需要2张" in reply
    assert "请告诉我需要几张" not in reply


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

    assert "58.30元/张" in reply
    assert "合计 58.30元" in reply
    assert "7排10座" in reply
    assert "重新发送" not in reply
    assert "几张" not in reply
    assert "只补充新的具体排数或座位号" in reply


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
