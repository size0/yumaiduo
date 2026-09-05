from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.rules_first_store import RulesFirstStore
from app.shop_automation_store import ShopAutomationStore


class LegacyRecognitionSpy:
    def __init__(self) -> None:
        self.recognize_calls = 0
        self.recognize_from_url_calls = 0

    async def recognize(self, *args, **kwargs):
        self.recognize_calls += 1
        raise AssertionError("legacy recognize must not run for canonical shops")

    async def recognize_from_url(self, *args, **kwargs):
        self.recognize_from_url_calls += 1
        raise AssertionError("legacy recognize_from_url must not run for canonical shops")


class LegacyQuoteSpy:
    def __init__(self) -> None:
        self.quote_calls = 0

    async def quote(self, *args, **kwargs):
        self.quote_calls += 1
        raise AssertionError("legacy quote must not run for canonical shops")


class LegacyChatSpy:
    def __init__(self) -> None:
        self.reply_calls = 0

    async def reply(self, *args, **kwargs):
        self.reply_calls += 1
        raise AssertionError("legacy agent must not run for canonical shops")


class CanonicalImageRuntimeStub:
    def __init__(self, status: str = "CANONICAL_QUOTE_UNAVAILABLE") -> None:
        self.status = status
        self.calls = 0

    async def process_image_event(self, body):
        self.calls += 1
        return {"status": self.status, "reason": "test"}


class CanonicalAgentStub:
    def __init__(self, status: str = "AGENT_REPLY_UNAVAILABLE") -> None:
        self.status = status
        self.calls = 0

    async def process(self, body):
        self.calls += 1
        return {"status": self.status, "reason": "test", "reply": "", "actions": []}


IDENTITY = {
    "tenant_id": "107",
    "shop_id": "2313315754",
    "buyer_id": "2217098857081",
    "chat_id": "66166718230",
}


def _shop_store(tmp_path: Path) -> ShopAutomationStore:
    store = ShopAutomationStore(tmp_path / "shops.json")
    store.sync("107", [{"accountUnb": "2313315754", "shopName": "canary"}])
    store.set_settings(
        "107", "2313315754",
        enabled=True,
        canonical_quote_enabled=True,
        canonical_conversation_enabled=True,
    )
    return store


def _image_event(event_id: str = "image-event-1") -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id,
            "tenantId": IDENTITY["tenant_id"],
            "event": "im.message.received",
            "payload": {
                "accountUnb": IDENTITY["shop_id"],
                "peerUnb": IDENTITY["buyer_id"],
                "chatId": IDENTITY["chat_id"],
                "remoteMessageId": event_id,
                "messageType": 2,
                "imageUrls": ["https://img.alicdn.com/canonical.webp"],
            },
        },
        "session": {
            "accountUnb": IDENTITY["shop_id"],
            "peerUnb": IDENTITY["buyer_id"],
            "chatId": IDENTITY["chat_id"],
        },
        "recent_messages": [],
    }


def _text_event(event_id: str = "text-event-1", *, text: str = "价钱多少") -> dict[str, object]:
    return {
        "envelope": {
            "id": event_id,
            "tenantId": IDENTITY["tenant_id"],
            "event": "im.message.received",
            "payload": {
                "accountUnb": IDENTITY["shop_id"],
                "peerUnb": IDENTITY["buyer_id"],
                "chatId": IDENTITY["chat_id"],
                "remoteMessageId": event_id,
                "messageType": 1,
                "content": {"text": text},
            },
        },
        "session": {
            "accountUnb": IDENTITY["shop_id"],
            "peerUnb": IDENTITY["buyer_id"],
            "chatId": IDENTITY["chat_id"],
        },
        "recent_messages": [],
    }


def test_canonical_image_failure_does_not_call_legacy_recognition_or_quote(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    monkeypatch.setenv("CANONICAL_QUOTE_RUNTIME_ENABLED", "true")
    legacy_recognition = LegacyRecognitionSpy()
    legacy_quote = LegacyQuoteSpy()
    runtime = CanonicalImageRuntimeStub()
    client = TestClient(create_app(
        service=legacy_recognition,
        quote_service=legacy_quote,
        chat_reply_service=LegacyChatSpy(),
        canonical_quote_runtime=runtime,
        shop_automation_store=_shop_store(tmp_path),
        rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    ))

    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process",
        json=_image_event(),
        headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
    )

    assert response.status_code == 202
    assert response.json()["canonical_quote_status"] == "CANONICAL_QUOTE_UNAVAILABLE"
    assert runtime.calls == 1
    assert legacy_recognition.recognize_calls == 0
    assert legacy_recognition.recognize_from_url_calls == 0
    assert legacy_quote.quote_calls == 0


def test_canonical_text_failure_does_not_call_legacy_agent_or_chat(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-test-secret")
    legacy_recognition = LegacyRecognitionSpy()
    legacy_quote = LegacyQuoteSpy()
    legacy_chat = LegacyChatSpy()
    agent = CanonicalAgentStub()
    client = TestClient(create_app(
        service=legacy_recognition,
        quote_service=legacy_quote,
        chat_reply_service=legacy_chat,
        canonical_conversation_agent=agent,
        shop_automation_store=_shop_store(tmp_path),
        rules_first_store=RulesFirstStore(tmp_path / "rules.sqlite3"),
    ))

    response = client.post(
        "/api/wanda-ai-v2/plugin/events/process",
        json=_text_event(),
        headers={"X-Wanda-AI-V2-Bridge-Key": "bridge-test-secret"},
    )

    assert response.status_code == 202
    assert response.json()["canonical_agent_status"] == "AGENT_REPLY_UNAVAILABLE"
    assert agent.calls == 1
    assert legacy_recognition.recognize_calls == 0
    assert legacy_recognition.recognize_from_url_calls == 0
    assert legacy_quote.quote_calls == 0
    assert legacy_chat.reply_calls == 0
