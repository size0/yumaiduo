from __future__ import annotations

from .models import GateResult, RecoveryAction, RecoveryDecision, SafetyClass


class RecoveryPolicy:
    """Deterministic policy; it never creates provider facts or prices."""

    def evaluate(self, result: GateResult) -> RecoveryDecision:
        if result.success:
            return RecoveryDecision(
                gate=result.gate, status=result.status, safety_class=result.safety_class,
                action=RecoveryAction.CONTINUE,
            )
        if result.safety_class is SafetyClass.HARD_SAFETY:
            return RecoveryDecision(
                gate=result.gate, status=result.status, safety_class=result.safety_class,
                action=RecoveryAction.STOP, missing_fields=result.missing_fields,
                reason=result.reason_code or result.status, stop_scope="STOP_QUOTE_PIPELINE",
            )
        if result.retryable:
            return RecoveryDecision(
                gate=result.gate, status=result.status, safety_class=result.safety_class,
                action=RecoveryAction.RETRY, missing_fields=result.missing_fields,
                recovery_methods=["retry"], reason=result.reason_code or result.status,
            )
        if result.candidates:
            return RecoveryDecision(
                gate=result.gate, status=result.status, safety_class=result.safety_class,
                action=RecoveryAction.ASK_CLARIFICATION,
                missing_fields=result.missing_fields, recovery_methods=["candidate_selection"],
                reason=result.reason_code or result.status,
            )
        if result.missing_fields:
            return RecoveryDecision(
                gate=result.gate, status=result.status, safety_class=result.safety_class,
                action=RecoveryAction.ASK_CLARIFICATION,
                missing_fields=result.missing_fields, recovery_methods=["conversation_facts", "normalize", "resolver"],
                reason=result.reason_code or result.status,
            )
        return RecoveryDecision(
            gate=result.gate, status=result.status, safety_class=result.safety_class,
            action=RecoveryAction.WARNING if result.safety_class is SafetyClass.SOFT_WARNING else RecoveryAction.FALLBACK,
            recovery_methods=["fallback"], reason=result.reason_code or result.status,
        )
