from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from app.canonical_conversation_agent import AgentContextBuilder, CanonicalConversationAgent, OpenAICompatibleAgentModel
from app.config import Settings
from app.main import create_app
from app.models import VisionSettingsUpdate
from app.rules_first_store import RulesFirstStore
from app.settings_store import PersistentSettingsStore


class ReversibleProtector:
    def protect(self, value: str) -> str:
        return f"protected:{value[::-1]}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("protected:")[::-1]


def make_store(path: Path) -> PersistentSettingsStore:
    return PersistentSettingsStore(
        path,
        protector=ReversibleProtector(),
        environment=Settings(api_key="env-vision", chat_api_key="env-chat"),
    )


def update(*, chat_model: str, chat_base_url: str = "https://chat.example/v1", chat_api_key: str = "chat-key") -> VisionSettingsUpdate:
    return VisionSettingsUpdate.model_validate({
        "base_url": "https://vision.example/v1",
        "model": "vision-model",
        "chat_base_url": chat_base_url,
        "chat_model": chat_model,
        "vision_prompt": "识别电影票信息。",
        "chat_prompt": "你是客服。",
        "api_key": "vision-key",
        "chat_api_key": chat_api_key,
    })


def test_model_config_resolution_prefers_shop_then_tenant_then_global_then_env(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vision-settings.json")

    global_saved = store.save(update(chat_model="global-model"))
    tenant_saved = store.save(update(chat_model="tenant-model"), tenant_id="107")
    shop_saved = store.save(update(chat_model="shop-model"), tenant_id="107", shop_id="2313315754")

    shop = store.resolve_model_config("107", "2313315754")
    tenant = store.resolve_model_config("107", "other-shop")
    global_config = store.resolve_model_config("other-tenant", "other-shop")

    assert shop.model == "shop-model"
    assert shop.config_id == shop_saved.config_id
    assert shop.revision == shop_saved.config_revision
    assert shop.scope == "SHOP"
    assert tenant.model == "tenant-model"
    assert tenant.config_id == tenant_saved.config_id
    assert tenant.scope == "TENANT"
    assert global_config.model == "global-model"
    assert global_config.config_id == global_saved.config_id
    assert global_config.scope == "GLOBAL"


def test_unconfigured_store_uses_environment_only_as_fallback(tmp_path: Path) -> None:
    config = make_store(tmp_path / "vision-settings.json").resolve_model_config("107", "2313315754")

    assert config.model == "qwen3.5-flash-2026-02-23"
    assert config.api_key == "env-chat"
    assert config.scope == "ENVIRONMENT"
    assert config.config_id == "environment-chat"
    assert config.revision == 0


def test_settings_api_reads_and_saves_scoped_model_config_without_returning_key(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vision-settings.json")
    client = create_app(settings_store=store)
    from fastapi.testclient import TestClient

    http = TestClient(client)
    headers = {"x-wanda-tenant-id": "107", "x-wanda-shop-id": "2313315754"}
    saved = http.put("/api/settings/vision", headers=headers, json=update(chat_model="shop-model").model_dump())
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["scope"] == "SHOP"
    assert payload["config_id"]
    assert payload["config_revision"] == 1
    assert payload["has_chat_api_key"] is True
    assert "api_key" not in payload
    assert "chat_api_key" not in payload

    loaded = http.get("/api/settings/vision", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["chat_model"] == "shop-model"
    assert loaded.json()["config_id"] == payload["config_id"]


class FakeModel:
    def __init__(self, label: str, calls: list[str]) -> None:
        self.label = label
        self.calls = calls

    async def complete(self, messages, tools):
        self.calls.append(self.label)
        return {"reply": "好的。"}


def test_agent_resolves_config_per_run_and_audits_id_revision_without_context_key(tmp_path: Path) -> None:
    rules = RulesFirstStore(tmp_path / "rules.sqlite3", protector=ReversibleProtector())
    calls: list[str] = []
    current = {"config_id": "config-a", "config_revision": 1, "provider": "OpenAI-compatible", "base_url": "https://a.example/v1", "model": "a-model"}

    def resolve(_tenant: str, _shop: str, _purpose: str):
        return {"model": FakeModel(current["model"], calls), "metadata": dict(current)}

    body = {
        "envelope": {"id": "event-1", "tenantId": "107", "payload": {"accountUnb": "2313315754", "peerUnb": "buyer", "chatId": "chat", "content": {"text": "你好"}}},
        "session": {"accountUnb": "2313315754", "peerUnb": "buyer", "chatId": "chat"},
        "recent_messages": [],
    }
    agent = CanonicalConversationAgent(AgentContextBuilder(), FakeModel("unused", calls), model_resolver=resolve, audit_store=rules)

    first = asyncio.run(agent.process(body))
    assert first["status"] == "AGENT_REPLY_READY"
    assert calls == ["a-model"]
    run = rules.get_agent_run(first["agent_run_id"])
    assert run is not None
    assert run["model_config_id"] == "config-a"
    assert run["model_config_revision"] == 1
    assert "api_key" not in str(run["context"])

    current.update(config_id="config-b", config_revision=2, model="b-model")
    body["envelope"]["id"] = "event-2"
    second = asyncio.run(agent.process(body))
    assert second["status"] == "AGENT_REPLY_READY"
    assert calls == ["a-model", "b-model"]
    second_run = rules.get_agent_run(second["agent_run_id"])
    assert second_run["model_config_id"] == "config-b"
    assert second_run["model_config_revision"] == 2


def test_env_does_not_override_existing_ui_config(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vision-settings.json")
    store.save(update(chat_model="ui-model"), tenant_id="107")
    resolved = store.resolve_model_config("107", "2313315754")
    assert resolved.model == "ui-model"
    assert resolved.api_key == "chat-key"


def test_model_resolution_failure_fails_closed_instead_of_using_bootstrap_model() -> None:
    calls: list[str] = []

    def resolve(_tenant: str, _shop: str, _purpose: str):
        raise RuntimeError("settings_unavailable")

    agent = CanonicalConversationAgent(
        AgentContextBuilder(), FakeModel("bootstrap", calls), model_resolver=resolve,
    )
    body = {
        "envelope": {"id": "event-1", "tenantId": "107", "payload": {"accountUnb": "2313315754", "peerUnb": "buyer", "chatId": "chat", "content": {"text": "你好"}}},
        "session": {"accountUnb": "2313315754", "peerUnb": "buyer", "chatId": "chat"},
        "recent_messages": [],
    }

    result = asyncio.run(agent.process(body))
    assert result["status"] == "AGENT_REPLY_UNAVAILABLE"
    assert result["reason"] == "agent_model_failed"
    assert calls == []


def test_plain_and_tools_calls_use_the_resolved_ui_endpoint_and_model() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    async def scenario() -> None:
        model = OpenAICompatibleAgentModel(
            api_key="not-persisted-test-key", base_url="https://ui-config.example/v1", model="ui-model",
            transport=httpx.MockTransport(handler),
        )
        plain = await model.complete([{"role": "user", "content": "Reply with OK."}], ())
        tools = await model.complete([{"role": "user", "content": "Reply with OK."}], ({"type": "function", "function": {"name": "get_current_context"}},))
        assert plain["reply"] == "OK"
        assert tools["reply"] == "OK"

    asyncio.run(scenario())
    assert [item["url"] for item in requests] == [
        "https://ui-config.example/v1/chat/completions",
        "https://ui-config.example/v1/chat/completions",
    ]
    assert [item["body"]["model"] for item in requests] == ["ui-model", "ui-model"]
    assert requests[0]["body"]["tools"] == []
    assert requests[1]["body"]["tools"]
