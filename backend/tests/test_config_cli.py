from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import cli
from app.config import Settings
from app.errors import ConfigurationError, ProviderError
from app.models import MovieImageInfo
from app.service import MovieImageRecognitionService


JPEG = b"\xff\xd8\xff\xe0fixture"


def test_settings_loads_dashscope_environment_without_persisting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://model.example/v1/")
    monkeypatch.setenv("DASHSCOPE_MODEL", " qwen3-vl-flash ")
    monkeypatch.setenv("DASHSCOPE_CHAT_API_KEY", "chat-environment-key")
    monkeypatch.setenv("DASHSCOPE_CHAT_BASE_URL", "https://chat.example/v1/")
    monkeypatch.setenv("DASHSCOPE_CHAT_MODEL", " gpt-5.5 ")
    monkeypatch.setenv("MAX_IMAGE_BYTES", "2048")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("WANDA_FULFILLMENT_CALLBACK_ENABLED", "true")
    monkeypatch.setenv("WANDA_FULFILLMENT_CALLBACK_SECRET", "fulfillment-secret")

    value = Settings.from_env()

    assert value.api_key == "environment-key"
    assert value.base_url == "https://model.example/v1"
    assert value.model == "qwen3-vl-flash"
    assert value.chat_api_key == "chat-environment-key"
    assert value.chat_base_url == "https://chat.example/v1"
    assert value.chat_model == "gpt-5.5"
    assert value.max_image_bytes == 2048
    assert value.request_timeout_seconds == 15
    assert value.wanda_fulfillment_callback_enabled is True
    assert value.wanda_fulfillment_callback_secret == "fulfillment-secret"


def test_settings_rejects_insecure_url_and_empty_model() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(base_url="http://model.example", model="vision")
    with pytest.raises(ValidationError, match="cannot be empty"):
        Settings(base_url="https://model.example", model=" ")
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(chat_base_url="http://chat.example")


@pytest.mark.asyncio
async def test_service_requires_environment_key_only_after_validating_image() -> None:
    service = MovieImageRecognitionService(Settings(api_key=""))
    with pytest.raises(ConfigurationError):
        await service.recognize(JPEG, "image/jpeg")


def movie_result() -> MovieImageInfo:
    return MovieImageInfo(movie_name="奥德赛", selected_count_visible=0, confidence=0.9)


@pytest.mark.asyncio
async def test_cli_prints_json_for_a_local_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    image_path = tmp_path / "ticket.jpg"
    image_path.write_bytes(JPEG)

    class StubService:
        def __init__(self, _settings: Settings) -> None: pass
        async def recognize(self, content: bytes, content_type: str) -> MovieImageInfo:
            assert content == JPEG
            assert content_type == "image/jpeg"
            return movie_result()

    monkeypatch.setattr(cli, "MovieImageRecognitionService", StubService)
    code = await cli.recognize_file(image_path)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["data"]["movie_name"] == "奥德赛"


@pytest.mark.asyncio
async def test_cli_returns_safe_json_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    image_path = tmp_path / "ticket.jpg"
    image_path.write_bytes(JPEG)

    class FailingService:
        def __init__(self, _settings: Settings) -> None: pass
        async def recognize(self, _content: bytes, _content_type: str) -> MovieImageInfo:
            raise ProviderError("provider_timeout", "识别超时")

    monkeypatch.setattr(cli, "MovieImageRecognitionService", FailingService)
    code = await cli.recognize_file(image_path)
    output = json.loads(capsys.readouterr().out)

    assert code == 1
    assert output == {"ok": False, "error": "识别超时"}
