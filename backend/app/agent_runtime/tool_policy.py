"""Mode and phase gates for model-visible tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import RuntimeBudget
from .tool_registry import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolDecision:
    allowed: bool
    reason: str = ""


class ToolPolicy:
    _MODE_BUDGETS = {
        "hybrid": RuntimeBudget(max_model_rounds=4, max_tool_calls=6, max_seconds=15),
        "agent": RuntimeBudget(),
        "full": RuntimeBudget(max_model_rounds=8, max_tool_calls=12, max_seconds=30),
        "simulation": RuntimeBudget(max_model_rounds=8, max_tool_calls=12, max_seconds=30),
        "rules": RuntimeBudget(max_model_rounds=1, max_tool_calls=0, max_seconds=5),
    }

    def budget_for(self, mode: str, deadline_seconds: float | None = None) -> RuntimeBudget:
        base = self._MODE_BUDGETS.get(str(mode).lower(), self._MODE_BUDGETS["hybrid"])
        if deadline_seconds is None:
            return base
        return RuntimeBudget(base.max_model_rounds, base.max_tool_calls, min(base.max_seconds, float(deadline_seconds)), base.max_retry_attempts)

    def check(self, definition: ToolDefinition, *, mode: str, phase: str, runtime_context: dict[str, Any] | None = None, writes_this_round: int = 0) -> ToolDecision:
        mode = str(mode).lower()
        if mode == "rules":
            return ToolDecision(False, "rules_mode_disables_agent_tools")
        if phase not in definition.phases:
            return ToolDecision(False, "tool_not_exposed_in_phase")
        if not definition.read_only and mode not in {"full", "simulation"}:
            return ToolDecision(False, "write_tool_requires_full_mode")
        if not definition.read_only and writes_this_round >= 1:
            return ToolDecision(False, "one_write_tool_per_round")
        context = runtime_context or {}
        missing = [key for key in definition.required_fields if not context.get(key)]
        if missing:
            return ToolDecision(False, "missing_preconditions:" + ",".join(missing))
        return ToolDecision(True)
