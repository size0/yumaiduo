"""Bounded retry and recovery decisions."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    retry: bool
    reason: str
    next_state: str = "RUNNING"


class RecoveryPolicy:
    def __init__(self, *, max_attempts: int = 1) -> None:
        self.max_attempts = max(0, min(1, int(max_attempts)))

    def decide(self, *, attempts: int, read_only: bool, retry_allowed: bool = True, interrupted: bool = False) -> RecoveryDecision:
        if interrupted:
            return RecoveryDecision(False, "run_interrupted", "INTERRUPTED")
        if not read_only:
            return RecoveryDecision(False, "write_failure_not_retried", "WAITING_BUYER")
        if not retry_allowed or attempts >= self.max_attempts:
            return RecoveryDecision(False, "retry_budget_exhausted", "FAILED")
        return RecoveryDecision(True, "transient_read_failure", "RECOVERING")
