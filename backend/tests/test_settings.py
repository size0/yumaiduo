from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge_store import KnowledgeStore
from app.main import create_app
from app.models import VisionSettingsUpdate
from app.prompts import DEFAULT_CHAT_PROMPT
from app.service import MovieImageRecognitionService
from app.settings_store import AesGcmSecretProtector, PersistentSettingsStore


class ReversibleTestProtector:
    def protect(self, value: str) -> str:
        return "protected:" + value[::-1]

    def unprotect(self, value: str) -> str:
        assert value.startswith("protected:")
        return value.removeprefix("protected:")[::-1]


def test_aes_gcm_secret_protector_round_trip_for_linux_storage() -> None:
    protector = AesGcmSecretProtector(bytes(range(32)))
    protected = protector.protect("remote-secret")

    assert protected.startswith("aesgcm:")
    assert "remote-secret" not in protected
    assert protector.unprotect(protected) == "remote-secret"


class UnusedRecognitionService:
    async def recognize(self, _image: bytes, _content_type: str, _buyer_message: str = "", *, prior_recognitions=None):
        raise AssertionError("recognition should not be called")


def make_store(path: Path) -> PersistentSettingsStore:
    return PersistentSettingsStore(
        path,
        protector=ReversibleTestProtector(),
        environment=Settings(api_key=""),
    )


def test_default_vision_settings_use_requested_flash_snapshot(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vision-settings.json")
    current = store.current()

    assert current.model == "qwen3.5-flash-2026-02-23"
    assert current.chat_model == "qwen3.5-flash-2026-02-23"
    assert current.chat_base_url == current.base_url
    assert current.chat_api_key == current.api_key
    assert current.enable_thinking is False
    assert current.reasoning_effort == "none"
    assert "电影票" in current.vision_prompt
    assert "客服" in current.chat_prompt


def test_legacy_single_model_settings_migrate_both_stages_to_same_model(tmp_path: Path) -> None:
    path = tmp_path / "vision-settings.json"
    path.write_text(json.dumps({
        "version": 1,
        "base_url": "https://relay.example/v1",
        "model": "legacy-vision-model",
        "api_key_protected": "protected:" + "legacy-key"[::-1],
    }), encoding="utf-8")
    current = make_store(path).current()
    assert current.model == "legacy-vision-model"
    assert current.chat_model == "legacy-vision-model"
    assert current.chat_base_url == "https://relay.example/v1"
    assert current.api_key == "legacy-key"
    assert current.chat_api_key == "legacy-key"


def test_settings_api_persists_model_thinking_prompt_and_encrypted_key(tmp_path: Path) -> None:
    path = tmp_path / "vision-settings.json"
    store = make_store(path)
    client = TestClient(create_app(service=UnusedRecognitionService(), settings_store=store))

    saved = client.put(
        "/api/settings/vision",
        json={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-flash-2026-02-23",
            "chat_base_url": "https://airelvo.cc/v1",
            "chat_model": "gpt-5.5",
            "enable_thinking": True,
            "reasoning_effort": "low",
            "vision_prompt": "只提取清晰可见的电影票信息。",
            "chat_prompt": "你是测试电影票客服。",
            "api_key": "test-persistent-secret",
            "clear_api_key": False,
            "chat_api_key": "test-chat-secret",
            "clear_chat_api_key": False,
        },
    )

    assert saved.status_code == 200
    body = saved.json()
    assert body["has_api_key"] is True
    assert body["masked_api_key"] == "***cret"
    assert body["has_chat_api_key"] is True
    assert body["masked_chat_api_key"] == "***cret"
    assert "api_key" not in body
    raw_file = path.read_text(encoding="utf-8")
    assert "test-persistent-secret" not in raw_file
    assert "test-chat-secret" not in raw_file
    assert "protected:" in raw_file

    restarted_store = make_store(path)
    restarted = restarted_store.current()
    assert restarted.api_key == "test-persistent-secret"
    assert restarted.model == "qwen3.5-flash-2026-02-23"
    assert restarted.chat_base_url == "https://airelvo.cc/v1"
    assert restarted.chat_api_key == "test-chat-secret"
    assert restarted.chat_model == "gpt-5.5"
    assert restarted.enable_thinking is True
    assert restarted.reasoning_effort == "low"
    assert restarted.vision_prompt == "只提取清晰可见的电影票信息。"
    assert restarted.chat_prompt == "你是测试电影票客服。"


def test_settings_api_blank_key_preserves_existing_key_and_can_clear_it(tmp_path: Path) -> None:
    store = PersistentSettingsStore(
        tmp_path / "vision-settings.json",
        protector=ReversibleTestProtector(),
        environment=Settings(api_key="environment-fallback-secret"),
    )
    client = TestClient(create_app(service=UnusedRecognitionService(), settings_store=store))
    base = {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.5-flash-2026-02-23",
        "enable_thinking": False,
        "vision_prompt": "识别电影票信息。",
        "clear_api_key": False,
    }
    assert client.put("/api/settings/vision", json={**base, "api_key": "first-secret"}).status_code == 200
    preserved = client.put("/api/settings/vision", json={**base, "api_key": None})
    assert preserved.json()["has_api_key"] is True
    cleared = client.put("/api/settings/vision", json={**base, "api_key": None, "clear_api_key": True})
    assert cleared.json()["has_api_key"] is False
    assert store.current().api_key == ""


def test_saved_settings_apply_to_the_next_recognition_without_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vision-settings.json")
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"movie_name":"奥德赛","selected_count_visible":0,"confidence":0.9}'}}]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            service = MovieImageRecognitionService(store.current, client=http_client)
            store.save(VisionSettingsUpdate.model_validate({
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-first-model",
                "chat_base_url": "https://chat-first.example/v1", "chat_model": "gpt-first-model", "enable_thinking": False, "vision_prompt": "第一次提示词", "api_key": "saved-secret", "chat_api_key": "saved-chat-secret",
            }))
            await service.recognize(b"\xff\xd8\xfffixture", "image/jpeg")
            store.save(VisionSettingsUpdate.model_validate({
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-second-model",
                "chat_base_url": "https://chat-second.example/v1", "chat_model": "gpt-second-model", "enable_thinking": True, "vision_prompt": "第二次提示词", "api_key": None, "chat_api_key": None,
            }))
            await service.recognize(b"\xff\xd8\xfffixture", "image/jpeg")

    asyncio.run(scenario())
    assert [item["model"] for item in payloads] == ["qwen-first-model", "qwen-second-model"]
    assert payloads[1]["enable_thinking"] is True
    assert "第二次提示词" in payloads[1]["messages"][0]["content"]


def test_default_chat_prompt_integrates_wplus_context_and_authority_rules() -> None:
    assert "【店铺背景】" in DEFAULT_CHAT_PROMPT
    assert "支持代订万达W+会员座位" in DEFAULT_CHAT_PROMPT
    assert "先明确回复“可以买”" not in DEFAULT_CHAT_PROMPT
    assert "不得重复索要截图或已知字段" not in DEFAULT_CHAT_PROMPT
    assert "不重复索要截图或已知字段" in DEFAULT_CHAT_PROMPT
    assert "不得自行计算或生成价格" in DEFAULT_CHAT_PROMPT
    assert "新会话只做正常问候" in DEFAULT_CHAT_PROMPT
    assert "主动解释其含义、选择方式和核验流程" not in DEFAULT_CHAT_PROMPT


def test_knowledge_api_supports_seed_edit_enable_and_delete(tmp_path: Path) -> None:
    knowledge = KnowledgeStore(tmp_path / "knowledge.json")
    client = TestClient(create_app(service=UnusedRecognitionService(), knowledge_store=knowledge))

    listed = client.get("/api/settings/knowledge")
    assert listed.status_code == 200
    entries = listed.json()["entries"]
    assert len(entries) == 18
    entry = entries[0]
    updated = client.put(f"/api/settings/knowledge/{entry['id']}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    deleted = client.delete(f"/api/settings/knowledge/{entry['id']}")
    assert deleted.status_code == 200
    assert all(item["id"] != entry["id"] for item in client.get("/api/settings/knowledge").json()["entries"])


def test_conversation_policy_persists_customer_service_strategy(tmp_path: Path) -> None:
    from app.conversation_policy_store import ConversationPolicyStore

    policy = ConversationPolicyStore(tmp_path / "conversation-policy.json")
    client = TestClient(create_app(service=UnusedRecognitionService(), conversation_policy_store=policy))
    saved = client.put("/api/settings/conversation-policy", json={
        "agent_persona": "耐心的真人店主",
        "business_background": "只提供万达官方实时购票服务。",
        "customer_service_knowledge": "退款问题统一转人工。",
        "reply_style": "简短、自然、先回答再引导。",
        "human_service_hours": "工作日 10:00-22:00",
    })
    assert saved.status_code == 200
    assert saved.json()["agent_persona"] == "耐心的真人店主"
    assert client.get("/api/settings/conversation-policy").json()["human_service_hours"] == "工作日 10:00-22:00"


def test_settings_page_contract_is_present_in_chat_ui(tmp_path: Path) -> None:
    client = TestClient(create_app(service=UnusedRecognitionService(), settings_store=make_store(tmp_path / "settings.json")))
    html = client.get("/").text

    assert 'id="siteNavigation"' in html
    assert 'id="workspaceChat"' in html
    assert 'data-workspace="chat"' in html
    assert 'data-workspace="pricing"' in html
    assert 'data-workspace="model"' in html
    assert 'data-workspace="knowledge"' in html
    assert 'data-workspace="safety"' in html
    assert 'data-workspace="logs"' in html
    assert "客服工作台" in html
    assert "运营报价" in html
    assert "模型接口" in html
    assert "知识库" in html
    assert 'id="knowledgeList"' in html
    assert "W+代订与纯文字咨询" in html
    assert "当前展示的是迁移审核快照" in html
    assert "安全门禁" in html
    assert "运行日志" in html
    assert 'id="settingsDrawer"' in html
    assert 'id="modelSelect"' in html
    assert 'id="chatBaseUrl"' in html
    assert 'id="chatApiKey"' in html
    assert 'id="chatModelSelect"' in html
    assert "万达官方直连" in html
    assert 'id="fetchChatModels"' in html
    assert 'id="thinkingToggle"' in html
    assert 'id="reasoningEffort"' in html
    assert 'id="visionPrompt"' in html
    assert 'id="chatPrompt"' in html
    assert 'id="operationsSettingsTab"' in html
    assert 'id="pricingEnabled"' in html
    assert 'id="regularMarkup"' in html
    assert 'id="wplusDiscount"' in html
    assert 'id="wplusThreshold"' in html
    assert 'id="previewMember"' in html
    assert 'id="pricingPreviewResult"' in html
    assert "/api/settings/vision" in html
    assert "/api/settings/operations" in html
    assert "data.seat_display||'W+座位'" in html
