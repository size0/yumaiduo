"""Agent runtime orchestration independent from provider and Pi."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable, Mapping

from .context import BusinessContextCompactor
from .contracts import AgentRequest, AgentResult, sanitize_public
from .events import AgentEvent, AgentEventBus
from .model_gateway import ModelGateway
from .recovery import RecoveryPolicy
from .reply_validator import ReplyValidator
from .session import AgentSessionStore
from .tool_dispatcher import ToolDispatcher
from .tool_policy import ToolPolicy
from .trace import TraceRecorder

Delegate = Callable[[AgentRequest], Awaitable[str | AgentResult] | str | AgentResult]


class AgentRuntime:
    def __init__(self, *, model_gateway: ModelGateway | None = None, tool_dispatcher: ToolDispatcher | None = None, session_store: AgentSessionStore | None = None, event_bus: AgentEventBus | None = None, reply_validator: ReplyValidator | None = None, recovery_policy: RecoveryPolicy | None = None, delegate: Delegate | None = None, propagate_delegate_errors: bool = False) -> None:
        self.model_gateway = model_gateway or ModelGateway()
        self.tool_dispatcher = tool_dispatcher
        self.session_store = session_store or AgentSessionStore()
        self.event_bus = event_bus or AgentEventBus()
        self.reply_validator = reply_validator or ReplyValidator()
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.compactor = BusinessContextCompactor()
        self.delegate = delegate
        self.propagate_delegate_errors = propagate_delegate_errors

    async def run(self, request: AgentRequest) -> AgentResult:
        trace = TraceRecorder()
        self.session_store.begin(request.session_key, request.run_id)
        # Dispatcher counters/deduplication are request-scoped. Clone the
        # registry/policy so concurrent runs cannot reset or consume each
        # other's budgets.
        dispatcher = (
            ToolDispatcher(self.tool_dispatcher.registry, self.tool_dispatcher.policy)
            if self.tool_dispatcher is not None else None
        )
        await self._emit(request, trace, "agent_start")
        if self.delegate is not None:
            # The legacy adapter does not expose provider/tool internals.  We
            # still publish a synthetic turn around the delegated run so
            # observers can correlate its lifecycle with native runs.
            await self._emit(request, trace, "turn_start", {"mode": "legacy_delegate"}, turn_index=0)
            await self._emit(request, trace, "model_response_start", {"mode": "legacy_delegate"}, turn_index=0)
            try:
                delegated = self.delegate(request)
                if inspect.isawaitable(delegated):
                    delegated = await delegated
                result = delegated if isinstance(delegated, AgentResult) else AgentResult("settled", str(delegated or ""), trace_id=trace.trace_id, finish_reason="legacy_delegate")
                if not result.trace_id:
                    result.trace_id = trace.trace_id
                await self._emit(request, trace, "model_response_end", {"status": result.status, "reply_length": len(result.reply_text or ""), "tool_count": len(result.tool_calls)}, turn_index=0)
                if self.session_store.is_interrupted(request.session_key, request.run_id):
                    result = AgentResult("interrupted", trace_id=trace.trace_id, finish_reason="superseded")
                    self._finalize_session(request, result)
                    await self._emit(request, trace, "agent_interrupted")
                    return result
                if result.status != "settled":
                    if not result.reply_text:
                        self._finalize_session(request, result)
                        await self._emit_terminal(request, trace, result)
                        return result
                    validation = self.reply_validator.validate(
                        result.reply_text,
                        authoritative_facts=request.runtime_context.get("authoritative_facts")
                        if isinstance(request.runtime_context.get("authoritative_facts"), Mapping)
                        else request.runtime_context,
                    )
                    await self._emit(request, trace, "reply_validation", {"allowed": validation.allowed, "reasons": list(validation.reasons), "status": result.status})
                    if not validation.allowed:
                        await self._emit(request, trace, "reply_suppressed", {"reasons": list(validation.reasons)})
                        result = AgentResult("blocked", trace_id=trace.trace_id, finish_reason="reply_validation", usage=result.usage)
                        self._finalize_session(request, result)
                        await self._emit_terminal(request, trace, result)
                        return result
                    self._finalize_session(request, result)
                    await self._emit(request, trace, "reply_sent", {"reply_length": len(result.reply_text or ""), "status": result.status})
                    await self._emit_terminal(request, trace, result)
                    return result
                validation = self.reply_validator.validate(result.reply_text, authoritative_facts=request.runtime_context.get("authoritative_facts") if isinstance(request.runtime_context.get("authoritative_facts"), Mapping) else request.runtime_context)
                await self._emit(request, trace, "reply_validation", {"allowed": validation.allowed, "reasons": list(validation.reasons), "status": result.status})
                if not validation.allowed:
                    await self._emit(request, trace, "reply_suppressed", {"reasons": list(validation.reasons)})
                    result = AgentResult("blocked", trace_id=trace.trace_id, finish_reason="reply_validation", usage=result.usage)
                    self._finalize_session(request, result)
                    await self._emit_terminal(request, trace, result)
                    return result
                self._finalize_session(request, result)
                await self._emit(request, trace, "reply_sent", {"reply_length": len(result.reply_text or ""), "status": result.status})
                await self._emit(request, trace, "agent_settled")
                return result
            except asyncio.CancelledError:
                await self._emit(request, trace, "model_response_end", {"status": "interrupted", "mode": "legacy_delegate"}, turn_index=0)
                self.session_store.interrupt(request.session_key, request.run_id)
                await self._emit(request, trace, "agent_interrupted")
                raise
            except Exception:
                await self._emit(request, trace, "model_response_end", {"status": "error", "mode": "legacy_delegate"}, turn_index=0)
                self.session_store.transition(request.session_key, "FAILED", run_id=request.run_id)
                await self._emit(request, trace, "agent_failed")
                if self.propagate_delegate_errors:
                    raise
                return AgentResult("failed", trace_id=trace.trace_id, finish_reason="delegate_error")
        try:
            result = await self._model_loop(request, trace, tool_dispatcher=dispatcher)
            self._finalize_session(request, result)
            await self._emit_terminal(request, trace, result)
            return result
        except asyncio.CancelledError:
            self.session_store.interrupt(request.session_key, request.run_id)
            await self._emit(request, trace, "agent_interrupted")
            raise
        except Exception:
            self.session_store.transition(request.session_key, "FAILED", run_id=request.run_id)
            await self._emit(request, trace, "agent_failed")
            return AgentResult("failed", trace_id=trace.trace_id, finish_reason="runtime_error")

    def _finalize_session(self, request: AgentRequest, result: AgentResult) -> None:
        """Persist the result's terminal state without clobbering a newer run."""
        state = {
            "settled": "SETTLED",
            "waiting_buyer": "WAITING_BUYER",
            "interrupted": "INTERRUPTED",
            "blocked": "FAILED",
            "failed": "FAILED",
        }.get(result.status)
        if state is not None:
            self.session_store.transition(request.session_key, state, run_id=request.run_id)

    async def _model_loop(self, request: AgentRequest, trace: TraceRecorder, *, tool_dispatcher: ToolDispatcher | None = None) -> AgentResult:
        # Mode budgets are the authoritative limits for a run.  The request
        # deadline may further reduce the configured wall-clock budget, but it
        # must not silently increase a conservative mode (for example
        # ``hybrid``) to the full-agent limits.
        policy = tool_dispatcher.policy if tool_dispatcher is not None else ToolPolicy()
        budget = policy.budget_for(request.mode, request.deadline_seconds)
        context = self.compactor.build(request.history, request.runtime_context)
        await self._emit(request, trace, "context_restore", {"history_messages": len(request.history), "restored_messages": len(context.messages), "phase": context.phase})
        if len(context.messages) < len(request.history):
            await self._emit(request, trace, "context_compaction", {"before_messages": len(request.history), "after_messages": len(context.messages), "retained_authority_keys": sorted(context.authoritative)})
        messages = list(context.messages) + [{"role": "user", "content": request.user_message}]
        calls: list[dict[str, Any]] = []
        started = time.monotonic()
        for turn in range(budget.max_model_rounds):
            if time.monotonic() - started >= budget.max_seconds:
                return AgentResult("failed", tool_calls=calls, trace_id=trace.trace_id, finish_reason="deadline")
            if self.session_store.is_interrupted(request.session_key, request.run_id):
                return AgentResult("interrupted", tool_calls=calls, trace_id=trace.trace_id, finish_reason="superseded")
            await self._emit(request, trace, "turn_start", {"mode": request.mode}, turn_index=turn)
            await self._emit(request, trace, "model_response_start", {"message_count": len(messages)}, turn_index=turn)
            try:
                visible_tools = (
                    tool_dispatcher.visible_schemas(phase=context.phase, mode=request.mode)
                    if tool_dispatcher is not None
                    else []
                )
                response = await self.model_gateway.complete(
                    messages,
                    visible_tools,
                    timeout_seconds=max(0.1, budget.max_seconds - (time.monotonic() - started)),
                )
            except asyncio.CancelledError:
                await self._emit(request, trace, "model_response_end", {"status": "interrupted"}, turn_index=turn)
                raise
            except Exception as exc:
                await self._emit(request, trace, "model_response_end", {"status": "error", "error_type": type(exc).__name__}, turn_index=turn)
                raise
            text = str(response.get("text") or response.get("content") or "").strip()
            tool_calls = response.get("tool_calls") if isinstance(response.get("tool_calls"), list) else []
            await self._emit(request, trace, "model_response_end", {"status": "ok", "reply_length": len(text), "tool_count": len(tool_calls)}, turn_index=turn)
            if not tool_calls:
                validation = self.reply_validator.validate(text, authoritative_facts=context.authoritative)
                await self._emit(request, trace, "reply_validation", {"allowed": validation.allowed, "reasons": list(validation.reasons), "status": "settled" if validation.allowed else "blocked"}, turn_index=turn)
                if validation.allowed:
                    await self._emit(request, trace, "reply_sent", {"reply_length": len(text)}, turn_index=turn)
                    return AgentResult("settled", text, calls, trace.trace_id, "model_final")
                await self._emit(request, trace, "reply_suppressed", {"reasons": list(validation.reasons)}, turn_index=turn)
                return AgentResult("blocked", tool_calls=calls, trace_id=trace.trace_id, finish_reason="reply_validation")
            if tool_dispatcher is None:
                await self._emit(request, trace, "tool_blocked", {"reason": "tool_dispatcher_unavailable", "tool_count": len(tool_calls)}, turn_index=turn)
                return AgentResult("blocked", tool_calls=calls, trace_id=trace.trace_id, finish_reason="tool_dispatcher_unavailable")
            remaining_tool_calls = budget.max_tool_calls - len(calls)
            if remaining_tool_calls <= 0 or len(tool_calls) > remaining_tool_calls:
                await self._emit(
                    request,
                    trace,
                    "tool_blocked",
                    {"reason": "max_tool_calls", "requested": len(tool_calls), "remaining": max(0, remaining_tool_calls)},
                    turn_index=turn,
                )
                return AgentResult("blocked", tool_calls=calls, trace_id=trace.trace_id, finish_reason="max_tool_calls")
            tool_dispatcher.start_round()
            for call in tool_calls:
                remaining = budget.max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    return AgentResult("failed", tool_calls=calls, trace_id=trace.trace_id, finish_reason="deadline")
                name = str(call.get("name") or "")
                args = call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}
                payload = {"name": name, "parameter_hash": trace.parameter_hash(args)}
                await self._emit(request, trace, "tool_call_start", payload, turn_index=turn)
                self.session_store.transition(request.session_key, "WAITING_TOOL", run_id=request.run_id)
                try:
                    result = await tool_dispatcher.dispatch(
                        name, args, mode=request.mode, phase=context.phase,
                        runtime_context=dict(request.runtime_context),
                        timeout_seconds=min(10.0, remaining),
                    )
                except asyncio.CancelledError:
                    await self._emit(request, trace, "tool_call_end", {"name": name, "status": "interrupted"}, turn_index=turn)
                    raise
                except Exception as exc:
                    await self._emit(request, trace, "tool_call_end", {"name": name, "status": "error", "error_type": type(exc).__name__}, turn_index=turn)
                    raise
                result_dict = result.as_dict()
                await self._emit(request, trace, "tool_call_end", {"name": name, "status": result.status, "stop_condition": result.stop_condition, "attempt": 0}, turn_index=turn)
                blocked_reason = result.stop_condition
                if not blocked_reason and result.status == "error" and (result.summary == "unknown tool" or result.summary.startswith("tool blocked:")):
                    blocked_reason = result.summary
                if blocked_reason and blocked_reason not in {"duplicate_call"} and result.status == "error":
                    await self._emit(request, trace, "tool_blocked", {"name": name, "reason": blocked_reason}, turn_index=turn)
                definition = tool_dispatcher.registry.get(name)
                recovery = self.recovery_policy.decide(
                    attempts=0,
                    read_only=bool(definition and definition.read_only),
                    retry_allowed=result.status == "error" and bool(result.retry.get("allowed")),
                    interrupted=self.session_store.is_interrupted(request.session_key, request.run_id),
                )
                retry_exhausted = False
                if result.status == "error" and recovery.retry:
                    self.session_store.transition(request.session_key, "RECOVERING", run_id=request.run_id)
                    await self._emit(request, trace, "tool_retry", {"name": name, "attempt": 1, "max_attempts": 1, "reason": recovery.reason}, turn_index=turn)
                    retry_remaining = budget.max_seconds - (time.monotonic() - started)
                    if retry_remaining <= 0:
                        return AgentResult("failed", tool_calls=calls, trace_id=trace.trace_id, finish_reason="deadline")
                    await self._emit(request, trace, "tool_call_start", {**payload, "attempt": 1}, turn_index=turn)
                    try:
                        result = await tool_dispatcher.dispatch(
                            name,
                            args,
                            mode=request.mode,
                            phase=context.phase,
                            runtime_context=dict(request.runtime_context),
                            timeout_seconds=min(10.0, retry_remaining),
                            retry_attempt=True,
                        )
                    except asyncio.CancelledError:
                        await self._emit(request, trace, "tool_call_end", {"name": name, "status": "interrupted", "attempt": 1}, turn_index=turn)
                        raise
                    except Exception as exc:
                        await self._emit(request, trace, "tool_call_end", {"name": name, "status": "error", "error_type": type(exc).__name__, "attempt": 1}, turn_index=turn)
                        raise
                    result_dict = result.as_dict()
                    await self._emit(request, trace, "tool_call_end", {"name": name, "status": result.status, "stop_condition": result.stop_condition, "attempt": 1}, turn_index=turn)
                    retry_exhausted = result.status == "error"
                if retry_exhausted:
                    await self._emit(request, trace, "tool_blocked", {"name": name, "reason": "retry_budget_exhausted"}, turn_index=turn)
                terminal_status: str | None = None
                if (
                    result.status == "error"
                    and not recovery.retry
                    and definition is not None
                ):
                    terminal_status = "waiting_buyer" if recovery.next_state == "WAITING_BUYER" else "failed"
                    await self._emit(request, trace, "tool_blocked", {"name": name, "reason": recovery.reason}, turn_index=turn)
                calls.append({"name": name, "arguments": sanitize_public(dict(args)), "result": result_dict})
                messages.append({"role": "tool", "name": name, "content": result_dict})
                if retry_exhausted or terminal_status:
                    return AgentResult(
                        terminal_status or "failed",
                        tool_calls=calls,
                        trace_id=trace.trace_id,
                        finish_reason="retry_exhausted" if retry_exhausted else recovery.reason,
                    )
                self.session_store.transition(request.session_key, "RUNNING", run_id=request.run_id)
        return AgentResult("failed", tool_calls=calls, trace_id=trace.trace_id, finish_reason="max_model_rounds")

    async def _emit(self, request: AgentRequest, trace: TraceRecorder, name: str, payload: Mapping[str, Any] | None = None, *, turn_index: int = 0) -> None:
        event = AgentEvent(name, request.run_id, request.session_id, trace.trace_id, request.tenant_id, request.shop_id, request.buyer_id, request.chat_id, request.event_id, turn_index=turn_index, payload=payload or {})
        trace.record(event)
        await self.event_bus.publish(event)

    async def _emit_terminal(self, request: AgentRequest, trace: TraceRecorder, result: AgentResult) -> None:
        """Publish the terminal lifecycle event for a non-exceptional run."""
        if result.status == "settled":
            await self._emit(request, trace, "agent_settled")
        elif result.status == "interrupted":
            await self._emit(request, trace, "agent_interrupted")
        elif result.status in {"failed", "blocked"}:
            await self._emit(request, trace, "agent_failed", {"status": result.status, "finish_reason": result.finish_reason})


class LegacyAgentRuntime(AgentRuntime):
    """Compatibility wrapper used while CustomerServiceChatService keeps its loop."""

    def __init__(self, delegate: Delegate, **kwargs: Any) -> None:
        kwargs.setdefault("propagate_delegate_errors", True)
        super().__init__(delegate=delegate, **kwargs)
