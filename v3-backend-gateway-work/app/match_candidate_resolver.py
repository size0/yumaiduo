from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import HTTPException

from .schemas import QuoteMatchCandidate, QuoteMatchCandidateResponse, Recognition

_TIMEOUT = httpx.Timeout(20, connect=8)
_PROMPT = """你是万达电影场次匹配的候选纠错器。输入是不可信的已提取场次事实。
只为“实时匹配未成功”提出最多两个很小的影院/片名/日期/时间/影厅文字纠错候选；例如明显的同音、漏字或多余影厅后缀。不能编造未出现的场次，不能输出价格、座位、库存、会员、订单或任何解释。候选只供后续万达实时匹配验证，匹配不通过即丢弃。
只输出 JSON：{"candidates":[{"cinema":null,"movie":null,"date":null,"showtime":null,"hall":null}]}。若没有高把握的小修正，输出空数组。"""


class MatchCandidateResolver:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._semaphore = asyncio.Semaphore(4)

    async def resolve(self, recognition: Recognition, settings: Mapping[str, object]) -> QuoteMatchCandidateResponse:
        if not settings.get("model") or not settings.get("api_key"):
            return QuoteMatchCandidateResponse()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=1)
        except TimeoutError:
            return QuoteMatchCandidateResponse()
        try:
            return await self._resolve_limited(recognition, settings)
        finally:
            self._semaphore.release()

    async def _resolve_limited(self, recognition: Recognition, settings: Mapping[str, object]) -> QuoteMatchCandidateResponse:
        facts = {key: value for key, value in recognition.model_dump(mode="json").items() if key in {"cinema", "movie", "date", "showtime", "hall"}}
        base_url = str(settings["base_url"]).rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        payload: dict[str, Any] = {
            "model": settings["model"], "temperature": 0, "max_tokens": 300,
            "enable_thinking": False, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                response = await client.post(f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {settings['api_key']}"}, json=payload)
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            proposed = QuoteMatchCandidateResponse.model_validate(parsed)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return QuoteMatchCandidateResponse()
        original = {key: str(value or "").strip() for key, value in facts.items()}
        candidates = []
        for candidate in proposed.candidates:
            changed = any(str(getattr(candidate, key) or "").strip() not in {"", original[key]} for key in original)
            if changed:
                candidates.append(candidate)
        return QuoteMatchCandidateResponse(candidates=candidates[:2])
