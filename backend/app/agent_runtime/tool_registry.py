"""Canonical, phase-aware tool registry."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .contracts import ToolResult

ToolHandler = Callable[[Mapping[str, Any]], ToolResult | Mapping[str, Any] | Awaitable[ToolResult | Mapping[str, Any]]]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str = ""
    handler: ToolHandler | None = None
    read_only: bool = True
    phases: frozenset[str] = field(default_factory=lambda: frozenset({"consultation", "recognition", "order", "fulfillment"}))
    required_fields: tuple[str, ...] = ()
    next_state: str | None = None
    retry_allowed: bool = False
    schema: Mapping[str, Any] = field(default_factory=dict)

    async def execute(self, arguments: Mapping[str, Any]) -> ToolResult:
        if self.handler is None:
            return ToolResult.error("tool handler unavailable")
        result = self.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, Mapping):
            status = str(result.get("status") or "success")
            return ToolResult(status, str(result.get("summary") or ""), result.get("data") if isinstance(result.get("data"), Mapping) else {}, result.get("next_actions") if isinstance(result.get("next_actions"), list) else [], result.get("retry") if isinstance(result.get("retry"), Mapping) else {"allowed": self.retry_allowed, "max_attempts": int(self.retry_allowed)}, result.get("stop_condition"))
        return ToolResult.error("invalid tool result")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        key = str(definition.name).strip()
        if not key or key in self._tools:
            raise ValueError(f"duplicate or empty tool name: {key}")
        self._tools[key] = definition
        return definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def exposed(self, phase: str, *, include_writes: bool = True) -> list[ToolDefinition]:
        return [item for item in self._tools.values() if phase in item.phases and (include_writes or item.read_only)]

    def schemas(self, phase: str, *, include_writes: bool = True) -> list[dict[str, Any]]:
        return [{"name": item.name, "description": item.description, "parameters": dict(item.schema)} for item in self.exposed(phase, include_writes=include_writes)]
