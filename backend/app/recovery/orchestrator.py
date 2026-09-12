from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from .context import QuotePipelineContext
from .models import GateResult, RecoveryAction, RecoveryDecision
from .policy import RecoveryPolicy

GateRunner = Callable[[QuotePipelineContext], GateResult | Awaitable[GateResult]]
RecoveryHandler = Callable[[QuotePipelineContext, GateResult], GateResult | Awaitable[GateResult]]


class QuoteRecoveryOrchestrator:
    """Small deterministic runner for GateResult stages.

    Existing production orchestration can adopt this one stage at a time. The
    runner owns progression; stage services only return facts and status.
    """

    def __init__(self, stages: Sequence[GateRunner], *, policy: RecoveryPolicy | None = None,
                 max_steps: int = 16, max_recovery_attempts: int = 1,
                 retry_handlers: Mapping[str, RecoveryHandler] | None = None,
                 fallback_handlers: Mapping[str, RecoveryHandler] | None = None) -> None:
        self._stages = tuple(stages)
        self._policy = policy or RecoveryPolicy()
        self._max_steps = max(1, max_steps)
        self._max_recovery_attempts = max(0, max_recovery_attempts)
        self._retry_handlers = dict(retry_handlers or {})
        self._fallback_handlers = dict(fallback_handlers or {})

    async def run(self, context: QuotePipelineContext) -> tuple[QuotePipelineContext, GateResult, RecoveryDecision]:
        last_result = GateResult(gate="PIPELINE", status="EMPTY", success=False, safety_class="RECOVERABLE")
        last_decision = self._policy.evaluate(last_result)
        step = 0
        while step < len(self._stages):
            if step >= self._max_steps:
                return context, last_result.model_copy(update={"status": "RECOVERY_LIMIT"}), RecoveryDecision(
                    gate="PIPELINE", status="RECOVERY_LIMIT", safety_class=last_result.safety_class,
                    action=RecoveryAction.STOP, reason="MAX_RECOVERY_STEPS", stop_scope="STOP_QUOTE_PIPELINE",
                )
            stage = self._stages[step]
            result = stage(context)
            if hasattr(result, "__await__"):
                result = await result
            if not isinstance(result, GateResult):
                raise TypeError(f"stage returned {type(result).__name__}, expected GateResult")
            context.current_gate = result.gate
            context.generation += 1
            last_result = result
            last_decision = self._policy.evaluate(result)
            if last_decision.action is RecoveryAction.RETRY:
                handler = self._retry_handlers.get(result.gate)
                if handler is not None and self._max_recovery_attempts > 0:
                    self._max_recovery_attempts -= 1
                    retry_result = handler(context, result)
                    if hasattr(retry_result, "__await__"):
                        retry_result = await retry_result
                    if isinstance(retry_result, GateResult):
                        last_result = retry_result
                        last_decision = self._policy.evaluate(retry_result)
                        if last_decision.action is RecoveryAction.CONTINUE:
                            step += 1
                            continue
                return context, last_result, last_decision
            if last_decision.action is RecoveryAction.FALLBACK:
                handler = self._fallback_handlers.get(result.gate)
                if handler is not None:
                    fallback_result = handler(context, result)
                    if hasattr(fallback_result, "__await__"):
                        fallback_result = await fallback_result
                    if isinstance(fallback_result, GateResult):
                        last_result = fallback_result
                        last_decision = self._policy.evaluate(fallback_result)
                        if last_decision.action is RecoveryAction.CONTINUE:
                            step += 1
                            continue
                return context, last_result, last_decision
            if last_decision.action is not RecoveryAction.CONTINUE:
                return context, result, last_decision
            step += 1
        return context, last_result, last_decision
