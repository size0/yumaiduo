from __future__ import annotations

import pytest

from app.agent.context import AgentContext
from app.agent.observations import Observation
from app.agent.registry import ToolDefinition, ToolRegistry
from app.agent.result import AgentResult, AgentStatus
from app.agent.runtime import AgentHarness


class FakeModel:
    def __init__(self, turns: list[dict[str, object]]) -> None:
        self.turns = iter(turns)
        self.calls: list[tuple[dict[str, object], list[dict[str, object]]]] = []

    async def complete(
        self, context: dict[str, object], tools: list[dict[str, object]], *, trace_id: str,
    ) -> dict[str, object]:
        self.calls.append((context, tools))
        return next(self.turns)


def test_observation_has_only_standard_fields() -> None:
    observation = Observation.success(
        "quote_ready",
        facts={"total_price_cents": 7600},
        candidates=[{"show_id": "show-1"}],
    )

    assert observation.as_dict() == {
        "ok": True,
        "code": "quote_ready",
        "facts": {"total_price_cents": 7600},
        "candidates": [{"show_id": "show-1"}],
        "missing_fields": [],
        "conflicts": [],
        "message": "",
    }


@pytest.mark.asyncio
async def test_read_only_registry_is_single_source_for_schema_and_execution() -> None:
    async def cinema_list(_arguments: dict[str, object]) -> Observation:
        return Observation.success("cinemas_ready", facts={"count": 1})

    registry = ToolRegistry(read_only=True)
    registry.register(ToolDefinition(
        name="cinema.list",
        description="List cinemas",
        input_schema={"type": "object", "properties": {}},
        handler=cinema_list,
    ))

    assert [schema["function"]["name"] for schema in registry.schemas()] == ["cinema.list"]
    result = await registry.execute("cinema.list", {})
    assert result.code == "cinemas_ready"

    with pytest.raises(ValueError, match="agent_write_tool_disabled"):
        registry.register(ToolDefinition(
            name="change_price",
            description="Change price",
            input_schema={"type": "object"},
            risk_level="write",
            handler=cinema_list,
        ))


def test_context_contains_identity_event_observations_and_tool_schemas() -> None:
    context = AgentContext(
        identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c"},
        current_event={"content": "两张多少钱"},
        recent_messages=[{"direction": "inbound", "content": "两张多少钱"}],
        observations=[Observation.success("recognized", facts={"movie": "测试电影"})],
        available_tools=[{"function": {"name": "quote.preview"}}],
    )

    payload = context.as_dict()
    assert payload["identity"]["buyer_id"] == "b"
    assert payload["current_event"]["content"] == "两张多少钱"
    assert payload["observations"][0]["code"] == "recognized"
    assert payload["available_tools"][0]["function"]["name"] == "quote.preview"


def test_replied_result_requires_non_empty_reply() -> None:
    with pytest.raises(ValueError, match="agent_reply_required"):
        AgentResult(status=AgentStatus.REPLIED, reply="", trace_id="trace-1")


@pytest.mark.asyncio
async def test_runtime_executes_read_tool_then_returns_reply() -> None:
    async def quote_preview(_arguments: dict[str, object]) -> Observation:
        return Observation.success("quote_ready", facts={"total_price_cents": 7600})

    registry = ToolRegistry(read_only=True)
    registry.register(ToolDefinition(
        name="quote.preview",
        description="Preview authoritative quote",
        input_schema={"type": "object", "properties": {}},
        handler=quote_preview,
    ))
    model = FakeModel([
        {"tool_call": {"name": "quote.preview", "arguments": {}}},
        {"final": "两张一共76元。"},
    ])
    harness = AgentHarness(model=model, registry=registry, max_rounds=3)
    context = AgentContext(
        identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c"},
        current_event={"content": "两张多少钱"},
    )

    result = await harness.run(context)
    assert result.status is AgentStatus.REPLIED
    assert result.reply == "两张一共76元。"
    assert [item["name"] for item in result.tool_calls] == ["quote.preview"]
    assert len(model.calls) == 2
