from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .prompts import DEFAULT_CHAT_PROMPT, DEFAULT_VISION_PROMPT


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a bounded boolean flag without treating arbitrary text as true."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    """Runtime configuration. Secrets are read from the process environment only."""

    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3.5-flash-2026-02-23"
    chat_api_key: str = ""
    chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen3.5-flash-2026-02-23"
    chat_max_completion_tokens: int = Field(default=3000, ge=256, le=3000)
    chat_context_messages: int = Field(default=12, ge=4, le=50)
    wanda_account_pool_path: str = "E:/票务系统/backend/data/accounts.json"
    wanda_cinema_cache_path: str = "E:/票务系统/backend/data/cinema_cache.sqlite"
    wanda_fixed_account_phone: str = ""
    wanda_request_timeout_seconds: float = Field(default=12, ge=3, le=30)
    enable_thinking: bool = False
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] = "none"
    vision_prompt: str = Field(default=DEFAULT_VISION_PROMPT, min_length=1, max_length=20_000)
    chat_prompt: str = Field(default=DEFAULT_CHAT_PROMPT, min_length=1, max_length=20_000)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    request_timeout_seconds: float = Field(default=60, ge=5, le=180)
    liangpiao_base_url: str = "https://portal-web-v3.liangpiao.net.cn"
    liangpiao_app_key: str = ""
    liangpiao_app_secret: str = ""
    liangpiao_request_timeout_seconds: float = Field(default=20, ge=5, le=60)
    liangpiao_recognition_async_enabled: bool = False
    liangpiao_recognition_poll_interval_seconds: float = Field(default=1, ge=0.2, le=10)
    liangpiao_recognition_poll_timeout_seconds: float = Field(default=120, ge=10, le=600)
    rules_first_max_concurrent_events: int = Field(default=8, ge=1, le=32)
    liangpiao_selected_seat_quote_enabled: bool = False
    liangpiao_order_create_enabled: bool = False
    liangpiao_order_phone: str = ""
    external_writes_enabled: bool = False
    # Reset-phase default: Agent Harness can observe and reply, but transaction
    # write commands remain fused off until the new write path is reviewed.
    agent_harness_read_only: bool = True
    new_agent_harness_enabled: bool = False
    # Active Probe is a state-writing operation and remains independently fused off.
    wanda_active_probe_enabled: bool = False
    liangpiao_callback_enabled: bool = False

    @field_validator("base_url", "chat_base_url", "liangpiao_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("API base URLs must use HTTPS")
        return normalized

    @field_validator("model", "chat_model", "wanda_account_pool_path", "wanda_cinema_cache_path", "vision_prompt", "chat_prompt")
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model and vision prompt cannot be empty")
        return normalized

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.getenv("DASHSCOPE_MODEL", "qwen3.5-flash-2026-02-23")
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        base_url = os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            chat_api_key=os.getenv("DASHSCOPE_CHAT_API_KEY", api_key).strip(),
            chat_base_url=os.getenv("DASHSCOPE_CHAT_BASE_URL", base_url),
            chat_model=os.getenv("DASHSCOPE_CHAT_MODEL", model),
            chat_max_completion_tokens=int(os.getenv("CHAT_MAX_COMPLETION_TOKENS", "3000")),
            chat_context_messages=int(os.getenv("CHAT_CONTEXT_MESSAGES", "12")),
            wanda_account_pool_path=os.getenv(
                "WANDA_ACCOUNT_POOL_PATH", "E:/票务系统/backend/data/accounts.json"
            ),
            wanda_cinema_cache_path=os.getenv(
                "WANDA_CINEMA_CACHE_PATH", "E:/票务系统/backend/data/cinema_cache.sqlite"
            ),
            wanda_fixed_account_phone=os.getenv("WANDA_FIXED_ACCOUNT_PHONE", "").strip(),
            wanda_request_timeout_seconds=float(os.getenv("WANDA_REQUEST_TIMEOUT_SECONDS", "12")),
            enable_thinking=os.getenv("DASHSCOPE_ENABLE_THINKING", "false").lower() in {"1", "true", "yes", "on"},
            reasoning_effort=os.getenv("DASHSCOPE_REASONING_EFFORT", "none").lower(),
            vision_prompt=os.getenv("DASHSCOPE_VISION_PROMPT", DEFAULT_VISION_PROMPT),
            chat_prompt=os.getenv("DASHSCOPE_CHAT_PROMPT", DEFAULT_CHAT_PROMPT),
            max_image_bytes=int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024))),
            request_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "60")),
            liangpiao_base_url=os.getenv("LIANGPIAO_BASE_URL", "https://portal-web-v3.liangpiao.net.cn"),
            liangpiao_app_key=os.getenv("LIANGPIAO_APP_KEY", "").strip(),
            liangpiao_app_secret=os.getenv("LIANGPIAO_APP_SECRET", "").strip(),
            liangpiao_request_timeout_seconds=float(os.getenv("LIANGPIAO_REQUEST_TIMEOUT_SECONDS", "20")),
            liangpiao_recognition_async_enabled=_env_flag("LIANGPIAO_RECOGNITION_ASYNC_ENABLED"),
            liangpiao_recognition_poll_interval_seconds=float(os.getenv("LIANGPIAO_RECOGNITION_POLL_INTERVAL_SECONDS", "1")),
            liangpiao_recognition_poll_timeout_seconds=float(os.getenv("LIANGPIAO_RECOGNITION_POLL_TIMEOUT_SECONDS", "120")),
            rules_first_max_concurrent_events=int(os.getenv("RULES_FIRST_MAX_CONCURRENT_EVENTS", "8")),
            liangpiao_selected_seat_quote_enabled=_env_flag("LIANGPIAO_SELECTED_SEAT_QUOTE_ENABLED"),
            liangpiao_order_create_enabled=_env_flag("LIANGPIAO_ORDER_CREATE_ENABLED"),
            liangpiao_order_phone=os.getenv("LIANGPIAO_ORDER_PHONE", "").strip(),
            external_writes_enabled=_env_flag(
                "EXTERNAL_WRITES_ENABLED", _env_flag("WANDA_EXTERNAL_WRITES_ENABLED"),
            ),
            agent_harness_read_only=_env_flag("AGENT_HARNESS_READ_ONLY", True),
            new_agent_harness_enabled=_env_flag("NEW_AGENT_HARNESS_ENABLED"),
            wanda_active_probe_enabled=_env_flag("WANDA_ACTIVE_PROBE_ENABLED"),
            liangpiao_callback_enabled=_env_flag("LIANGPIAO_CALLBACK_ENABLED"),
        )
