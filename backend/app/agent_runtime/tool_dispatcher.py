"""Request-scoped tool execution with budgets, deduplication and safe results."""
from __future__ import annotations

import asyncio
from typing import Any, Mapping

from .contracts import ToolResult
from .tool_policy import ToolPolicy
from .tool_registry import ToolRegistry


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, policy: ToolPolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy or ToolPolicy()
        self.calls = 0
        self.writes_this_round = 0
        self._seen: set[tuple[str, str]] = set()

    def reset(self) -> None:
        """Clear request-scoped budget and deduplication state."""
        self.calls = 0
        self.writes_this_round = 0
        self._seen.clear()

    def start_round(self) -> None:
        self.writes_this_round = 0

    def visible_schemas(self, *, phase: str, mode: str) -> list[dict[str, Any]]:
        """Return canonical model-visible tools for the current mode/phase."""
        if str(mode).lower() == "rules":
            return []
        return self.registry.schemas(
            phase,
            include_writes=str(mode).lower() in {"full", "simulation"},
        )

    async def dispatch(self, name: str, arguments: Mapping[str, Any], *, mode: str = "hybrid", phase: str = "consultation", runtime_context: dict[str, Any] | None = None, timeout_seconds: float = 10.0, retry_attempt: bool = False) -> ToolResult:
        definition = self.registry.get(name)
        if definition is None:
            return ToolResult.error("unknown tool")
        if retry_attempt and not definition.read_only:
            return ToolResult.error("write tools cannot be retried", stop_condition="write_retry_forbidden")
        decision = self.policy.check(definition, mode=mode, phase=phase, runtime_context=runtime_context, writes_this_round=self.writes_this_round)
        if not decision.allowed:
            return ToolResult.error("tool blocked: " + decision.reason, stop_condition=decision.reason)
        if self.calls >= self.policy.budget_for(mode).max_tool_calls:
            return ToolResult.error("tool budget exhausted", stop_condition="max_tool_calls")
        key = (name, repr(sorted((str(k), repr(v)) for k, v in arguments.items())))
        if key in self._seen and not retry_attempt:
            return ToolResult.warning("duplicate tool call suppressed", stop_condition="duplicate_call")
        self._seen.add(key)
        self.calls += 1
        if not definition.read_only:
            self.writes_this_round += 1
        try:
            return await asyncio.wait_for(definition.execute(arguments), timeout=max(0.1, timeout_seconds))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return ToolResult.error("tool timed out", retry={"allowed": definition.retry_allowed, "max_attempts": int(definition.retry_allowed)})
        except Exception:
            return ToolResult.error("tool execution failed", retry={"allowed": definition.retry_allowed, "max_attempts": int(definition.retry_allowed)})
