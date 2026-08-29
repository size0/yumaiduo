from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagnostics import DiagnosticsStore
from app.errors import ProviderError
from app.main import create_app
from app.service import MovieImageRecognitionService


JPEG = b"\xff\xd8\xff\xe0diagnostic-fixture"


class StubService:
    async def recognize(self, _image: bytes, _content_type: str, _buyer_message: str = "", *, prior_recognitions=None):
        raise AssertionError("not used")


def test_diagnostics_endpoint_returns_formatted_request_events() -> None:
    diagnostics = DiagnosticsStore(max_entries=20)
    client = TestClient(create_app(service=StubService(), diagnostics_store=diagnostics))

    health = client.get("/health")
    response = client.get("/api/diagnostics/recent?limit=20")

    assert health.status_code == 200
    assert response.status_code == 200
    entries = response.json()["entries"]
    request_entry = next(item for item in entries if item["event"] == "request_completed" and item["details"]["path"] == "/health")
    assert request_entry["request_id"] == health.headers["x-request-id"]
    assert request_entry["details"]["status"] == 200
    assert isinstance(request_entry["details"]["duration_ms"], float)


@pytest.mark.asyncio
async def test_schema_failure_keeps_complete_provider_response_and_validation_details_in_memory() -> None:
    diagnostics = DiagnosticsStore(max_entries=20)
    provider_body = {
        "id": "chatcmpl-debug-1",
        "model": "qwen3.5-flash-2026-02-23",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"movie_name":"奥德赛","confidence":7}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=provider_body, headers={"x-request-id": "aliyun-debug-id"})

    settings = Settings(api_key="test-key", chat_api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = MovieImageRecognitionService(settings, client=http_client, diagnostics=diagnostics)
        with pytest.raises(ProviderError) as caught:
            await service.recognize(JPEG, "image/jpeg")

    assert caught.value.code == "provider_schema_invalid"
    entries = diagnostics.recent(limit=20)
    provider = next(item for item in entries if item["event"] == "vision_provider_response")
    assert provider["details"]["response"] == provider_body
    assert provider["details"]["provider_request_id"] == "aliyun-debug-id"
    schema = next(item for item in entries if item["event"] == "vision_schema_invalid")
    assert schema["details"]["model_content"] == '{"movie_name":"奥德赛","confidence":7}'
    assert schema["details"]["parsed_json"]["confidence"] == 7
    assert schema["details"]["validation_errors"][0]["loc"] == ["confidence"]


def test_chat_ui_has_bottom_formatted_diagnostics_viewer() -> None:
    client = TestClient(create_app(service=StubService(), diagnostics_store=DiagnosticsStore()))
    html = client.get("/").text

    assert 'id="diagnosticsPanel"' in html
    assert 'id="diagnosticsEntries"' in html
    assert "/api/diagnostics/recent" in html
    assert "JSON.stringify(entry, null, 2)" in html
    assert "模型及万达官方只读接口返回" in html
    assert 'id="aiReplyEnabled"' in html
    assert "/api/settings/conversation-policy" in html
