from __future__ import annotations

from pathlib import Path

import pytest

from app.chat_service import ConversationChatStore
from app.conversation_policy_store import ConversationPolicyStore
from app.models import MovieImageInfo, RealQuote
from app.plugin_automation import PluginAutomation


def test_conversation_policy_defaults_to_per_user_24_hours_and_50_messages(tmp_path: Path) -> None:
    store = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    current = store.current()
    assert current.memory_hours == 24
    assert current.memory_depth == 50
    assert current.human_takeover_delay_seconds == 20
    assert current.ai_reply_enabled is True
    assert "W+未标记" in current.customer_service_knowledge
    assert "重新发送一张标记好的截图" in current.customer_service_knowledge
    assert "拍下后先不要付款" in current.customer_service_knowledge
    saved = store.save({**current.model_dump(), "memory_hours": 12, "memory_depth": 30})
    assert saved.revision == 1
    assert ConversationPolicyStore(tmp_path / "conversation-policy.json").current().memory_depth == 30


def test_ai_reply_switch_is_dynamic_and_partial_updates_preserve_other_policy_fields(tmp_path: Path) -> None:
    store = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    store.save({"memory_depth": 30, "human_takeover_delay_seconds": 45})

    saved = store.save({"ai_reply_enabled": False})

    assert saved.ai_reply_enabled is False
    assert saved.memory_depth == 30
    assert saved.human_takeover_delay_seconds == 45
    assert saved.revision == 2


def test_legacy_keyword_confirmation_knowledge_migrates_to_current_playbook(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversation-policy.json"
    policy = ConversationPolicyStore(path).save({
        "customer_service_knowledge": (
            "报价发送前检查信息完整，报价后引导买家核对并明确回复“正确”。"
            "信息不确定时一律转人工。"
        ),
    })
    assert "明确回复“正确”" in policy.customer_service_knowledge

    migrated = ConversationPolicyStore(path).current()

    assert "明确回复“正确”" not in migrated.customer_service_knowledge
    assert "W+未标记" in migrated.customer_service_knowledge
    assert "拍下后先不要付款" in migrated.customer_service_knowledge


def test_business_background_save_clears_legacy_persona_background(tmp_path: Path) -> None:
    store = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    store.save({"persona_background": "旧业务背景"})
    migrated = store.current()
    assert migrated.business_background == "旧业务背景"
    assert migrated.persona_background == ""
    saved = store.save({"business_background": "新业务背景"})
    assert saved.business_background == "新业务背景"
    assert saved.persona_background == ""


def test_chat_memory_depth_changes_without_restarting_store(tmp_path: Path) -> None:
    policies = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    memory = ConversationChatStore(policy_provider=policies.current)
    for index in range(30):
        memory.add_exchange("tenant:shop:chat", f"u{index}", f"a{index}")
    messages, _ = memory.snapshot("tenant:shop:chat")
    assert len(messages) == 50
    policies.save({**policies.current().model_dump(), "memory_depth": 10})
    memory.add_exchange("tenant:shop:chat", "latest", "reply")
    messages, _ = memory.snapshot("tenant:shop:chat")
    assert len(messages) == 10


@pytest.mark.asyncio
async def test_ai_reply_switch_suppresses_only_generic_model_reply() -> None:
    class Recognition:
        async def recognize(self, image: bytes, content_type: str, buyer_message: str = ""):
            return MovieImageInfo(movie_name="测试", selected_count_visible=0)

    class Quote:
        async def quote(self, recognition: MovieImageInfo):
            return RealQuote(quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5000)

    class Chat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("AI reply switch must run before the generic model")

    class Policy:
        ai_reply_enabled = False
        human_takeover_delay_seconds = 20
        stage_gate_enabled = False

    automation = PluginAutomation(
        Recognition(), Quote(), mode="auto", image_loader=lambda _: None,
        chat_service=Chat(), conversation_policy_provider=Policy,
    )
    body = {
        "envelope": {"id": "event-ai-off", "tenantId": "tenant-1", "event": "im.message.received", "timestamp": 1_787_580_100_000, "payload": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1", "remoteMessageId": "buyer-ai-off"}},
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "order": None,
        "recent_messages": [{"direction": "inbound", "messageId": "buyer-ai-off", "sentAtMs": 1_787_580_100_000, "messageType": 1, "content": "能不能推荐一下", "imageUrls": []}],
    }

    result = await automation.process_event(body)

    assert result["decision"] == {"mode": "auto", "actions": [], "reason": "generic_ai_reply_disabled"}


@pytest.mark.asyncio
async def test_ai_reply_switch_keeps_image_recognition_and_deterministic_quote_enabled() -> None:
    calls = {"recognition": 0, "quote": 0}

    class Recognition:
        async def recognize(self, image: bytes, content_type: str, buyer_message: str = ""):
            calls["recognition"] += 1
            return MovieImageInfo(
                movie_name="测试电影", city="深圳", cinema_name="深圳万达广场店",
                date_text="明天", showtime_start="19:30", selected_count_visible=0,
            )

    class Quote:
        async def quote(self, recognition: MovieImageInfo):
            calls["quote"] += 1
            return RealQuote(
                quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5000,
                needs_ticket_count=True, pricing_source="official",
            )

    class Policy:
        ai_reply_enabled = False
        human_takeover_delay_seconds = 20
        stage_gate_enabled = False

    async def load_image(_: str) -> tuple[bytes, str]:
        return b"image", "image/jpeg"

    automation = PluginAutomation(
        Recognition(), Quote(), mode="auto", image_loader=load_image,
        conversation_policy_provider=Policy,
    )
    image_url = "https://img.alicdn.com/ticket.jpg"
    body = {
        "envelope": {"id": "event-image-ai-off", "tenantId": "tenant-1", "event": "im.message.received", "timestamp": 1_787_580_100_000, "payload": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1", "remoteMessageId": "buyer-image", "messageType": 2, "imageUrls": [image_url]}},
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "order": None,
        "recent_messages": [{"direction": "inbound", "messageId": "buyer-image", "sentAtMs": 1_787_580_100_000, "messageType": 2, "content": image_url, "imageUrls": [image_url]}],
    }

    result = await automation.process_event(body)

    assert calls == {"recognition": 1, "quote": 1}
    assert result["decision"]["actions"][0]["type"] == "send_message"
    assert "50.00" in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_recent_human_reply_holds_only_generic_ai_reply_for_configured_delay() -> None:
    class Recognition:
        async def recognize(self, image: bytes, content_type: str):
            return MovieImageInfo(movie_name="测试", selected_count_visible=0)

    class Quote:
        async def quote(self, recognition: MovieImageInfo):
            return RealQuote(quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5000)

    class Chat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("human cooldown must run before AI")

    class Policy:
        human_takeover_delay_seconds = 20

    automation = PluginAutomation(
        Recognition(), Quote(), mode="auto", image_loader=lambda _: None,
        chat_service=Chat(), conversation_policy_provider=Policy,
    )
    body = {
        "envelope": {"id": "event-1", "tenantId": "tenant-1", "event": "im.message.received", "timestamp": 1_787_580_100_000, "payload": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1", "remoteMessageId": "buyer-new"}},
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "order": None,
        "recent_messages": [
            {"direction": "seller", "messageId": "human-1", "sentAtMs": 1_787_580_090_000, "content": "人工回复"},
            {"direction": "inbound", "messageId": "buyer-new", "sentAtMs": 1_787_580_100_000, "messageType": 1, "content": "想问一下具体怎么操作", "imageUrls": []},
        ],
    }
    result = await automation.process_event(body)
    assert result["decision"]["reason"] == "human_takeover_cooldown"


@pytest.mark.asyncio
async def test_recent_human_reply_does_not_stop_deterministic_keyword_rule() -> None:
    class Recognition:
        async def recognize(self, image: bytes, content_type: str):
            return MovieImageInfo(movie_name="测试", selected_count_visible=0)

    class Quote:
        async def quote(self, recognition: MovieImageInfo):
            return RealQuote(quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5000)

    class Chat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("keyword rule must bypass generic AI")

    class Policy:
        human_takeover_delay_seconds = 20

    from app.reply_template_store import ReplyTemplates
    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "hours", "keywords": ["营业时间"], "match_mode": "exact",
            "reply": "每天10点到22点。",
        }],
    })
    automation = PluginAutomation(
        Recognition(), Quote(), mode="auto", image_loader=lambda _: None,
        chat_service=Chat(), conversation_policy_provider=Policy,
        template_provider=lambda: templates,
    )
    body = {
        "envelope": {"id": "event-rule", "tenantId": "tenant-1", "event": "im.message.received", "timestamp": 1_787_580_100_000, "payload": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1", "remoteMessageId": "buyer-rule"}},
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "order": None,
        "recent_messages": [
            {"direction": "seller", "messageId": "human-1", "sentAtMs": 1_787_580_090_000, "content": "人工图片", "messageType": 2},
            {"direction": "inbound", "messageId": "buyer-rule", "sentAtMs": 1_787_580_100_000, "messageType": 1, "content": "营业时间", "imageUrls": []},
        ],
    }

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "human_takeover_cooldown"
    assert result["decision"]["actions"] == []
