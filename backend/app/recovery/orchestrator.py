from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import json
import logging
from time import perf_counter
from .context import QuotePipelineContext
from .models import GateResult, RecoveryAction, RecoveryDecision
from .policy import RecoveryPolicy
LOGGER = logging.getLogger(__name__)

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
        def record(result: GateResult, duration_ms: float) -> None:
            trace = {"gate": result.gate, "status": result.status, "success": result.success,
                     "reason_code": result.reason_code, "missing_fields_count": len(result.missing_fields),
                     "retryable": result.retryable, "provider_verified": result.provider_verified,
                     "amount_safe": result.metadata.get("amount_safe") if isinstance(result.metadata, dict) else None,
                     "quote_record_created": result.status == "QUOTE_PERSISTED", "duration_ms": round(duration_ms, 2)}
            context.gate_trace.append(trace)
            if not result.success and context.first_failed_gate is None:
                context.first_failed_gate, context.first_failed_status, context.first_failed_reason_code = result.gate, result.status, result.reason_code
            try:
                LOGGER.info("canonical_quote_gate_trace %s", trace)
            except Exception:
                pass
        last_result = GateResult(gate="PIPELINE", status="EMPTY", success=False, safety_class="RECOVERABLE")
        last_decision = self._policy.evaluate(last_result)
        recovery_attempts = self._max_recovery_attempts
        step = 0
        seen_states: set[str] = set()
        while step < len(self._stages):
            if step >= self._max_steps:
                return context, last_result.model_copy(update={"status": "RECOVERY_LIMIT"}), RecoveryDecision(
                    gate="PIPELINE", status="RECOVERY_LIMIT", safety_class=last_result.safety_class,
                    action=RecoveryAction.STOP, reason="MAX_RECOVERY_STEPS", stop_scope="STOP_QUOTE_PIPELINE",
                )
            stage = self._stages[step]
            started = perf_counter()
            try:
                result = stage(context)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as error:
                stage_name = getattr(stage, "__name__", None) or f"stage_{step}"
                result = GateResult(gate="PIPELINE", status="STAGE_EXCEPTION", success=False,
                                    safety_class="HARD_SAFETY", reason_code=type(error).__name__,
                                    metadata={"stage": stage_name, "stage_index": step})
            if not isinstance(result, GateResult):
                raise TypeError(f"stage returned {type(result).__name__}, expected GateResult")
            context.current_gate = result.gate
            context.generation += 1
            record(result, (perf_counter() - started) * 1000)
            last_result = result
            fingerprint = json.dumps({"gate": result.gate, "status": result.status,
                                      "facts": result.facts, "missing": result.missing_fields,
                                      "candidates": result.candidates}, sort_keys=True, default=str)
            if fingerprint in seen_states:
                return context, result.model_copy(update={"status": "RECOVERY_LOOP_DETECTED"}), RecoveryDecision(
                    gate="PIPELINE", status="RECOVERY_LOOP_DETECTED", safety_class="RECOVERABLE",
                    action=RecoveryAction.STOP, reason="REPEATED_GATE_STATE", stop_scope="STOP_QUOTE_PIPELINE",
                )
            seen_states.add(fingerprint)
            last_decision = self._policy.evaluate(result)
            if last_decision.action is RecoveryAction.RETRY:
                handler = self._retry_handlers.get(result.gate)
                if handler is not None and recovery_attempts > 0:
                    recovery_attempts -= 1
                    try:
                        retry_result = handler(context, result)
                        if hasattr(retry_result, "__await__"):
                            retry_result = await retry_result
                    except Exception as error:
                        return context, GateResult(
                            gate=result.gate, status="RECOVERY_HANDLER_EXCEPTION", success=False,
                            safety_class="HARD_SAFETY", reason_code=type(error).__name__,
                            metadata={"handler": "retry", "gate": result.gate},
                        ), RecoveryDecision(
                            gate=result.gate, status="RECOVERY_HANDLER_EXCEPTION", safety_class="HARD_SAFETY",
                            action=RecoveryAction.STOP, reason="RECOVERY_HANDLER_EXCEPTION",
                            stop_scope="STOP_QUOTE_PIPELINE",
                        )
                    if isinstance(retry_result, GateResult):
                        record(retry_result, 0)
                        last_result = retry_result
                        last_decision = self._policy.evaluate(retry_result)
                        if last_decision.action is RecoveryAction.CONTINUE:
                            step += 1
                            continue
                return context, last_result, last_decision
            if last_decision.action is RecoveryAction.FALLBACK:
                handler = self._fallback_handlers.get(result.gate)
                if handler is not None:
                    try:
                        fallback_result = handler(context, result)
                        if hasattr(fallback_result, "__await__"):
                            fallback_result = await fallback_result
                    except Exception as error:
                        return context, GateResult(
                            gate=result.gate, status="RECOVERY_HANDLER_EXCEPTION", success=False,
                            safety_class="HARD_SAFETY", reason_code=type(error).__name__,
                            metadata={"handler": "fallback", "gate": result.gate},
                        ), RecoveryDecision(
                            gate=result.gate, status="RECOVERY_HANDLER_EXCEPTION", safety_class="HARD_SAFETY",
                            action=RecoveryAction.STOP, reason="RECOVERY_HANDLER_EXCEPTION",
                            stop_scope="STOP_QUOTE_PIPELINE",
                        )
                    if isinstance(fallback_result, GateResult):
                        record(fallback_result, 0)
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
