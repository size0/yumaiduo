from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from ..config import Settings
from ..errors import ProviderError
from ..service import MovieImageRecognitionService
from .prompt import BASE_SYSTEM_PROMPT


class OpenAICompatibleModel:
    """Small provider adapter for the new Harness; returns only model intent."""

    def __init__(self, settings: Settings | Callable[[], Settings], *, client: httpx.AsyncClient | None = None) -> None:
        self._settings_provider = settings if callable(settings) else lambda: settings
        self._client = client

    async def complete(self, context: dict[str, Any], tools: list[dict[str, Any]], *, trace_id: str) -> Mapping[str, Any]:
        settings = self._settings_provider()
        messages = [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
        ]
        payload: dict[str, Any] = {
            "model": settings.chat_model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": min(int(settings.chat_max_completion_tokens), 3000),
        }
        if tools:
            payload["tools"] = [self._provider_schema(item) for item in tools]
            payload["tool_choice"] = "auto"
        url = MovieImageRecognitionService._completion_url(settings.chat_base_url)
        try:
            if self._client is not None:
                response = await self._client.post(url, headers={"Authorization": f"Bearer {settings.chat_api_key}"}, json=payload, timeout=settings.request_timeout_seconds)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, headers={"Authorization": f"Bearer {settings.chat_api_key}"}, json=payload, timeout=settings.request_timeout_seconds)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError("agent_model_unavailable", "AI 客服暂时无法连接。") from error
        if response.status_code >= 400 or not isinstance(body, Mapping):
            raise ProviderError("agent_model_request_failed", "AI 客服暂时无法完成判断。")
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("agent_model_response_invalid", "AI 客服返回了无效结果。") from error
        if not isinstance(message, Mapping):
            raise ProviderError("agent_model_response_invalid", "AI 客服返回了无效结果。")
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            call = calls[0]
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(function, Mapping):
                arguments: Any = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = {}
                return {"tool_call": {"name": self._canonical_name(str(function.get("name") or "")), "arguments": arguments}}
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content if isinstance(item, Mapping))
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except ValueError:
                return {"final": content.strip()}
            if isinstance(parsed, Mapping):
                final = parsed.get("final") or parsed.get("reply") or parsed.get("message")
                if isinstance(final, str):
                    return {"final": final}
        return {"final": str(content).strip()}

    @staticmethod
    def _provider_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
        function = schema.get("function") if isinstance(schema.get("function"), Mapping) else {}
        canonical = str(function.get("name") or "")
        return {**dict(schema), "function": {**dict(function), "name": canonical.replace(".", "_")[:64]}}

    @staticmethod
    def _canonical_name(name: str) -> str:
        return name.replace("_", ".") if name in {"cinema_list", "movie_resolve", "show_list", "show_detail", "seat_list", "quote_preview", "order_current", "conversation_current"} else name
