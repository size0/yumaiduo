from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from .context import QuotePipelineContext
from .models import GateResult, RecoveryAction, RecoveryDecision
from .policy import RecoveryPolicy

GateRunner = Callable[[QuotePipelineContext], GateResult | Awaitable[GateResult]]


class QuoteRecoveryOrchestrator:
    """Small deterministic runner for GateResult stages.

    Existing production orchestration can adopt this one stage at a time. The
    runner owns progression; stage services only return facts and status.
    """

    def __init__(self, stages: Sequence[GateRunner], *, policy: RecoveryPolicy | None = None) -> None:
        self._stages = tuple(stages)
        self._policy = policy or RecoveryPolicy()

    async def run(self, context: QuotePipelineContext) -> tuple[QuotePipelineContext, GateResult, RecoveryDecision]:
        last_result = GateResult(gate="PIPELINE", status="EMPTY", success=False, safety_class="RECOVERABLE")
        last_decision = self._policy.evaluate(last_result)
        for stage in self._stages:
            result = stage(context)
            if hasattr(result, "__await__"):
                result = await result
            if not isinstance(result, GateResult):
                raise TypeError(f"stage returned {type(result).__name__}, expected GateResult")
            context.current_gate = result.gate
            context.generation += 1
            last_result = result
            last_decision = self._policy.evaluate(result)
            if last_decision.action is not RecoveryAction.CONTINUE:
                return context, result, last_decision
        return context, last_result, last_decision
