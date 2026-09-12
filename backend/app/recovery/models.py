from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SafetyClass(StrEnum):
    HARD_SAFETY = "HARD_SAFETY"
    RECOVERABLE = "RECOVERABLE"
    SOFT_WARNING = "SOFT_WARNING"


class RecoveryAction(StrEnum):
    STOP = "STOP"
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    FALLBACK = "FALLBACK"
    ASK_CLARIFICATION = "ASK_CLARIFICATION"
    WARNING = "WARNING"


class GateResult(BaseModel):
    gate: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=120)
    success: bool
    safety_class: SafetyClass
    facts: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    candidates: list[Any] = Field(default_factory=list)
    reason_code: str | None = Field(default=None, max_length=120)
    retryable: bool = False
    provider_verified: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecoveryDecision(BaseModel):
    gate: str
    status: str
    safety_class: SafetyClass
    action: RecoveryAction
    missing_fields: list[str] = Field(default_factory=list)
    recovery_methods: list[str] = Field(default_factory=list)
    reason: str = ""
    stop_scope: str | None = None
