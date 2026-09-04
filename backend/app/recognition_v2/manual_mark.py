from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx

from ..config import Settings


ManualMarkDetectorCallable = Callable[[str], bool | None | Awaitable[bool | None]]
ImageHashProvider = Callable[[str], bytes | str | None | Awaitable[bytes | str | None]]


class ManualMarkResultStore(Protocol):
    def get_manual_mark_result(
        self, image_sha256: str, *, detector_version: str | None = None,
    ) -> Mapping[str, Any] | None: ...

    def save_manual_mark_result(
        self, image_sha256: str, manual_mark_result: bool, *,
        detector_version: str, detected_at: str | None = None,
    ) -> Mapping[str, Any]: ...


_MANUAL_MARK_PROMPT = """只判断当前图片是否存在买家后期人为添加的手绘标记。
手绘标记包括手绘圆圈、椭圆、箭头、涂画、手绘划线，或明显后期圈住座位区域的标记。
不要把 App 原生座位颜色、已选座位系统高亮、W+区域底色、座位边框、原生图标、按钮、系统文字或页面装饰判为手绘标记。
不要识别或输出影院、城市、影片、场次、影厅、座位、价格、标记位置或 Provider route。
只能返回 JSON 对象，且只能包含一个布尔字段：{"has_manual_mark": true} 或 {"has_manual_mark": false}。
无法判断时返回 {"has_manual_mark": null}。"""


class ManualMarkDetector:
    """One-shot manual-mark detector with an injectable test boundary."""

    DETECTOR_VERSION = "manual-mark-v1"
    MAX_IMAGE_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        detector: ManualMarkDetectorCallable | None = None,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        result_store: ManualMarkResultStore | None = None,
        image_hash_provider: ImageHashProvider | None = None,
        detector_version: str = DETECTOR_VERSION,
    ) -> None:
        self._detector = detector
        self._settings = settings
        self._client = http_client
        self._result_store = result_store
        self._image_hash_provider = image_hash_provider
        self._detector_version = str(detector_version).strip() or self.DETECTOR_VERSION
        self._image_client: httpx.AsyncClient | None = None
        self._owns_image_client = False
        self._owns_client = False
        timeout_seconds = self._settings.request_timeout_seconds if self._settings is not None else 10.0
        timeout = httpx.Timeout(
            timeout_seconds, connect=min(5.0, timeout_seconds),
        )
        if self._settings is not None and self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
            self._owns_client = True
        if self._result_store is not None and self._image_hash_provider is None:
            self._image_client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
            self._owns_image_client = True

    async def detect(self, image_url: str) -> bool | None:
        image_hash = await self._image_sha256(image_url)
        if image_hash is not None and self._result_store is not None:
            try:
                cached = self._result_store.get_manual_mark_result(
                    image_hash, detector_version=self._detector_version,
                )
            except Exception:
                return None
            cached_value = cached.get("manual_mark_result") if cached else None
            if isinstance(cached_value, bool):
                return cached_value
        if self._detector is not None:
            value = await self._call_injected_detector(image_url)
        elif self._settings is None or not self._settings.api_key.strip() or self._client is None:
            value = None
        else:
            value = await self._call_vision_provider(image_url)
        # Only determinate provider answers are durable.  In particular, a
        # transient null cannot overwrite an existing true/false result.
        if image_hash is not None and self._result_store is not None and isinstance(value, bool):
            try:
                self._result_store.save_manual_mark_result(
                    image_hash, value, detector_version=self._detector_version,
                )
            except Exception:
                # A determinate answer without durable evidence must not enter
                # the exact-seat quote path.
                return None
        return value

    async def _image_sha256(self, image_url: str) -> str | None:
        if self._image_hash_provider is not None:
            try:
                value = self._image_hash_provider(image_url)
                if inspect.isawaitable(value):
                    value = await value
                return _normalize_image_hash(value)
            except Exception:
                return None
        if self._image_client is None:
            return None
        try:
            response = await self._image_client.get(image_url, timeout=self._image_client.timeout)
            if response.status_code >= 400 or len(response.content) > self.MAX_IMAGE_BYTES:
                return None
            return hashlib.sha256(response.content).hexdigest()
        except (httpx.HTTPError, ValueError, TypeError):
            return None

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
        if self._owns_image_client and self._image_client is not None:
            await self._image_client.aclose()
            self._image_client = None


def _normalize_image_hash(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) == 64:
        try:
            int(normalized, 16)
        except ValueError:
            return None
        return normalized
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if normalized else None
