"""Stable contracts shared by the V4 agent harness.

These contracts deliberately contain orchestration metadata only.  Authoritative
commerce facts remain in the rules/runtime layer and are never inferred here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping


AGENT_STATUSES = frozenset({"settled", "waiting_buyer", "blocked", "failed", "interrupted"})
TOOL_STATUSES = frozenset({"success", "warning", "error"})
_SENSITIVE_KEYS = re.compile(
    r"(?i)(token|cookie|csrf|secret|authorization|password|api[_ -]?key|"
    r"raw[_ -]?response|provider[_ -]?response|request[_ -]?headers|prompt|"
    r"reasoning|ticket[_ -]?(?:code|voucher|number)|ticket(?:code|voucher|number))"
)


def sanitize_public(value: Any) -> Any:
    """Return a bounded, secret-free representation for model/trace output."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEYS.search(str(key)) else sanitize_public(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_public(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:2000]
    return value


@dataclass(slots=True)
class AgentRequest:
    run_id: str
    session_id: str
    tenant_id: str
    shop_id: str
    buyer_id: str
    chat_id: str
    event_id: str | None
    user_message: str
    history: list[dict[str, Any]] = field(default_factory=list)
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    mode: str = "hybrid"
    deadline_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("run_id", "session_id", "tenant_id", "shop_id", "buyer_id", "chat_id"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            setattr(self, name, value)
        self.user_message = str(self.user_message or "").strip()
        if not self.user_message:
            raise ValueError("user_message must not be empty")
        self.history = [dict(item) for item in self.history if isinstance(item, Mapping)]
        self.runtime_context = dict(self.runtime_context or {})
        self.mode = str(self.mode or "hybrid").strip().lower()
        self.deadline_seconds = max(0.1, float(self.deadline_seconds))

    @property
    def session_key(self) -> str:
        return "\0".join((self.tenant_id, self.shop_id, self.buyer_id, self.chat_id))


@dataclass(slots=True)
class AgentResult:
    status: str
    reply_text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.status = str(self.status).strip().lower()
        if self.status not in AGENT_STATUSES:
            raise ValueError(f"unsupported agent status: {self.status}")
        if self.reply_text is not None:
            self.reply_text = str(self.reply_text)
        self.tool_calls = [dict(item) for item in self.tool_calls if isinstance(item, Mapping)]
        self.trace_id = str(self.trace_id or "")
        self.finish_reason = str(self.finish_reason or "")
        if self.usage is not None:
            self.usage = dict(self.usage)


@dataclass(slots=True)
class ToolResult:
    status: str
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)
    retry: Mapping[str, Any] = field(default_factory=lambda: {"allowed": False, "max_attempts": 0})
    stop_condition: str | None = None

    def __post_init__(self) -> None:
        self.status = str(self.status).strip().lower()
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"unsupported tool status: {self.status}")
        self.summary = str(self.summary or "")[:1000]
        self.data = sanitize_public(dict(self.data or {}))
        self.next_actions = [str(item) for item in self.next_actions]
        retry = dict(self.retry or {})
        retry["allowed"] = bool(retry.get("allowed", False))
        retry["max_attempts"] = max(0, min(int(retry.get("max_attempts", 0)), 1))
        self.retry = retry
        if self.stop_condition is not None:
            self.stop_condition = str(self.stop_condition)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "data": dict(self.data),
            "next_actions": list(self.next_actions),
            "retry": dict(self.retry),
            "stop_condition": self.stop_condition,
        }

    @classmethod
    def success(cls, summary: str, data: Mapping[str, Any] | None = None, **kwargs: Any) -> "ToolResult":
        return cls("success", summary, data or {}, **kwargs)

    @classmethod
    def warning(cls, summary: str, data: Mapping[str, Any] | None = None, **kwargs: Any) -> "ToolResult":
        return cls("warning", summary, data or {}, **kwargs)

    @classmethod
    def error(cls, summary: str, data: Mapping[str, Any] | None = None, **kwargs: Any) -> "ToolResult":
        return cls("error", summary, data or {}, **kwargs)


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    max_model_rounds: int = 8
    max_tool_calls: int = 12
    max_seconds: float = 30.0
    max_retry_attempts: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_model_rounds", max(1, int(self.max_model_rounds)))
        object.__setattr__(self, "max_tool_calls", max(0, int(self.max_tool_calls)))
        object.__setattr__(self, "max_seconds", max(0.1, float(self.max_seconds)))
        object.__setattr__(self, "max_retry_attempts", max(0, min(1, int(self.max_retry_attempts))))
