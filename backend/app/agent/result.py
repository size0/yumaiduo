from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .observations import Observation


class AgentStatus(StrEnum):
    REPLIED = "REPLIED"
    RETRY = "RETRY"
    MANUAL = "MANUAL"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: AgentStatus
    reply: str | None = None
    reason: str = ""
    trace_id: str = ""
    observations: tuple[Observation, ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.status is AgentStatus.REPLIED and not str(self.reply or "").strip():
            raise ValueError("agent_reply_required")
        if not self.reason:
            raise ValueError("agent_result_reason_required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reply": self.reply,
            "reason": self.reason,
            "trace_id": self.trace_id,
            "observations": [item.as_dict() for item in self.observations],
            "tool_calls": [dict(item) for item in self.tool_calls],
        }
