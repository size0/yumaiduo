"""Small provider-neutral model gateway interface."""
from __future__ import annotations

import inspect
import asyncio
from typing import Any, Awaitable, Callable, Mapping


ModelCallable = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class ModelGateway:
    def __init__(self, call: ModelCallable | None = None) -> None:
        self._call = call

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *, timeout_seconds: float = 20.0) -> Mapping[str, Any]:
        if self._call is None:
            return {"type": "final", "text": ""}
        result = self._call(messages, list(tools or []))
        if inspect.isawaitable(result):
            # wait_for propagates CancelledError so an interrupted buyer run
            # cannot emit a late reply.
            result = await asyncio.wait_for(result, timeout=max(0.1, timeout_seconds))
        return dict(result) if isinstance(result, Mapping) else {"type": "final", "text": str(result)}
