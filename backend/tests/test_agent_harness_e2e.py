from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.agent.context import AgentContext
from app.agent.observations import Observation
from app.agent.runtime import AgentHarness
from app.agent.result import AgentResult, AgentStatus
from app.agent.tools import build_read_only_registry
from app.chat_service import ConversationChatStore, CustomerServiceChatService
from app.config import Settings
from app.plugin_automation import RulesFirstDecisionEngine
from app.agent.validators import validate_quote_reply
from app.quote_record_store import QuoteRecordStore
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore
from app.models import MovieImageInfo, RealQuote
from app.wanda_direct_quote import WandaDirectQuoteService


class FakeRecognition:
    async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
        assert image_url.startswith("https://")
        return MovieImageInfo(
            city="泉州", cinema_name="泉州德化万达广场店", movie_name="欢迎来龙餐馆",
            date=date(2026, 9, 2), date_text="2026-09-02", showtime_start="14:40",
            hall_name="6号CINITY厅", selected_seats=[
                {"seat_number": "12排16座"}, {"seat_number": "12排17座"},
            ], selected_count_visible=2,
        )


class FakeQuote:
    async def quote(self, recognition: MovieImageInfo) -> RealQuote:
        assert len(recognition.selected_seats) == 2
        return RealQuote(
            quote_scope="exact_seats", seat_zone_type="CINITY", ticket_count=2,
            unit_quote_cents=3800, total_quote_cents=7600,
            member_unit_price_cents=3000, original_unit_price_cents=5000,
            price_source="realtime_regular_area", matched_cinema_name=recognition.cinema_name,
        )


class TraceModel:
    def __init__(self) -> None:
        self.index = 0

    async def complete(self, context, tools, *, trace_id):
        self.index += 1
        if self.index == 1:
            assert {item["function"]["name"] for item in tools} == {
                "recognize_screenshot", "cinema.list", "movie.resolve", "show.list",
                "show.detail", "seat.list", "quote.preview", "order.current", "conversation.current",
            }
            return {"tool_call": {"name": "recognize_screenshot", "arguments": {"image_url": "https://img.alicdn.com/ticket.jpg"}}}
        if self.index == 2:
            observation = context["observations"][-1]
            assert observation["code"] == "recognition_ready"
            return {"tool_call": {"name": "quote.preview", "arguments": observation["facts"]}}
        assert context["observations"][-1]["code"] == "quote_ready"
        return {"final": "这两个位置目前两张一共76元。"}


@pytest.mark.asyncio
async def test_real_read_only_registry_and_harness_complete_image_quote_trace() -> None:
    registry = build_read_only_registry(
        recognition_service=FakeRecognition(), quote_service=FakeQuote(),
    )
    result = await AgentHarness(model=TraceModel(), registry=registry, max_tool_calls=8).run(
        AgentContext(
            identity={"tenant_id": "107", "shop_id": "231", "buyer_id": "2977030784", "chat_id": "c"},
            current_event={"event_id": "e", "content": "两张多少钱", "image_urls": ["https://img.alicdn.com/ticket.jpg"]},
        )
    )
    assert result.status.value == "REPLIED"
    assert result.reply == "这两个位置目前两张一共76元。"
    assert [item["name"] for item in result.tool_calls] == ["recognize_screenshot", "quote.preview"]
    assert result.observations[-1].facts["total_price_cents"] == 7600


@pytest.mark.asyncio
async def test_existing_provider_client_shapes_are_adapted_without_raw_credentials() -> None:
    class RawClient:
        async def cinema_list(self, **kwargs):
            return {"data": {"list": [{"cinemaId": 1, "cinemaName": "德化万达"}]}, "cookie": "secret"}

        async def movie_list(self, **kwargs):
            return {"data": {"list": [{"movieId": 2, "movieName": "欢迎来龙餐馆"}], "token": "secret"}}

        async def show_list(self, **kwargs):
            return {"data": {"list": [{"showId": "show-1", "startTime": "14:40"}]}}

        async def show_detail(self, **kwargs):
            return {"data": {"showId": "show-1", "hall": "6号CINITY厅"}}

        async def seat_list(self, **kwargs):
            return {"data": {"list": [{"seatNo": "12排16座", "areaId": "regular"}]}}

    registry = build_read_only_registry(provider_client=RawClient())
    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c"}
    for name in ("cinema.list", "movie.resolve", "show.list", "show.detail", "seat.list"):
        observation = await registry.execute(name, {}, identity=identity)
        assert observation.ok is True
        assert observation.facts["count"] == 1
        assert "cookie" not in str(observation.as_dict())
        assert "token" not in str(observation.as_dict())


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("cinema.list", {}), ("movie.resolve", {}), ("show.list", {}),
        ("show.detail", {}), ("seat.list", {}), ("order.current", {}),
        ("conversation.current", {}),
    ],
)
@pytest.mark.asyncio
async def test_every_registered_read_tool_has_a_safe_observation(name, arguments) -> None:
    registry = build_read_only_registry()
    result = await registry.execute(name, arguments)
    assert isinstance(result, Observation)
    assert result.code
    assert set(result.as_dict()) == {"ok", "code", "facts", "candidates", "missing_fields", "conflicts", "message"}


def test_quote_validator_rejects_untrusted_or_mismatched_amount() -> None:
    quote = Observation.success("quote_ready", facts={"quantity": 2, "total_price_cents": 7600})
    assert validate_quote_reply("两张一共76元", [quote]) == (True, "quote_facts_match")
    assert validate_quote_reply("两张一共99元", [quote])[0] is False
    assert validate_quote_reply("每张76，两张152元", [quote])[0] is False
    assert validate_quote_reply("两张一共76元", []) == (False, "price_without_successful_quote")


@pytest.mark.asyncio
async def test_chat_service_feature_flag_uses_new_harness_and_preserves_identity() -> None:
    class FakeHarness:
        async def run(self, context: AgentContext, *, freshness_check=None):
            assert context.identity["tenant_id"] == "tenant-1"
            assert context.current_event["message_id"] == "message-1"
            return AgentResult(status=AgentStatus.REPLIED, reply="两张一共76元。", reason="quote_ready")

    service = CustomerServiceChatService(
        Settings(chat_api_key="test", chat_base_url="https://example.com/v1"),
        agent_harness=FakeHarness(), new_agent_harness_enabled=True,
    )
    context = {"tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1", "current_event": {"message_id": "message-1"}}
    assert await service.reply("两张多少钱", "chat-1", runtime_context=context) == "两张一共76元。"


@pytest.mark.asyncio
async def test_agent_context_uses_sorted_structured_platform_history_without_current_event() -> None:
    captured = {}

    class CaptureHarness:
        async def run(self, context, *, freshness_check=None):
            captured["context"] = context
            return AgentResult(status=AgentStatus.REPLIED, reply="收到。", reason="consultation")

    store = ConversationChatStore()
    service = CustomerServiceChatService(
        Settings(chat_api_key="test", chat_base_url="https://example.com/v1"),
        conversation_store=store, agent_harness=CaptureHarness(), new_agent_harness_enabled=True,
    )
    service.sync_platform_history("chat-1", [
        {"direction": "inbound", "messageId": "current", "sentAtMs": 3000, "messageType": 1, "content": "现在这条"},
        {"direction": "seller", "messageId": "human-1", "sentAtMs": 2000, "messageType": 1, "content": "人工回复"},
        {"direction": "inbound", "messageId": "image-1", "sentAtMs": 1000, "messageType": 2, "imageUrls": ["https://img.alicdn.com/a.jpg"]},
    ], current_message_id="current")
    await service.reply("现在这条", "chat-1", runtime_context={
        "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "chat-1",
        "current_event": {"message_id": "current", "content": "现在这条"},
    })
    history = captured["context"].recent_messages
    assert [item["message_id"] for item in history] == ["image-1", "human-1"]
    assert history[0]["image_urls"] == ["https://img.alicdn.com/a.jpg"]
    assert history[1]["sender_type"] == "human_seller"


@pytest.mark.asyncio
async def test_new_harness_event_returns_send_message_action_for_rules_outbox() -> None:
    class FakeChat:
        async def reply(self, text, conversation_id, runtime_context=None):
            assert runtime_context["tenant_id"] == "tenant-1"
            return "这两个位置目前两张一共76元。"

        def sync_platform_history(self, *args, **kwargs):
            return None

    engine = RulesFirstDecisionEngine(
        recognition_service=object(), quote_service=object(), chat_service=FakeChat(),
        agent_harness=object(), new_agent_harness_enabled=True,
    )
    decision = await engine.process_event({
        "envelope": {"id": "event-1", "tenantId": "tenant-1", "event": "im.message.received", "payload": {"messageType": "1"}},
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "recent_messages": [{"id": "message-1", "direction": "inbound", "messageType": "1", "content": "两张多少钱"}],
    })
    actions = decision["decision"]["actions"]
    assert len(actions) == 1
    assert actions[0]["type"] == "send_message"
    assert actions[0]["text"] == "这两个位置目前两张一共76元。"
    assert actions[0]["agent_harness"] is True


@pytest.mark.parametrize(
    ("scenario", "reply"),
    [
        ("B_missing_seat", "请补充希望的排数或座位，我再为您查询。"),
        ("C_cinema_candidates", "请确认您要查询哪一家影院。"),
        ("D_show_candidates", "请确认具体的场次时间。"),
        ("E_quote_provider_failure", "这个场次暂时没有查到可用价格，我需要人工确认。"),
        ("F_general_consultation", "IMAX和CINITY都不错，主要看您更重视画面沉浸感还是厅内音效。"),
    ],
)
@pytest.mark.asyncio
async def test_read_only_acceptance_scenarios_finish_without_unverified_price(scenario, reply) -> None:
    class FinalModel:
        async def complete(self, context, tools, *, trace_id):
            return {"final": reply}

    result = await AgentHarness(model=FinalModel(), registry=build_read_only_registry()).run(
        AgentContext(
            identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": scenario},
            current_event={"content": scenario},
        )
    )
    assert result.status is AgentStatus.REPLIED
    assert result.reply == reply


@pytest.mark.asyncio
async def test_wanda_self_real_quote_input_resolves_cinema_without_liangpiao_seat_mapping(tmp_path) -> None:
    import sqlite3

    cache = tmp_path / "cinema.sqlite"
    with sqlite3.connect(cache) as connection:
        connection.execute("create table cinemas (cinema_id text, city_name text, cinema_name text, address text)")
        connection.execute("insert into cinemas values (?, ?, ?, ?)", ("6669", "泉州", "泉州德化万达广场店", "福建省泉州市德化县"))
        connection.commit()
    service = WandaDirectQuoteService(Settings(wanda_cinema_cache_path=str(cache)))
    recognition = MovieImageInfo(
        city="泉州", cinema_name="泉州德化万达广场店", movie_name="欢迎来龙餐馆",
        date=date(2026, 9, 2), date_text="2026-09-02", showtime_start="14:40",
        hall_name="6号CINITY厅", selected_seats=[{"seat_number": "12排16座"}, {"seat_number": "12排17座"}],
        selected_count_visible=2, seat_matched=False,
    )
    resolved = await service._resolve_cinema(service._settings_provider(), recognition)
    await service.aclose()
    assert resolved["cinema_id"] == "6669"
    assert recognition.seat_matched is False
    assert recognition.selected_seats[0].seat_number == "12排16座"


@pytest.mark.asyncio
async def test_order_current_uses_server_bound_identity_not_model_order_argument() -> None:
    captured = {}

    async def current_order(identity):
        captured.update(identity)
        return {"data": {"orderId": "server-bound-order", "status": "pending"}}

    registry = build_read_only_registry(current_order_provider=current_order)
    observation = await registry.execute(
        "order.current", {"order_no": "forged-order"},
        identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"},
    )
    assert observation.ok is True
    assert observation.candidates[0]["orderId"] == "server-bound-order"
    assert captured["tenant_id"] == "t"
    assert "order_no" not in captured


@pytest.mark.asyncio
async def test_quote_preview_writes_a_separate_identity_bound_audit_record(tmp_path) -> None:
    class PlainProtector:
        def protect(self, value: str) -> str:
            return value

        def unprotect(self, value: str) -> str:
            return value

    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    registry = build_read_only_registry(quote_service=FakeQuote(), quote_recorder=store.save)
    observation = await registry.execute(
        "quote.preview",
        {"cinema_name": "泉州德化万达广场店", "movie_name": "欢迎来龙餐馆", "date": "2026-09-02", "showtime_start": "14:40", "selected_seats": [{"seat_number": "12排16座"}, {"seat_number": "12排17座"}]},
        identity={"tenant_id": "107", "shop_id": "231", "buyer_id": "2977030784", "chat_id": "chat-1", "event_id": "event-1", "trace_id": "trace-1"},
    )
    records = store.list("107")
    assert observation.code == "quote_ready"
    assert len(records) == 1
    assert records[0]["quote_id"].startswith("agent-quote-")
    assert records[0]["quote_id"] != "event-1"
    assert records[0]["event_id"] == "event-1"
    assert records[0]["total_quote_cents"] == 7600
    assert records[0]["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_new_message_cancels_stale_agent_reply_before_final() -> None:
    release = asyncio.Event()

    class SlowModel:
        async def complete(self, context, tools, *, trace_id):
            await release.wait()
            return {"final": "旧截图报价"}

    conversations = ConversationChatStore()
    revision = conversations.begin_turn("chat-1", "message-1")
    harness = AgentHarness(model=SlowModel(), registry=build_read_only_registry())
    task = asyncio.create_task(harness.run(
        AgentContext(
            identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "chat-1"},
            current_event={"event_id": "event-1", "message_id": "message-1"},
            conversation_revision=revision,
        ),
        freshness_check=lambda: conversations.is_current("chat-1", revision),
    ))
    await asyncio.sleep(0)
    conversations.begin_turn("chat-1", "message-2")
    release.set()
    result = await task
    assert result.status is AgentStatus.RETRY
    assert result.reason == "cancelled_stale"


@pytest.mark.asyncio
async def test_agent_result_reaches_durable_outbox_with_bound_identity(tmp_path) -> None:
    class PlainProtector:
        def protect(self, value: str) -> str:
            return value

        def unprotect(self, value: str) -> str:
            return value

    class Engine:
        async def process_event(self, _body):
            return {"decision": {"mode": "auto", "reply_route": "agent", "reason": "new_agent_harness_reply_ready", "actions": [{
                "id": "event-1:agent-harness-reply", "type": "send_message", "text": "这两个位置目前两张一共76元。",
                "trace_id": "trace-1", "preserve_on_new_buyer_message": True,
            }]}}

    store = RulesFirstStore(tmp_path / "events.sqlite3", protector=PlainProtector())
    states = SqliteTransactionStateStore(tmp_path / "events.sqlite3", protector=PlainProtector())
    runtime = RulesFirstRuntime(store, Engine(), RuleStateCoordinator(states), states)
    body = {"envelope": {"id": "event-1", "tenantId": "tenant-1", "event": "im.message.received", "payload": {}}, "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"}, "recent_messages": []}
    runtime.accept(body)
    assert await runtime.drain_once() is True
    command = runtime.claim_commands()[0]
    assert command["command_type"] == "send_message"
    assert command["tenant_id"] == "tenant-1"
    assert command["event_id"] == "event-1"
    assert command["action"]["text"] == "这两个位置目前两张一共76元。"
    assert command["action"]["trace_id"] == "trace-1"
    assert command["action"]["preserve_on_new_buyer_message"] is True
    assert command["context"]["session"]["accountUnb"] == "shop-1"
    assert command["context"]["session"]["peerUnb"] == "buyer-1"
    assert command["context"]["session"]["chatId"] == "chat-1"


def test_read_only_registry_does_not_expose_transaction_tools() -> None:
    registry = build_read_only_registry()
    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "create_liangpiao_order" not in names
    assert "change_price" not in names
    assert "send_ticket" not in names
