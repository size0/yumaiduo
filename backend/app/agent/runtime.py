from __future__ import annotations

import asyncio
import json
import uuid
from time import monotonic
from typing import Any, Callable, Mapping, Protocol

from .context import AgentContext
from .observations import Observation
from .registry import ToolRegistry
from .result import AgentResult, AgentStatus
from .validators import validate_quote_reply


class ModelClient(Protocol):
    async def complete(
        self,
        context: dict[str, Any],
        tools: list[dict[str, Any]],
        *,
        trace_id: str,
    ) -> Mapping[str, Any]: ...


class AgentHarness:
    """Single read-only Agent loop for the reset phase."""

    def __init__(
        self,
        *,
        model: ModelClient,
        registry: ToolRegistry,
        max_rounds: int = 8,
        max_tool_calls: int = 8,
        deadline_seconds: float = 30.0,
        per_tool_timeout_seconds: float = 10.0,
    ) -> None:
        if max_rounds < 1 or max_rounds > 20:
            raise ValueError("agent_max_rounds_invalid")
        if max_tool_calls < 1 or max_tool_calls > 32:
            raise ValueError("agent_max_tool_calls_invalid")
        if not 1 <= deadline_seconds <= 180:
            raise ValueError("agent_deadline_invalid")
        if not 0.1 <= per_tool_timeout_seconds <= deadline_seconds:
            raise ValueError("agent_tool_timeout_invalid")
        self._model = model
        self._registry = registry
        self._max_rounds = max_rounds
        self._max_tool_calls = max_tool_calls
        self._deadline_seconds = deadline_seconds
        self._per_tool_timeout_seconds = per_tool_timeout_seconds

    async def run(
        self,
        context: AgentContext,
        *,
        freshness_check: Callable[[], bool] | None = None,
    ) -> AgentResult:
        trace_id = context.trace_id or f"agent-{uuid.uuid4().hex}"
        current = AgentContext(
            identity=context.identity,
            current_event=context.current_event,
            recent_messages=context.recent_messages,
            observations=context.observations,
            business_state=context.business_state,
            available_tools=tuple(self._registry.schemas()),
            trace_id=trace_id,
            conversation_revision=context.conversation_revision,
            latest_buyer_message_id=context.latest_buyer_message_id,
        )
        calls: list[dict[str, Any]] = []
        seen_calls: set[tuple[str, str]] = set()
        started = monotonic()
        deadline = started + min(self._deadline_seconds, float(context.business_state.get("deadline_seconds", self._deadline_seconds)))
        for round_index in range(self._max_rounds):
            if monotonic() >= deadline:
                return AgentResult(
                    status=AgentStatus.RETRY, reason="turn_timeout", trace_id=trace_id,
                    observations=current.observations, tool_calls=tuple(calls),
                )
            try:
                response = await asyncio.wait_for(
                    self._model.complete(
                        current.as_dict(), self._registry.schemas(), trace_id=trace_id,
                    ), timeout=max(0.1, deadline - monotonic()),
                )
            except Exception:
                return AgentResult(
                    status=AgentStatus.RETRY,
                    reason="model_unavailable",
                    trace_id=trace_id,
                    observations=current.observations,
                    tool_calls=tuple(calls),
                )
            final = response.get("final")
            if isinstance(final, str) and final.strip():
                if freshness_check is not None and not freshness_check():
                    return AgentResult(
                        status=AgentStatus.RETRY, reason="cancelled_stale",
                        trace_id=trace_id, observations=current.observations,
                        tool_calls=tuple(calls),
                    )
                valid, reason = validate_quote_reply(final.strip(), current.observations)
                if not valid:
                    return AgentResult(
                        status=AgentStatus.RETRY, reason=f"reply_validation_failed:{reason}",
                        trace_id=trace_id, observations=current.observations,
                        tool_calls=tuple(calls),
                    )
                return AgentResult(
                    status=AgentStatus.REPLIED,
                    reply=final.strip(),
                    reason="final_reply",
                    trace_id=trace_id,
                    observations=current.observations,
                    tool_calls=tuple(calls),
                )
            raw_call = response.get("tool_call")
            if not isinstance(raw_call, Mapping):
                return AgentResult(
                    status=AgentStatus.FAILED,
                    reason="model_response_invalid",
                    trace_id=trace_id,
                    observations=current.observations,
                    tool_calls=tuple(calls),
                )
            name = str(raw_call.get("name") or "").strip()
            arguments = raw_call.get("arguments")
            if not name or not isinstance(arguments, Mapping):
                return AgentResult(
                    status=AgentStatus.FAILED,
                    reason="tool_call_invalid",
                    trace_id=trace_id,
                    observations=current.observations,
                    tool_calls=tuple(calls),
                )
            if freshness_check is not None and not freshness_check():
                return AgentResult(
                    status=AgentStatus.RETRY, reason="cancelled_stale",
                    trace_id=trace_id, observations=current.observations,
                    tool_calls=tuple(calls),
                )
            normalized_arguments = dict(arguments)
            key = (name, json.dumps(normalized_arguments, ensure_ascii=False, sort_keys=True, default=str))
            if key in seen_calls:
                observation = Observation.warning(
                    "duplicate_tool_call_suppressed",
                    message="相同工具请求已执行，不能重复调用。",
                )
            else:
                if len(calls) >= self._max_tool_calls:
                    return AgentResult(
                        status=AgentStatus.RETRY, reason="tool_budget_exhausted",
                        trace_id=trace_id, observations=current.observations,
                        tool_calls=tuple(calls),
                    )
                seen_calls.add(key)
                try:
                    observation = await asyncio.wait_for(
                        self._registry.execute(
                            name,
                            normalized_arguments,
                            identity={
                                **{str(key): str(value) for key, value in current.identity.items()},
                                "event_id": str(current.current_event.get("event_id") or ""),
                                "trace_id": trace_id,
                            },
                            context=current.as_dict(),
                        ),
                        timeout=min(self._per_tool_timeout_seconds, max(0.1, deadline - monotonic())),
                    )
                except Exception:
                    observation = Observation.warning(
                        "tool_timeout",
                        message="工具查询超时，请稍后重试；再次失败将转人工确认。",
                    )
            calls.append({
                "round": round_index,
                "name": name,
                "arguments": normalized_arguments,
                "observation": observation.as_dict(),
            })
            current = current.with_observation(observation)
        return AgentResult(
            status=AgentStatus.RETRY,
            reason="round_budget_exhausted",
            trace_id=trace_id,
            observations=current.observations,
            tool_calls=tuple(calls),
        )
