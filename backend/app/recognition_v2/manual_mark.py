from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from ..config import Settings


ManualMarkDetectorCallable = Callable[[str], bool | None | Awaitable[bool | None]]

_MANUAL_MARK_PROMPT = """只判断当前图片是否存在买家后期人为添加的手绘标记。
手绘标记包括手绘圆圈、椭圆、箭头、涂画、手绘划线，或明显后期圈住座位区域的标记。
不要把 App 原生座位颜色、已选座位系统高亮、W+区域底色、座位边框、原生图标、按钮、系统文字或页面装饰判为手绘标记。
不要识别或输出影院、城市、影片、场次、影厅、座位、价格、标记位置或 Provider route。
只能返回 JSON 对象，且只能包含一个布尔字段：{"has_manual_mark": true} 或 {"has_manual_mark": false}。
无法判断时返回 {"has_manual_mark": null}。"""


class ManualMarkDetector:
    """One-shot manual-mark detector with an injectable test boundary."""

    def __init__(
        self,
        detector: ManualMarkDetectorCallable | None = None,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._detector = detector
        self._settings = settings
        self._client = http_client
        self._owns_client = False
        if self._settings is not None and self._client is None:
            timeout = httpx.Timeout(
                self._settings.request_timeout_seconds,
                connect=min(5.0, self._settings.request_timeout_seconds),
            )
            self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
            self._owns_client = True

    async def detect(self, image_url: str) -> bool | None:
        if self._detector is not None:
            return await self._call_injected_detector(image_url)
        if self._settings is None or not self._settings.api_key.strip() or self._client is None:
            return None
        return await self._call_vision_provider(image_url)

    async def _call_injected_detector(self, image_url: str) -> bool | None:
        try:
            value: Any = self._detector(image_url)
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    async def _call_vision_provider(self, image_url: str) -> bool | None:
        assert self._settings is not None
        assert self._client is not None
        url = self._completion_url(self._settings.base_url)
        payload = {
            "model": self._settings.model,
            "max_tokens": 32,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _MANUAL_MARK_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": "只返回 has_manual_mark 布尔字段。"},
                    ],
                },
            ],
        }
        try:
            response = await self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._settings.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._settings.request_timeout_seconds,
            )
            if response.status_code >= 400:
                return None
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) for item in content if isinstance(item, Mapping)
                )
            if not isinstance(content, str):
                return None
            parsed = json.loads(content)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        value = parsed.get("has_manual_mark")
        return value if isinstance(value, bool) else None

    @staticmethod
    def _completion_url(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        return normalized if normalized.endswith("/chat/completions") else normalized + "/chat/completions"

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
