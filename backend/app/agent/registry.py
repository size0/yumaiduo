from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .observations import Observation

ToolHandler = (
    Callable[[dict[str, Any]], Observation | Awaitable[Observation]]
    | Callable[[dict[str, Any], Mapping[str, str]], Observation | Awaitable[Observation]]
    | Callable[[dict[str, Any], Mapping[str, str], Mapping[str, Any]], Observation | Awaitable[Observation]]
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    scope: str = "conversation"
    risk_level: str = "read"
    audit: bool = True
    observation_schema: Mapping[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


class ToolRegistry:
    def __init__(self, *, read_only: bool = True) -> None:
        self._read_only = read_only
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.description:
            raise ValueError("agent_tool_definition_invalid")
        if definition.risk_level not in {"read", "write"}:
            raise ValueError("agent_tool_risk_invalid")
        if definition.scope not in {"conversation", "tenant", "request"}:
            raise ValueError("agent_tool_scope_invalid")
        if self._read_only and definition.risk_level != "read":
            raise ValueError("agent_write_tool_disabled")
        if definition.name in self._definitions:
            raise ValueError("agent_tool_duplicate")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.schema() for definition in self._definitions.values()]

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        identity: Mapping[str, str] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Observation:
        definition = self.get(name)
        if definition is None:
            return Observation.warning("tool_not_found", message=f"Unknown tool: {name}")
        if definition.scope == "request":
            required_identity = ("tenant_id", "shop_id", "buyer_id", "chat_id")
            if identity is None or any(not str(identity.get(key) or "").strip() for key in required_identity):
                return Observation.warning("tool_identity_required", message="工具缺少服务端会话身份，已停止执行。")
        try:
            handler = definition.handler
            try:
                parameter_count = len(inspect.signature(handler).parameters)
            except (TypeError, ValueError):
                parameter_count = 1
            if parameter_count >= 3:
                result = handler(dict(arguments), dict(identity or {}), dict(context or {}))
            elif parameter_count >= 2:
                result = handler(dict(arguments), dict(identity or {}))
            else:
                result = handler(dict(arguments))
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Observation):
                return Observation.warning("tool_observation_invalid", message="工具返回了无效观察结果")
            return result
        except Exception:
            return Observation.warning(
                "tool_execution_failed",
                message="工具暂时不可用，请稍后重试；如果再次失败则转人工确认。",
            )
