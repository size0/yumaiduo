from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import ProviderError
from app.main import create_app
from app.model_catalog import ModelCatalogService
from app.models import ModelCatalogResponse
from app.settings_store import PersistentSettingsStore


class ReversibleProtector:
    def protect(self, value: str) -> str:
        return "enc:" + value[::-1]

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")[::-1]


class StubRecognitionService:
    async def recognize(self, _image: bytes, _content_type: str, _buyer_message: str = "", *, prior_recognitions=None):
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_openai_compatible_model_catalog_uses_v1_models_and_bearer_key() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={
            "object": "list",
            "data": [
                {"id": "gpt-5.1", "object": "model", "owned_by": "openai"},
                {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
                {"id": "gpt-5.1", "object": "model", "owned_by": "duplicate"},
            ],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelCatalogService(client=client).list_models("https://airelvo.cc/", "relay-test-key")

    assert captured == {
        "url": "https://airelvo.cc/v1/models",
        "authorization": "Bearer relay-test-key",
    }
    assert result.models == ["gpt-4.1", "gpt-5.1"]
    assert result.count == 2


@pytest.mark.asyncio
async def test_model_catalog_maps_authentication_and_invalid_schema_errors() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"code": "invalid_key"}))) as client:
        with pytest.raises(ProviderError) as auth_error:
            await ModelCatalogService(client=client).list_models("https://airelvo.cc/v1", "bad-key")
    assert auth_error.value.code == "model_catalog_authentication_failed"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"unexpected": []}))) as client:
        with pytest.raises(ProviderError) as schema_error:
            await ModelCatalogService(client=client).list_models("https://airelvo.cc/v1", "key")
    assert schema_error.value.code == "model_catalog_response_invalid"


def test_models_api_uses_typed_key_then_falls_back_to_persisted_key(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class StubCatalog:
        async def list_models(self, base_url: str, api_key: str) -> ModelCatalogResponse:
            calls.append((base_url, api_key))
            return ModelCatalogResponse(models=["gpt-4.1", "gpt-5.1"], count=2)

    path = tmp_path / "settings.json"
    store = PersistentSettingsStore(path, protector=ReversibleProtector(), environment=Settings(
        api_key="persisted-key",
        chat_api_key="persisted-chat-key",
    ))
    client = TestClient(create_app(
        service=StubRecognitionService(),
        settings_store=store,
        model_catalog_service=StubCatalog(),
    ))

    typed = client.post("/api/settings/vision/models", json={"base_url": "https://airelvo.cc/v1", "api_key": "typed-key"})
    client.post("/api/settings/vision/models", json={"base_url": "https://airelvo.cc/v1", "api_key": None})
    chat_persisted = client.post("/api/settings/vision/models", json={
        "provider": "chat", "base_url": "https://chat-relay.example/v1", "api_key": None
    })

    assert typed.status_code == 200
    assert chat_persisted.status_code == 200
    assert typed.json()["models"] == ["gpt-4.1", "gpt-5.1"]
    assert calls == [
        ("https://airelvo.cc/v1", "typed-key"),
        ("https://airelvo.cc/v1", "persisted-key"),
        ("https://chat-relay.example/v1", "persisted-chat-key"),
    ]


def test_models_api_requires_a_typed_or_persisted_key(tmp_path: Path) -> None:
    store = PersistentSettingsStore(
        tmp_path / "settings.json",
        protector=ReversibleProtector(),
        environment=Settings(api_key=""),
    )
    client = TestClient(create_app(service=StubRecognitionService(), settings_store=store))
    response = client.post("/api/settings/vision/models", json={"base_url": "https://airelvo.cc/v1", "api_key": None})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_catalog_key_required"


def test_settings_ui_contains_relay_preset_and_get_models_control() -> None:
    client = TestClient(create_app(service=StubRecognitionService()))
    html = client.get("/").text
    assert 'id="useAirelvo"' in html
    assert 'id="fetchModels"' in html
    assert 'id="fetchChatModels"' in html
    assert "/api/settings/vision/models" in html
    assert "https://airelvo.cc/v1" in html
