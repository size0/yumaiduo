from __future__ import annotations

import asyncio
from time import time

import pytest

from app.agent_runtime import (
    AgentEvent,
    AgentEventBus,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    BusinessContextCompactor,
    AgentSessionStore,
    ModelGateway,
    ReplyValidator,
    RecoveryPolicy,
    RuntimeBudget,
    sanitize_public,
    TraceRecorder,
    ToolDefinition,
    ToolDispatcher,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
)


def request(run_id: str = "run-1") -> AgentRequest:
    return AgentRequest(run_id, "session", "tenant", "shop", "buyer", "chat", None, "查询场次")


def test_contracts_and_budget() -> None:
    req = request()
    assert req.session_key == "tenant\0shop\0buyer\0chat"
    assert RuntimeBudget(max_model_rounds=0, max_tool_calls=-1).max_model_rounds == 1
    assert ToolResult.success("ok", {"x": 1}).as_dict()["status"] == "success"
    safe = ToolResult.success("ok", {"token": "secret", "ticketCode": "ABC123", "ticket_voucher": "private", "raw_response": {"body": "private"}, "value": 1}).as_dict()
    assert safe["data"]["token"] == "[REDACTED]"
    assert safe["data"]["ticketCode"] == "[REDACTED]" and safe["data"]["ticket_voucher"] == "[REDACTED]"
    assert safe["data"]["value"] == 1
    assert sanitize_public({"api_key": "secret"})["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_and_applies_phase_policy() -> None:
    registry = ToolRegistry()
    calls = 0

    async def handler(args):
        nonlocal calls
        calls += 1
        return ToolResult.success("ok", {"value": args["value"]})

    registry.register(ToolDefinition("show.list", handler=handler, phases=frozenset({"consultation"})))
    dispatcher = ToolDispatcher(registry)
    first = await dispatcher.dispatch("show.list", {"value": 1}, mode="hybrid", phase="consultation")
    second = await dispatcher.dispatch("show.list", {"value": 1}, mode="hybrid", phase="consultation")
    blocked = await dispatcher.dispatch("show.list", {}, mode="hybrid", phase="order")
    assert first.status == "success" and second.stop_condition == "duplicate_call"
    assert blocked.status == "error" and calls == 1


@pytest.mark.asyncio
async def test_write_gate_allows_only_one_write_per_round() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("order.change_price", handler=lambda args: ToolResult.success("changed"), read_only=False, phases=frozenset({"order"})))
    registry.register(ToolDefinition("order.cancel", handler=lambda args: ToolResult.success("cancelled"), read_only=False, phases=frozenset({"order"})))
    dispatcher = ToolDispatcher(registry)
    first = await dispatcher.dispatch("order.change_price", {}, mode="simulation", phase="order")
    second = await dispatcher.dispatch("order.cancel", {}, mode="simulation", phase="order")
    assert first.status == "success" and second.stop_condition == "one_write_tool_per_round"


@pytest.mark.asyncio
async def test_legacy_runtime_delegate_and_interrupt() -> None:
    store = AgentSessionStore()

    async def delegate(req):
        await asyncio.sleep(0)
        return "已收到"

    runtime = AgentRuntime(delegate=delegate, session_store=store)
    result = await runtime.run(request())
    assert result.status == "settled" and result.reply_text == "已收到"
    store.begin(request().session_key, "run-2")
    store.begin(request().session_key, "run-3")
    assert store.is_interrupted(request().session_key, "run-2") is True


@pytest.mark.asyncio
async def test_model_gateway_timeout_and_cancellation() -> None:
    async def slow(messages, tools):
        await asyncio.sleep(0.2)
        return {"text": "ok"}

    with pytest.raises(asyncio.TimeoutError):
        await ModelGateway(slow).complete([], timeout_seconds=0.01)


def test_context_compaction_keeps_authority_and_recent_errors() -> None:
    compactor = BusinessContextCompactor(max_messages=2)
    context = compactor.build(
        [{"role": "user", "content": str(index)} for index in range(4)],
        {
            "confirmed_facts": {"cinema": "A"},
            "current_quote": {"quote_id": "q1"},
            "missing_fields": ["seat"],
            "recent_tool_errors": ["one", "two", "three", "four"],
            "phase": "recognition",
        },
    )
    assert [item["content"] for item in context.messages] == ["2", "3"]
    assert context.authoritative["confirmed_facts"] == {"cinema": "A"}
    assert context.authoritative["current_quote"] == {"quote_id": "q1"}
    assert context.recent_errors == ["two", "three", "four"]
    assert context.phase == "recognition"


def test_context_compaction_accepts_nested_authoritative_facts() -> None:
    context = BusinessContextCompactor().build(
        [],
        {"authoritative_facts": {"order_status": "pending_payment", "price_verified": True}},
    )
    assert context.authoritative == {"order_status": "pending_payment", "price_verified": True}


@pytest.mark.asyncio
async def test_event_bus_and_trace_redact_failures() -> None:
    bus = AgentEventBus()
    seen: list[str] = []

    def sync_subscriber(event: AgentEvent) -> None:
        seen.append(event.name)

    async def async_subscriber(event: AgentEvent) -> None:
        seen.append(event.run_id)

    def failing_subscriber(_: AgentEvent) -> None:
        raise RuntimeError("observer failure")

    bus.subscribe(sync_subscriber)
    bus.subscribe(async_subscriber)
    bus.subscribe(failing_subscriber)
    event = AgentEvent("agent_start", "run", "session", "trace", "tenant", "shop", "buyer", "chat", payload={"token": "secret", "safe": "ok"})
    await bus.publish(event)
    trace = TraceRecorder(trace_id="trace")
    trace.record(event)
    assert seen == ["agent_start", "run"]
    assert trace.snapshot()[0]["payload"]["token"] == "[REDACTED]"
    assert trace.parameter_hash({"a": 1})


def test_recovery_policy_decisions() -> None:
    policy = RecoveryPolicy(max_attempts=1)
    assert policy.decide(attempts=0, read_only=True).retry
    assert policy.decide(attempts=1, read_only=True).next_state == "FAILED"
    assert policy.decide(attempts=0, read_only=False).next_state == "WAITING_BUYER"
    assert policy.decide(attempts=0, read_only=True, retry_allowed=False).reason == "retry_budget_exhausted"
    assert policy.decide(attempts=0, read_only=True, interrupted=True).next_state == "INTERRUPTED"


def test_reply_validator_transaction_and_sensitive_branches() -> None:
    validator = ReplyValidator()
    assert validator.validate("", pending_tool_calls=1).reasons == ("empty_reply", "pending_tool_calls")
    assert not validator.validate("支付成功，请查收").allowed
    assert validator.validate("支付成功，请查收", authoritative_facts={"order_status": "paid"}).allowed
    assert not validator.validate("请使用 Bearer abc").allowed
    assert not validator.validate("当前报价100元").allowed
    assert validator.validate("当前报价100元", authoritative_facts={"current_quote": {"unit_quote_cents": 10000}}).allowed
    assert not validator.validate("有票且座位可用").allowed
    assert not validator.validate("已选座位第八排").allowed
    assert not validator.validate("订单状态正常").allowed


@pytest.mark.asyncio
async def test_model_gateway_sync_and_empty_paths() -> None:
    assert (await ModelGateway().complete([], timeout_seconds=1))["text"] == ""
    gateway = ModelGateway(lambda _messages, _tools: {"text": "ok"})
    assert (await gateway.complete([], []))["text"] == "ok"
    string_gateway = ModelGateway(lambda _messages, _tools: "plain")
    assert (await string_gateway.complete([], []))["text"] == "plain"


@pytest.mark.asyncio
async def test_dispatcher_error_budget_and_write_gate() -> None:
    registry = ToolRegistry()

    async def failing(_: object):
        raise RuntimeError("failure")

    registry.register(ToolDefinition("read.fail", handler=failing, retry_allowed=True))
    registry.register(ToolDefinition("order.write", handler=lambda _: ToolResult.success("ok"), read_only=False, phases=frozenset({"order"})))
    dispatcher = ToolDispatcher(registry, ToolPolicy())
    assert (await dispatcher.dispatch("missing", {}, mode="hybrid")).status == "error"
    assert (await dispatcher.dispatch("read.fail", {}, mode="hybrid")).retry["allowed"] is True
    dispatcher.start_round()
    first = await dispatcher.dispatch("order.write", {}, mode="full", phase="order")
    second = await dispatcher.dispatch("order.write", {"different": True}, mode="full", phase="order")
    assert first.status == "success"
    assert second.status == "error" and second.stop_condition == "one_write_tool_per_round"


def test_session_checkpoint_and_ttl() -> None:
    store = AgentSessionStore(ttl_seconds=1)
    state = store.begin("tenant\0shop\0buyer\0chat", "run")
    assert state.state == "RUNNING"
    checkpoint = store.checkpoint_state(state.session_key, "run", {"phase": "quote"})
    assert checkpoint.checkpoint == {"phase": "quote"}
    assert store.transition(state.session_key, "WAITING_BUYER", run_id="run").state == "WAITING_BUYER"


class _MemorySessionPersistence:
    def __init__(self, *, fail_load: bool = False, fail_save: bool = False) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.fail_load = fail_load
        self.fail_save = fail_save

    def load(self, session_key: str):
        if self.fail_load:
            raise RuntimeError("load unavailable")
        record = self.records.get(session_key)
        return dict(record) if record is not None else None

    def save(self, session_key: str, snapshot):
        if self.fail_save:
            raise RuntimeError("save unavailable")
        self.records[session_key] = dict(snapshot)


def test_session_checkpoint_restores_after_store_recreation() -> None:
    persistence = _MemorySessionPersistence()
    key = "tenant\0shop\0buyer\0chat"
    first_store = AgentSessionStore(persistence=persistence)
    first_store.begin(key, "run-old")
    first_store.checkpoint_state(key, "run-old", {"phase": "quote", "confirmed": ["cinema"]})

    # A new store models a process restart; the checkpoint is restored lazily.
    restored_store = AgentSessionStore(persistence=persistence)
    restored = restored_store.get(key)
    assert restored.run_id == "run-old"
    assert restored.state == "RUNNING"
    assert restored.checkpoint["phase"] == "quote"

    restored_store.begin(key, "run-new")
    assert restored_store.is_interrupted(key, "run-old") is True


def test_session_persistence_expiry_and_malformed_records_fail_closed() -> None:
    persistence = _MemorySessionPersistence()
    key = "tenant\0shop\0buyer\0chat"
    persistence.records[key] = {
        "session_key": key,
        "run_id": "stale",
        "state": "RUNNING",
        "saved_at": time() - 60,
    }
    expired = AgentSessionStore(ttl_seconds=1, persistence=persistence).get(key)
    assert expired.state == "IDLE" and expired.run_id == ""

    persistence.records[key] = {"session_key": key, "state": "UNKNOWN", "revision": "bad", "saved_at": time()}
    malformed = AgentSessionStore(persistence=persistence).get(key)
    assert malformed.state == "IDLE" and malformed.revision == 0


def test_session_persistence_errors_do_not_break_local_runtime() -> None:
    key = "tenant\0shop\0buyer\0chat"
    save_failing = AgentSessionStore(persistence=_MemorySessionPersistence(fail_save=True))
    assert save_failing.begin(key, "run").state == "RUNNING"
    load_failing = AgentSessionStore(persistence=_MemorySessionPersistence(fail_load=True))
    assert load_failing.get(key).state == "IDLE"


@pytest.mark.asyncio
async def test_runtime_model_loop_tool_and_blocked_paths() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("show.list", handler=lambda _: ToolResult.success("listed"), phases=frozenset({"consultation"})))
    dispatcher = ToolDispatcher(registry)
    responses = iter([
        {"tool_calls": [{"name": "show.list", "arguments": {}}]},
        {"text": "已查到场次"},
    ])
    store = AgentSessionStore()
    runtime = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: next(responses)), tool_dispatcher=dispatcher, session_store=store)
    result = await runtime.run(request())
    assert result.status == "settled" and result.tool_calls[0]["result"]["status"] == "success"
    assert store.get(request().session_key).state == "SETTLED"
    blocked_store = AgentSessionStore()
    blocked = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: {"tool_calls": [{"name": "missing", "arguments": {}}]}), session_store=blocked_store)
    blocked_result = await blocked.run(request("run-blocked"))
    assert blocked_result.status == "blocked"
    assert blocked_store.get(request("run-blocked").session_key).state == "FAILED"


@pytest.mark.asyncio
async def test_runtime_passes_phase_tools_and_applies_mode_round_budget() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        "show.list",
        description="List shows",
        handler=lambda _: ToolResult.success("listed"),
        phases=frozenset({"consultation"}),
        schema={"type": "object"},
    ))
    registry.register(ToolDefinition(
        "order.change_price",
        description="Change order price",
        handler=lambda _: ToolResult.success("changed"),
        read_only=False,
        phases=frozenset({"consultation"}),
        schema={"type": "object"},
    ))
    seen_tools: list[list[dict[str, object]]] = []

    def model(_messages, tools):
        seen_tools.append(list(tools))
        return {"tool_calls": [{"name": "show.list", "arguments": {}}]}

    runtime = AgentRuntime(
        model_gateway=ModelGateway(model),
        tool_dispatcher=ToolDispatcher(registry),
    )
    hybrid = request("run-budget")
    hybrid.deadline_seconds = 60
    result = await runtime.run(hybrid)

    # Hybrid is capped at four model rounds even if the request supplies a
    # larger deadline.  Only read-only tools are exposed in this mode.
    assert result.finish_reason == "max_model_rounds"
    assert len(seen_tools) == 4
    assert all([item["name"] for item in tools] == ["show.list"] for tools in seen_tools)


@pytest.mark.asyncio
async def test_runtime_resets_dispatcher_between_runs_and_fails_closed() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("show.list", handler=lambda _: ToolResult.success("listed"), phases=frozenset({"consultation"})))
    dispatcher = ToolDispatcher(registry)
    responses = iter([
        {"tool_calls": [{"name": "show.list", "arguments": {}}]},
        {"text": "第一轮"},
        {"tool_calls": [{"name": "show.list", "arguments": {}}]},
        {"text": "第二轮"},
    ])
    runtime = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: next(responses)), tool_dispatcher=dispatcher)
    assert (await runtime.run(request("run-a"))).reply_text == "第一轮"
    assert (await runtime.run(request("run-b"))).reply_text == "第二轮"

    failing_store = AgentSessionStore()
    failing = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: (_ for _ in ()).throw(RuntimeError("provider"))), session_store=failing_store)
    result = await failing.run(request("run-fail"))
    assert result.status == "failed" and result.finish_reason == "runtime_error"
    assert failing_store.get(request("run-fail").session_key).state == "FAILED"


@pytest.mark.asyncio
async def test_delegate_terminal_status_without_text_is_preserved() -> None:
    async def delegate(_: AgentRequest) -> AgentResult:
        return AgentResult("waiting_buyer", finish_reason="needs_confirmation")

    store = AgentSessionStore()
    runtime = AgentRuntime(delegate=delegate, session_store=store)
    result = await runtime.run(request("run-wait"))
    assert result.status == "waiting_buyer" and result.reply_text is None
    assert store.get(request("run-wait").session_key).state == "WAITING_BUYER"


@pytest.mark.asyncio
async def test_runtime_emits_complete_native_lifecycle() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("show.list", handler=lambda _: ToolResult.success("listed"), phases=frozenset({"consultation"})))
    events: list[AgentEvent] = []
    bus = AgentEventBus()
    bus.subscribe(events.append)
    responses = iter([
        {"tool_calls": [{"name": "show.list", "arguments": {"query": "today"}}]},
        {"text": "query complete"},
    ])
    runtime = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: next(responses)), tool_dispatcher=ToolDispatcher(registry), event_bus=bus)
    result = await runtime.run(request("run-events"))
    assert result.status == "settled"
    names = [event.name for event in events]
    expected = ["agent_start", "context_restore", "turn_start", "model_response_start", "model_response_end", "tool_call_start", "tool_call_end", "turn_start", "model_response_start", "model_response_end", "reply_validation", "reply_sent", "agent_settled"]
    position = 0
    for name in expected:
        position = names.index(name, position) + 1
    tool_start = next(event for event in events if event.name == "tool_call_start")
    assert tool_start.payload["parameter_hash"] and tool_start.turn_index == 0
    assert [event.turn_index for event in events if event.name == "turn_start"] == [0, 1]


@pytest.mark.asyncio
async def test_runtime_emits_retry_and_suppression_events() -> None:
    registry = ToolRegistry()
    attempts = 0

    async def failing_tool(_: object) -> ToolResult:
        nonlocal attempts
        attempts += 1
        return ToolResult.error("temporary", retry={"allowed": True, "max_attempts": 1})

    registry.register(ToolDefinition("show.list", handler=failing_tool, retry_allowed=True, phases=frozenset({"consultation"})))
    events: list[AgentEvent] = []
    bus = AgentEventBus()
    bus.subscribe(events.append)
    responses = iter([
        {"tool_calls": [{"name": "show.list", "arguments": {}}]},
        {"text": "Bearer secret-token"},
    ])
    snapshots: list[dict[str, object]] = []

    class Persistence:
        def load(self, _session_key: str):
            return None

        def save(self, _session_key: str, snapshot: dict[str, object]) -> None:
            snapshots.append(dict(snapshot))

    runtime = AgentRuntime(model_gateway=ModelGateway(lambda _messages, _tools: next(responses)), tool_dispatcher=ToolDispatcher(registry), event_bus=bus, session_store=AgentSessionStore(persistence=Persistence()))
    result = await runtime.run(request("run-events-retry"))
    assert result.status == "failed" and result.finish_reason == "retry_exhausted"
    assert attempts == 2
    names = [event.name for event in events]
    assert "tool_retry" in names and "tool_blocked" in names and "agent_failed" in names
    assert "reply_validation" not in names
    states = [snapshot["state"] for snapshot in snapshots]
    assert "WAITING_TOOL" in states and "RECOVERING" in states and states[-1] == "FAILED"


@pytest.mark.asyncio
async def test_runtime_blocks_oversized_tool_batch_before_execution() -> None:
    registry = ToolRegistry()
    executions = 0

    async def handler(_: object) -> ToolResult:
        nonlocal executions
        executions += 1
        return ToolResult.success("ok")

    registry.register(ToolDefinition("show.list", handler=handler, phases=frozenset({"consultation"})))
    tool_calls = [{"name": "show.list", "arguments": {"index": index}} for index in range(7)]
    runtime = AgentRuntime(
        model_gateway=ModelGateway(lambda _messages, _tools: {"tool_calls": tool_calls}),
        tool_dispatcher=ToolDispatcher(registry),
    )
    result = await runtime.run(request("run-tool-budget"))
    assert result.status == "blocked" and result.finish_reason == "max_tool_calls"
    assert executions == 0


@pytest.mark.asyncio
async def test_runtime_stops_on_write_tool_failure_and_waits_for_buyer() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        "order.write",
        handler=lambda _: ToolResult.error("write failed"),
        read_only=False,
        phases=frozenset({"order"}),
    ))
    responses = iter([{"tool_calls": [{"name": "order.write", "arguments": {}}]}])
    runtime = AgentRuntime(
        model_gateway=ModelGateway(lambda _messages, _tools: next(responses)),
        tool_dispatcher=ToolDispatcher(registry),
    )
    scoped = request("run-write-fail")
    scoped.mode = "full"
    scoped.runtime_context["phase"] = "order"
    result = await runtime.run(scoped)
    assert result.status == "waiting_buyer" and result.finish_reason == "write_failure_not_retried"


@pytest.mark.asyncio
async def test_legacy_delegate_emits_turn_and_reply_events() -> None:
    events: list[AgentEvent] = []
    bus = AgentEventBus()

    async def delegate(_: AgentRequest) -> str:
        return "acknowledged"

    bus.subscribe(events.append)
    runtime = AgentRuntime(delegate=delegate, event_bus=bus)
    result = await runtime.run(request("run-legacy-events"))
    assert result.status == "settled"
    names = [event.name for event in events]
    assert names[:3] == ["agent_start", "turn_start", "model_response_start"]
    assert names[-3:] == ["reply_validation", "reply_sent", "agent_settled"]


def test_reply_validator_blocks_unproven_claims() -> None:
    validator = ReplyValidator()
    assert not validator.validate("出票成功，请查收").allowed
    assert validator.validate("可以帮您查询场次").allowed
