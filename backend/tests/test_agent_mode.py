from __future__ import annotations

from dataclasses import dataclass
import json

import httpx
import pytest

from app.automation_mode import normalize_automation_mode
from app.cinema_routing import CinemaRouteResolver
from app.errors import RecognitionError
from app.chat_service import CustomerServiceChatService
from app.config import Settings
from fastapi.testclient import TestClient

from app.main import _resolve_wanda_seat_target
from app.models import (
    CinemaCandidate,
    MovieCandidate,
    MovieImageInfo,
    ProviderPriceOption,
    RealQuote,
    SelectedSeat,
    ShowCandidate,
)
from app.pending_cinema_candidate_store import PendingCinemaCandidateStore
from app.recognition_snapshot_store import RecognitionSnapshotStore
from app.plugin_automation import (
    PluginAutomation,
    _build_image_quote_targets,
    _exact_quote_actions,
    _recognition_ready_for_preflight,
)
from app.rules_first_store import RulesFirstStore


class _UnusedRecognitionService:
    async def recognize(self, *_args, **_kwargs):
        raise AssertionError("recognition should not run")


def test_automation_mode_accepts_the_four_supported_modes() -> None:
    assert [normalize_automation_mode(value) for value in ("rules", "hybrid", "agent", "full")] == [
        "rules", "hybrid", "agent", "full",
    ]


def test_exact_quote_uses_two_buyer_messages_with_price_alone_in_second() -> None:
    recognition = MovieImageInfo(
        cinema_name="上海寰映影城（大融城激光IMAX店）", movie_name="奥德赛",
        date_text="后天9月3日", showtime_start="09:30", hall_name="1号激光IMAX厅",
        selected_seats=[
            SelectedSeat(seat_number="6排12座"), SelectedSeat(seat_number="6排11座"),
        ], selected_count_visible=2,
    )
    quote = RealQuote(
        quote_scope="exact_seats", seat_zone_type="优选区", unit_quote_cents=12110,
        total_quote_cents=24220, ticket_count=2, matched_cinema_name=recognition.cinema_name,
        matched_movie_name=recognition.movie_name, matched_showtime_start="09:30",
        matched_hall_name=recognition.hall_name,
        seat_quotes=[
            {
                "seat_number": "6排12座", "seat_zone_type": "优选区",
                "original_price_cents": 13700, "unit_quote_cents": 12110,
            },
            {
                "seat_number": "6排11座", "seat_zone_type": "优选区",
                "original_price_cents": 13700, "unit_quote_cents": 12110,
            },
        ],
    )

    actions = _exact_quote_actions(
        {"event_id": "event-1"}, recognition, quote,
    )

    assert len(actions) == 2
    assert actions[0]["text"] == (
        "影院：上海寰映影城（大融城激光IMAX店）\n"
        "影片：《奥德赛》\n"
        "时间：后天9月3日 09:30\n"
        "影厅：1号激光IMAX厅\n"
        "已选座位：6排11座、6排12座\n"
        "张数：2张"
    )
    assert actions[1]["text"] == (
        "单价：121.10元/张\n"
        "合计：242.20元\n"
        "请直接提交2张订单，拍下后先不要付款，我这边改价。"
    )


def test_automation_mode_rejects_unknown_values() -> None:
    try:
        normalize_automation_mode("keyword_magic")
    except ValueError as error:
        assert str(error) == "automation_mode_invalid"
    else:
        raise AssertionError("unknown automation mode must be rejected")


@dataclass
class _Chat:
    calls: int = 0
    last_runtime_context: dict[str, object] | None = None

    def sync_platform_history(self, *_args, **_kwargs) -> None:
        return None

    async def reply(
        self, _text: str, _conversation_id: str,
        runtime_context: dict[str, object] | None = None,
    ) -> str:
        self.calls += 1
        self.last_runtime_context = runtime_context
        return "Agent 回复"


class _Recognition:
    async def recognize(self, *_args, **_kwargs) -> MovieImageInfo:
        return MovieImageInfo()


class _Quote:
    async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
        raise AssertionError("rules-mode text must not quote")


@pytest.mark.asyncio
async def test_wplus_target_maps_liangpiao_cinema_id_to_wanda_namespace() -> None:
    request = MovieImageInfo(
        city="沈阳", cinema_id=8258,
        cinema_name="辽宁省科技馆（万达影城IMAXGT双激光影厅）",
        movie_name="奥德赛", date="2026-09-01", showtime_start="14:10",
        hall_name="IMAXGT激光影厅-全国最大IMAX银幕", show_id="show-1",
    )

    class Route:
        route = "WANDA_SELF"
        recognition = request
        wanda_cinema_id = "7115"

    class Resolver:
        async def resolve(self, value: MovieImageInfo) -> Route:
            assert value.cinema_id == 8258
            return Route()

    mapped_request, wanda_cinema_id = await _resolve_wanda_seat_target(
        request, route_resolver=Resolver(),
    )

    assert mapped_request.cinema_id == 8258
    assert wanda_cinema_id == "7115"


def _message_body(*, image: bool = False) -> dict[str, object]:
    payload = {
        "messageType": 2 if image else 1,
        "remoteMessageId": "buyer-message",
        "content": "" if image else "怎么买？",
    }
    if image:
        payload["imageUrls"] = ["https://img.alicdn.com/seat.png"]
    message = {
        "direction": "inbound", "messageType": 2 if image else 1,
        "messageId": "buyer-message", "content": "" if image else "怎么买？",
        "imageUrls": ["https://img.alicdn.com/seat.png"] if image else [],
        "sentAtMs": 1787579999000,
    }
    return {
        "envelope": {
            "id": "mode-event", "tenantId": "tenant-1", "event": "im.message.received",
            "timestamp": 1787580000000,
            "payload": payload,
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": None,
        "recent_messages": [message],
    }


@pytest.mark.asyncio
async def test_rules_mode_skips_agent_for_the_next_buyer_message() -> None:
    chat = _Chat()
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=chat,
        automation_mode_provider=lambda _identity: "rules",
    )

    result = await automation.process_event(_message_body())

    assert chat.calls == 0
    assert result["decision"]["reason"] == "generic_ai_reply_disabled"
    assert result["decision"]["actions"] == []


@pytest.mark.asyncio
async def test_hybrid_mode_sends_the_buyer_message_to_agent() -> None:
    chat = _Chat()
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=chat,
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(_message_body())

    assert chat.calls == 1
    assert result["decision"]["reason"] == "automatic_reply_ready"


@pytest.mark.asyncio
async def test_agent_quote_tool_persists_a_scoped_quote_record() -> None:
    recognition = MovieImageInfo(
        city="深圳", cinema_name="南山万达", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        base_unit_cents=4_600, unit_quote_cents=4_900, total_quote_cents=4_900,
        ticket_count=1, matched_city_name="深圳", matched_cinema_name="南山万达",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class _QuoteResult:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            return quote

    records: list[dict[str, object]] = []
    automation = PluginAutomation(
        _Recognition(), _QuoteResult(), chat_service=_Chat(),
        quote_recorder=lambda value: records.append(dict(value)),
    )
    result = await automation._execute_agent_tool(
        "get_authoritative_quote", recognition.model_dump(mode="json"),
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"},
        envelope={"id": "e", "timestamp": 1787580000000, "payload": {}},
    )

    assert result["ok"] is True
    assert len(records) == 1
    assert records[0]["source"] == "agent_tool_quote"
    assert str(records[0]["record_id"]).startswith("e:agent:")


@pytest.mark.asyncio
async def test_canonical_quote_preflight_current_uses_the_same_guarded_quote_path() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, match_level="EXACT",
        city="深圳", cinema_name="南山万达", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        show_id="show-1", selected_seats=[SelectedSeat(seat_number="5排5座")],
        selected_count_visible=1,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
        matched_city_name="深圳", matched_cinema_name="南山万达",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class QuoteResult:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.show_id == "show-1"
            return quote

    automation = PluginAutomation(_Recognition(), QuoteResult(), chat_service=_Chat())
    result = await automation._execute_agent_tool(
        "quote.preflight_current", recognition.model_dump(mode="json"),
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"},
    )
    assert result["ok"] is True
    assert result["quote"]["total_quote_cents"] == 4_900


@pytest.mark.asyncio
async def test_current_order_state_rejects_arbitrary_order_arguments() -> None:
    automation = PluginAutomation(_Recognition(), _Quote(), chat_service=_Chat())
    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"}
    rejected = await automation._execute_agent_tool(
        "get_order_state", {"orderNo": "other-buyers-order"}, identity,
        order={"orderId": "current-order", "orderStatus": "paid"},
    )
    assert rejected == {"ok": False, "error": "order_state_arguments_invalid"}
    allowed = await automation._execute_agent_tool(
        "get_order_state", {}, identity,
        order={"orderId": "current-order", "orderStatus": "paid"},
    )
    assert allowed["ok"] is True
    assert allowed["order"]["orderId"] == "current-order"


@pytest.mark.asyncio
async def test_agent_quote_uses_durable_snapshot_instead_of_model_rewritten_facts(tmp_path) -> None:
    original = MovieImageInfo(
        is_seat_selection=True, match_level="EXACT",
        city="深圳", cinema_name="南山万达", movie_name="原影片",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        show_id="show-original",
        selected_seats=[SelectedSeat(seat_number="5排6座")],
        selected_count_visible=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, *_args, **_kwargs) -> MovieImageInfo:
            return original

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.movie_name == "原影片"
            assert request.show_id == "show-original"
            assert [seat.seat_number for seat in request.selected_seats] == ["5排6座"]
            return RealQuote(
                quote_scope="exact_seats", seat_zone_type="selected", seat_type="regular",
                base_unit_cents=4_600, unit_quote_cents=4_900,
                total_quote_cents=4_900, ticket_count=1,
                matched_city_name="深圳", matched_cinema_name="南山万达",
                matched_movie_name="原影片", matched_showtime_start="19:30",
            )

    automation = PluginAutomation(
        RecognitionTool(), QuoteTool(),
        recognition_snapshot_store=RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3"),
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-1",
    }
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"id": "event-1", "payload": {}},
    )
    target = recognized["quote_targets"][0]

    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote",
        {
            "snapshot_id": target["snapshot_id"],
            "snapshot_revision": target["snapshot_revision"],
            "target_id": target["target_id"],
            "movie_name": "篡改影片", "show_id": "show-tampered",
            "selected_seats": [{"seat_number": "99排99座"}],
        },
        identity, envelope={"id": "event-1", "payload": {}},
    )

    assert quoted["ok"] is True, quoted
    assert quoted["recognition"]["movie_name"] == "原影片"
    assert quoted["recognition"]["show_id"] == "show-original"


@pytest.mark.asyncio
async def test_recognition_snapshot_hashes_an_oversized_platform_event_id(tmp_path) -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, match_level="EXACT", cinema_name="南山万达",
        movie_name="原影片", date_text="今天", showtime_start="19:30",
        show_id="show-original",
        selected_seats=[SelectedSeat(seat_number="5排6座")],
        selected_count_visible=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, *_args, **_kwargs) -> MovieImageInfo:
            return recognition

    automation = PluginAutomation(
        RecognitionTool(), _Quote(),
        recognition_snapshot_store=RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3"),
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-" + ("x" * 194),
    }

    result = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"id": identity["event_id"], "payload": {}},
    )

    assert result["ok"] is True
    assert result["quote_targets"][0]["snapshot_id"].startswith("rs-")


@pytest.mark.asyncio
async def test_snapshot_quote_rejects_show_only_image_without_selected_seats(tmp_path) -> None:
    show_only = MovieImageInfo(
        is_seat_selection=False, match_level="EXACT", cinema_name="南山万达",
        movie_name="原影片", date_text="今天", showtime_start="19:30",
        show_id="show-original",
    )

    class RecognitionTool:
        async def recognize_from_url(self, *_args, **_kwargs) -> MovieImageInfo:
            return show_only

    class QuoteMustNotRun:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("show-only snapshot must not preflight")

    automation = PluginAutomation(
        RecognitionTool(), QuoteMustNotRun(),
        recognition_snapshot_store=RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3"),
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-1",
    }
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/show.png"},
        identity, envelope={"id": "event-1", "payload": {}},
    )
    target = recognized["quote_targets"][0]

    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote",
        {
            "snapshot_id": target["snapshot_id"],
            "snapshot_revision": target["snapshot_revision"],
            "target_id": target["target_id"],
        },
        identity, envelope={"id": "event-1", "payload": {}},
    )

    assert quoted == {"ok": False, "error": "recognition_not_ready_for_preflight"}


@pytest.mark.asyncio
async def test_agent_mode_image_only_recognizes_before_any_quote() -> None:
    chat = _Chat()
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=chat,
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(_message_body(image=True))

    assert chat.calls == 1
    assert result["decision"]["reason"] == "agent_image_reply_ready"
    assert result["decision"]["reply_route"] == "agent"
    assert chat.last_runtime_context["pending_image_url"] == "https://img.alicdn.com/seat.png"


@pytest.mark.asyncio
async def test_empty_liangpiao_seat_list_quotes_before_marker_confirmation() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_name="仓山万达", movie_name="奥德赛",
        date_text="2026-09-01", showtime_start="16:30", match_level="EXACT",
        selected_seats=[], selected_count_visible=0, seat_matched=False,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteService:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            return RealQuote(
                quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
                unit_quote_cents=4_900, pricing_source="万达官方实时区域参考价",
            )

    chat = _Chat()
    automation = PluginAutomation(
        RecognitionTool(), QuoteService(), chat_service=chat,
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(_message_body(image=True))

    assert chat.calls == 0
    assert result["decision"]["reason"] == "wplus_quote_ready"
    assert result["decision"]["actions"][0]["text"] == "49.00 一张"
    assert "截图是否已标记需要出票的位置" in result["decision"]["actions"][1]["text"]
    assert len(result["decision"]["actions"]) == 2


@pytest.mark.asyncio
async def test_agent_image_wplus_without_quote_returns_quote_failure() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_name="仓山万达", movie_name="奥德赛",
        date_text="2026-09-01", showtime_start="16:30", match_level="EXACT",
        selected_seats=[], selected_count_visible=0, seat_matched=False,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteService:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            raise RecognitionError("quote_unavailable", "quote unavailable", status_code=503)

    class ToolCallingChat(_Chat):
        async def reply(
            self, _text: str, _conversation_id: str,
            runtime_context: dict[str, object] | None = None,
        ) -> str:
            self.calls += 1
            assert runtime_context is not None
            executor = runtime_context["_agent_tool_executor"]
            recognized = await executor("recognize_screenshot", {
                "image_url": "https://img.alicdn.com/seat.png",
            })
            assert "quote_targets" in recognized, recognized
            return "Agent 回复"

    chat = ToolCallingChat()
    automation = PluginAutomation(
        RecognitionTool(), QuoteService(), chat_service=chat,
        automation_mode_provider=lambda _identity: "full",
    )

    result = await automation.process_event(_message_body(image=True))

    decision = result["decision"]
    assert decision["reason"] == "wplus_quote_unavailable"
    assert "当前场次暂未取得可核验价格" in decision["actions"][0]["text"]


@pytest.mark.asyncio
async def test_rules_image_empty_liangpiao_seat_list_quotes_before_marker_confirmation() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_name="仓山万达", movie_name="奥德赛",
        date_text="2026-09-01", showtime_start="16:30", match_level="EXACT",
        selected_seats=[], selected_count_visible=0,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, **_kwargs: object) -> MovieImageInfo:
            return recognition

    class QuoteService:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            return RealQuote(
                quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
                unit_quote_cents=4_900, pricing_source="万达官方实时区域参考价",
            )

    automation = PluginAutomation(
        RecognitionTool(), QuoteService(),
        automation_mode_provider=lambda _identity: "rules",
    )

    result = await automation.process_event(_message_body(image=True))

    assert result["decision"]["reason"] == "wplus_quote_marker_confirmation_required"
    assert result["decision"]["reply_route"] == "rule"
    assert result["decision"]["ai_called"] is False
    assert result["decision"]["actions"][0]["text"] == "49.00 一张"
    assert "截图是否已标记需要出票的位置" in result["decision"]["actions"][1]["text"]
    assert len(result["decision"]["actions"]) == 2


@pytest.mark.asyncio
async def test_hybrid_image_fixed_layer_recognizes_and_quotes_before_agent_reply() -> None:
    events: list[str] = []
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", match_level="EXACT",
        selected_seats=[SelectedSeat(seat_number="5排6座")], selected_count_visible=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            events.append("recognize")
            return recognition

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            events.append("quote")
            return RealQuote(
                quote_scope="exact_seats", seat_zone_type="selected",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    class ChatTool(_Chat):
        async def reply(
            self, _text: str, _conversation_id: str,
            runtime_context: dict[str, object] | None = None,
        ) -> str:
            events.append("agent")
            self.last_runtime_context = runtime_context
            return "第1张图5排6座，报价49元。"

    chat = ChatTool()
    automation = PluginAutomation(
        RecognitionTool(), QuoteTool(), chat_service=chat,
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(_message_body(image=True))

    assert events == ["recognize", "quote", "agent"]
    targets = chat.last_runtime_context["image_quote_targets"]
    assert targets[0]["quote"]["total_quote_cents"] == 4_900
    assert result["decision"]["actions"][0]["text"].endswith("已选座位：5排6座\n张数：1张")
    assert result["decision"]["actions"][1]["text"] == (
        "单价：49.00元/张\n合计：49.00元\n"
        "请直接提交1张订单，拍下后先不要付款，我这边改价。"
    )


@pytest.mark.asyncio
async def test_hybrid_image_agent_failure_uses_grounded_fixed_reply() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_name="南山万达影城", movie_name="测试电影",
        date_text="2026-09-01", showtime_start="19:30", match_level="EXACT",
        selected_seats=[SelectedSeat(seat_number="5排6座")], selected_count_visible=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            return RealQuote(
                quote_scope="exact_seats", seat_zone_type="selected",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    class FailingChat(_Chat):
        async def reply(self, *_args: object, **_kwargs: object) -> str:
            raise TimeoutError("model timeout")

    automation = PluginAutomation(
        RecognitionTool(), QuoteTool(), chat_service=FailingChat(),
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(_message_body(image=True))

    assert result["decision"]["reason"] == "exact_quote_split_messages_ready"
    assert len(result["decision"]["actions"]) == 2
    assert "49.00元/张" in result["decision"]["actions"][1]["text"]
    assert "请直接提交1张订单" in result["decision"]["actions"][1]["text"]


@pytest.mark.asyncio
async def test_agent_recognition_preserves_provider_candidates_but_hides_price_metadata() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_truncated=True,
        cinema_name="时代国际影城", movie_name="奥德赛", date_text="今天",
        showtime_start="15:10", selected_seats=[SelectedSeat(
            seat_number="8排7座", displayed_price=35, seat_no="8排7座", status="AVAILABLE",
        )], selected_count_visible=1, displayed_total=35,
        movie_id=88, no_match_reason="NOT_FOUND", cinema_hit_count=2,
        price_mismatch=False, seat_matched=True,
        candidate_movies=[MovieCandidate(movie_id=88, name="奥德赛", score=.98)],
        candidate_shows=[ShowCandidate(show_id="show-1", hall_name="1号厅", start_time="2026-09-01T15:10:00+08:00")],
        provider_prices=[ProviderPriceOption(
            ticket_mode="STANDARD", price_mode="FIXED", price_cents=3510, available=True,
        )],
        raw_results={
            "cinema": "时代国际影城", "film": "奥德赛", "priceAll": "35.00",
            "seat": [{"seatName": "8排7座", "seatPrice": "35.00"}],
        },
        final_results={
            "cinemaId": 1001, "movieId": 88, "priceAllFen": "3500",
            "priceMismatch": False,
            "seat": [{"seatNo": "8排7座", "seatPriceFen": "3500"}],
            "prices": [{
                "ticketMode": "STANDARD", "priceMode": "FIXED",
                "price": "3510", "maxPrice": "5200", "available": True,
            }],
        },
        raw_response={
            "code": 0,
            "data": {"rawResults": {"priceAll": "35.00"}},
        },
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    automation = PluginAutomation(RecognitionTool(), _Quote(), chat_service=_Chat())
    result = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"},
        envelope={"id": "e", "payload": {}},
    )

    public = result["recognition"]
    assert result["image_types"] == ["seat_selection"]
    assert public["movie_id"] == 88
    assert public["candidate_movies"][0]["name"] == "奥德赛"
    assert public["candidate_shows"][0]["show_id"] == "show-1"
    assert automation._get_pending_shows({
        "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
    })[0]["show_id"] == "show-1"
    assert "provider_prices" not in public
    assert "displayed_total" not in public
    assert "displayed_price" not in public["selected_seats"][0]
    assert "raw_response" not in public
    assert "priceAll" not in public["raw_results"]
    assert "seatPrice" not in public["raw_results"]["seat"][0]
    assert "priceAllFen" not in public["final_results"]
    assert "seatPriceFen" not in public["final_results"]["seat"][0]
    assert public["final_results"]["priceMismatch"] is False
    assert public["final_results"]["prices"] == [{
        "ticketMode": "STANDARD", "priceMode": "FIXED", "available": True,
    }]


def _wplus_marker_confirmation_body(message: str) -> dict[str, object]:
    previous_image = {
        "direction": "inbound", "messageType": 2, "messageId": "image-message",
        "imageUrls": ["https://img.alicdn.com/seat.png"], "sentAtMs": 1787579998000,
    }
    current_message = {
        "direction": "inbound", "messageType": 1, "messageId": "text-message",
        "content": message, "sentAtMs": 1787579999000,
    }
    return {
        "envelope": {
            "id": "text-event", "tenantId": "tenant-1", "event": "im.message.received",
            "timestamp": 1787580000000,
            "payload": {
                "messageType": 1, "remoteMessageId": "text-message", "content": message,
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": None,
        "recent_messages": [previous_image, current_message],
    }


def _seed_wplus_snapshot(store: RecognitionSnapshotStore) -> None:
    store.create(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        event_id="image-event", target_id="image-target-1",
        recognition=MovieImageInfo(
            is_seat_selection=True, cinema_name="仓山万达", movie_name="奥德赛",
            date_text="2026-09-01", showtime_start="16:30", match_level="EXACT",
            selected_seats=[], selected_count_visible=0,
        ),
    )


@pytest.mark.asyncio
async def test_delivered_quote_price_followup_uses_authoritative_record_without_agent(tmp_path) -> None:
    class FailingChat(_Chat):
        async def reply(self, *_args, **_kwargs) -> str:
            raise AssertionError("a delivered quote price follow-up must not call the Agent")

    quote_record = {
        "record_id": "quote-1", "tenant_id": "tenant-1", "shop_id": "shop-1",
        "buyer_id": "buyer-1", "chat_id": "chat-1", "item_id": "item-1",
        "status": "succeeded", "delivery_state": "delivered",
        "city": "福州", "cinema": "福州万达", "movie": "奥德赛",
        "quote_date": "2026-09-01", "date_text": "9月1日", "showtime_start": "16:30",
        "hall": "IMAX厅", "seat_display": "6排10座、6排11座",
        "selected_seats": [{"seat_no": "6排10座"}, {"seat_no": "6排11座"}],
        "quote_scope": "exact_seats", "seat_zone_type": "普通座", "seat_type": "regular",
        "unit_quote_cents": 3750, "total_quote_cents": 7500, "ticket_count": 2,
        "quote_expires_at": "2026-09-01T08:00:00+00:00",
    }
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=FailingChat(),
        quote_finder=lambda **_kwargs: quote_record,
    )

    result = await automation.process_event(_wplus_marker_confirmation_body("多少"))

    decision = result["decision"]
    assert decision["reason"] == "durable_quote_price_ready"
    assert decision["reply_route"] == "rule"
    assert decision["ai_called"] is False
    assert "75.00" in decision["actions"][0]["text"]
    assert "6排10座" in decision["actions"][0]["text"]


@pytest.mark.asyncio
async def test_wplus_followup_other_than_marker_answer_stays_in_marker_lane(tmp_path) -> None:
    snapshots = RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3")
    _seed_wplus_snapshot(snapshots)

    class FailingChat(_Chat):
        async def reply(self, *_args, **_kwargs) -> str:
            raise AssertionError("W+ marker lane must not call the Agent")

    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=FailingChat(),
        recognition_snapshot_store=snapshots,
    )

    result = await automation.process_event(_wplus_marker_confirmation_body("两张，什么价格"))

    decision = result["decision"]
    assert decision["reason"] == "wplus_marker_confirmation_required"
    assert decision["expected_action"] == "ASK_SEAT_MARK"
    assert decision["actions"][0]["rule_governed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["有标记", "标记了", "标记好了"])
async def test_wplus_marker_confirmation_asks_for_ticket_count(tmp_path, message: str) -> None:
    snapshots = RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3")
    _seed_wplus_snapshot(snapshots)
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=_Chat(),
        recognition_snapshot_store=snapshots,
    )

    result = await automation.process_event(_wplus_marker_confirmation_body(message))

    decision = result["decision"]
    assert decision["reason"] == "wplus_marker_confirmed_order_guidance"
    assert decision["buyer_intent"] == "CONFIRM_SEATS"
    assert decision["expected_action"] == "SUBMIT_ORDER"
    assert decision["actions"][0]["rule_governed"] is True
    assert decision["actions"][0]["text"] == "请问需要几张呢？"
    assert "直接提交订单" not in decision["actions"][0]["text"]


@pytest.mark.asyncio
async def test_wplus_marker_unknown_confirmation_uses_ai_with_conversation_context(tmp_path) -> None:
    snapshots = RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3")
    _seed_wplus_snapshot(snapshots)

    @dataclass
    class SemanticChat(_Chat):
        async def reply(
            self, _text: str, _conversation_id: str,
            runtime_context: dict[str, object] | None = None,
        ) -> str:
            self.calls += 1
            self.last_runtime_context = runtime_context
            assert runtime_context is not None
            assert runtime_context.get("wplus_marker_intent_classifier") is True
            marker_context = runtime_context.get("wplus_marker_context")
            assert isinstance(marker_context, dict)
            recent = marker_context.get("recent_conversation")
            assert isinstance(recent, list)
            assert recent[-1]["content"] == "嗯，按我刚才选的来就行"
            assert marker_context["pending_wplus_context"]["seat_selection_type"] == "W+"
            return '{"action":"reply","message":"{\\"classification\\":\\"confirmed\\"}"}'

    chat = SemanticChat()
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=chat,
        recognition_snapshot_store=snapshots,
    )

    result = await automation.process_event(
        _wplus_marker_confirmation_body("嗯，按我刚才选的来就行")
    )

    decision = result["decision"]
    assert chat.calls == 1
    assert decision["reason"] == "wplus_marker_confirmed_order_guidance"
    assert decision["expected_action"] == "SUBMIT_ORDER"
    assert decision["actions"][0]["text"] == "请问需要几张呢？"


@pytest.mark.asyncio
async def test_wplus_marker_confirmation_without_mark_requests_a_new_marked_image(tmp_path) -> None:
    snapshots = RecognitionSnapshotStore(tmp_path / "recognitions.sqlite3")
    _seed_wplus_snapshot(snapshots)
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=_Chat(),
        recognition_snapshot_store=snapshots,
    )

    result = await automation.process_event(_wplus_marker_confirmation_body("没有"))

    decision = result["decision"]
    assert decision["reason"] == "wplus_marker_required"
    assert decision["buyer_intent"] == "CONFIRM_SEATS"
    assert decision["expected_action"] == "ASK_SEAT_MARK"
    assert decision["actions"][0]["text"] == "请把需要出票的位置在座位图上圈好后，重新发送一张标记好的截图给我。"
    assert "不能直接选择" not in decision["actions"][0]["text"]


@pytest.mark.asyncio
async def test_empty_liangpiao_seat_list_quotes_in_direct_recognition_path() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, cinema_name="仓山万达", movie_name="奥德赛",
        date_text="2026-09-01", showtime_start="16:30", match_level="EXACT",
        selected_seats=[], selected_count_visible=0,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, **_kwargs: object) -> MovieImageInfo:
            return recognition

    class QuoteService:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            return RealQuote(
                quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
                unit_quote_cents=4_900, pricing_source="万达官方实时区域参考价",
            )

    automation = PluginAutomation(RecognitionTool(), QuoteService())
    recognized, quote, quote_error = await automation._recognize_and_quote(
        "https://img.alicdn.com/seat.png",
        identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "e"},
    )

    assert recognized == recognition
    assert quote is not None
    assert quote.unit_quote_cents == 4_900
    assert quote_error is None


def test_show_only_screenshot_never_reaches_preflight_without_selected_seats() -> None:
    show_only = MovieImageInfo(
        is_seat_selection=False, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", match_level="EXACT",
        selected_seats=[], selected_count_visible=0,
    )

    assert _recognition_ready_for_preflight(show_only) is False


@pytest.mark.asyncio
async def test_agent_exact_liangpiao_match_ignores_stale_candidate_arrays() -> None:
    exact = MovieImageInfo(
        is_seat_selection=True, cinema_truncated=True, match_level="EXACT",
        cinema_id=10027, city="上海", cinema_name="时代国际影城（金山店）",
        movie_id=88, movie_name="奥德赛", date_text="2026-09-01",
        showtime_start="15:10", hall_name="1号厅", show_id="show-1",
        selected_seats=[SelectedSeat(seat_number="8排7座")], selected_count_visible=1,
        candidate_cinemas=[
            CinemaCandidate(cinema_id=10027, name="时代国际影城（金山店）"),
            CinemaCandidate(cinema_id=10028, name="时代国际影城（其他店）"),
        ],
    )
    quote = RealQuote(
        quote_scope="exact_seats", seat_zone_type="STANDARD",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return exact

    class QuoteTool:
        async def quote(self, recognition: MovieImageInfo) -> RealQuote:
            assert recognition.cinema_id == 10027
            assert recognition.show_id == "show-1"
            return quote

    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "exact-e"}
    automation = PluginAutomation(RecognitionTool(), QuoteTool(), chat_service=_Chat())
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/exact.png"},
        identity, envelope={"id": "exact-e", "payload": {}},
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", recognized["recognition"], identity,
        envelope={"id": "exact-e", "timestamp": 1787580000000, "payload": {}},
    )

    assert recognized["recognition"]["candidate_cinemas"] == []
    assert quoted["ok"] is True
    assert quoted["quote"]["unit_quote_cents"] == 4_900


@pytest.mark.asyncio
async def test_agent_image_provider_failure_returns_safe_reply_instead_of_empty_actions() -> None:
    class FailingChat(_Chat):
        async def reply(self, _text: str, _conversation_id: str, runtime_context=None) -> str:
            raise RuntimeError("provider unavailable")

    automation = PluginAutomation(object(), object(), chat_service=FailingChat())
    result = await automation._agent_led_image_reply(
        {
            "id": "provider-failure-e",
            "payload": {"content": "这张多少钱"},
        },
        {
            "tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c",
            "event_id": "provider-failure-e",
        },
        [], None, ["https://img.alicdn.com/seat.png"],
        generic_ai_reply_enabled=True,
    )

    decision = result["decision"]
    assert decision["reason"] == "agent_led_image_reply_unavailable"
    assert decision["actions"]
    assert decision["actions"][0]["type"] == "send_message"
    assert "暂时不可用" in decision["actions"][0]["text"]


@pytest.mark.asyncio
async def test_wanda_quote_can_fallback_to_own_realtime_quote_when_liangpiao_seat_mapping_is_missing() -> None:
    recognition = MovieImageInfo(
        platform="万达", is_seat_selection=True, seat_matched=False,
        match_level="NONE", city="泉州", cinema_name="泉州德化万达广场店",
        movie_name="欢迎来龙餐馆", date_text="2026-09-02", showtime_start="14:40",
        hall_name="6号CINITY厅",
        selected_seats=[SelectedSeat(seat_number="12排16座"), SelectedSeat(seat_number="12排17座")],
        selected_count_visible=2,
    )
    quote = RealQuote(
        quote_scope="exact_seats", seat_zone_type="STANDARD",
        unit_quote_cents=4_900, total_quote_cents=9_800, ticket_count=2,
    )
    calls = 0

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            nonlocal calls
            calls += 1
            assert request.cinema_name == "泉州德化万达广场店"
            return quote

    resolver = CinemaRouteResolver(
        local_wanda_matcher=lambda _recognition: {"cinema_id": "wanda-1"},
    )

    @dataclass(frozen=True)
    class Snapshot:
        normalized: MovieImageInfo
        snapshot_id: str = "snapshot-1"
        revision: int = 1
        target_id: str = "image-target-1"

    class SnapshotStore:
        def get_current(self, **_kwargs: object) -> Snapshot:
            return Snapshot(recognition)

    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "wanda-fallback-e"}
    automation = PluginAutomation(
        object(), QuoteTool(), chat_service=_Chat(), cinema_route_resolver=resolver,
        recognition_snapshot_store=SnapshotStore(),
    )

    result = await automation._execute_agent_tool(
        "get_authoritative_quote",
        {**recognition.model_dump(mode="json"), "snapshot_id": "snapshot-1", "snapshot_revision": 1, "target_id": "image-target-1"},
        identity,
        envelope={"id": "wanda-fallback-e", "payload": {}},
    )

    assert result["ok"] is True
    assert result["quote"]["total_quote_cents"] == 9_800
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("recognition_update", "expected_error"), [
    ({"seat_matched": False}, "seat_mapping_required"),
    ({"price_mismatch": True}, "image_price_mismatch"),
])
async def test_agent_quote_rejects_unsafe_liangpiao_recognition_flags(
    recognition_update: dict[str, object], expected_error: str,
) -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True, city="上海", cinema_name="时代国际影城",
        movie_name="奥德赛", date_text="2026-09-01", showtime_start="15:10",
        hall_name="1号厅", selected_seats=[SelectedSeat(seat_number="8排7座")],
        selected_count_visible=1, match_level="EXACT",
    ).model_copy(update=recognition_update)

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteMustNotRun:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            raise AssertionError("unsafe Liangpiao flags must block quote")

    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "unsafe-e"}
    automation = PluginAutomation(RecognitionTool(), QuoteMustNotRun(), chat_service=_Chat())
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/unsafe.png"},
        identity, envelope={"id": "unsafe-e", "payload": {}},
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", recognized["recognition"], identity,
        envelope={"id": "unsafe-e", "payload": {}},
    )

    assert quoted == {"ok": False, "error": expected_error}


@pytest.mark.asyncio
async def test_agent_quote_rejects_liangpiao_expired_show_without_provider_call() -> None:
    expired = MovieImageInfo(
        cinema_name="时代国际影城", movie_name="奥德赛", date_text="2026-09-01",
        showtime_start="15:10", match_level="SHOW_EXPIRED",
        no_match_reason="SHOW_EXPIRED",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return expired

    class QuoteMustNotRun:
        async def quote(self, _recognition: MovieImageInfo) -> RealQuote:
            raise AssertionError("expired show must not reach quote provider")

    identity = {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "expired-e"}
    automation = PluginAutomation(RecognitionTool(), QuoteMustNotRun(), chat_service=_Chat())
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/expired.png"},
        identity, envelope={"id": "expired-e", "payload": {}},
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", recognized["recognition"], identity,
        envelope={"id": "expired-e", "payload": {}},
    )

    assert quoted == {"ok": False, "error": "showtime_expired"}


@pytest.mark.asyncio
async def test_agent_image_runtime_exposes_all_current_image_urls() -> None:
    chat = _Chat()
    automation = PluginAutomation(
        _Recognition(), _Quote(), chat_service=chat,
        automation_mode_provider=lambda _identity: "hybrid",
    )
    body = _message_body(image=True)
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]
    body["envelope"]["payload"]["imageUrls"] = urls
    body["recent_messages"][0]["imageUrls"] = urls

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "agent_image_reply_ready"
    assert chat.last_runtime_context["pending_image_url"] == urls[0]
    assert chat.last_runtime_context["pending_image_urls"] == urls


@pytest.mark.asyncio
async def test_hybrid_agent_can_recognize_human_seller_image_context() -> None:
    human_image_url = "https://img.alicdn.com/human-seat-map.jpg"
    calls: list[str] = []

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            calls.append(image_url)
            return MovieImageInfo(
                is_seat_selection=True, cinema_name="南山万达", movie_name="测试电影",
                date_text="今天", showtime_start="19:30", match_level="EXACT",
                selected_seats=[SelectedSeat(seat_number="5排6座")], selected_count_visible=1,
            )

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("human context recognition must not quote automatically")

    class ContextChat(_Chat):
        async def reply(
            self, _text: str, _conversation_id: str,
            runtime_context: dict[str, object] | None = None,
        ) -> str:
            assert runtime_context is not None
            assert human_image_url in runtime_context["human_seller_image_urls"]
            executor = runtime_context["_agent_tool_executor"]
            result = await executor("recognize_screenshot", {"image_url": human_image_url})
            assert result["ok"] is True
            assert result["context_only"] is True
            assert result["source"] == "human_seller_image"
            return "已看到人工客服发的座位图。"

    body = _message_body(image=True)
    body["recent_messages"] = [
        {"direction": "seller", "messageType": 2, "messageId": "human-seat-map",
         "imageUrls": [human_image_url], "content": human_image_url,
         "sentAtMs": 1787579990000},
        body["recent_messages"][0],
    ]
    automation = PluginAutomation(
        RecognitionTool(), QuoteTool(), chat_service=ContextChat(),
        automation_mode_provider=lambda _identity: "hybrid",
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "agent_image_reply_ready"
    assert calls == ["https://img.alicdn.com/seat.png", human_image_url]


@pytest.mark.asyncio
@pytest.mark.parametrize(("final_reply", "accepted"), [
    ("已核到南山万达19:30场次，当前报价49元。", True),
    ("已核到南山万达19:30场次，当前报价50元。", False),
])
async def test_agent_image_flow_recognizes_quotes_and_returns_one_final_reply(
    final_reply: str, accepted: bool,
) -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True,
        city="深圳", cinema_name="南山万达", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        selected_seats=[SelectedSeat(seat_number="5排6座")],
        selected_count_visible=1,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        base_unit_cents=4_600, unit_quote_cents=4_900, total_quote_cents=4_900,
        ticket_count=1, matched_city_name="深圳", matched_cinema_name="南山万达",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            assert image_url == "https://img.alicdn.com/seat.png"
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.cinema_name == "南山万达"
            return quote

    rounds = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            content = json.dumps({
                "action": "tool_call", "tool": "recognize_screenshot",
                "arguments": {"image_url": "https://img.alicdn.com/seat.png"},
            })
        elif rounds == 2:
            content = json.dumps({
                "action": "tool_call", "tool": "get_authoritative_quote",
                "arguments": recognition.model_dump(mode="json"),
            }, ensure_ascii=False)
        else:
            content = json.dumps({
                "action": "reply", "message": final_reply,
            }, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": content,
        }}]})

    records: list[dict[str, object]] = []
    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=lambda _identity: "hybrid",
            quote_recorder=lambda value: records.append(dict(value)),
        )
        result = await automation.process_event(_message_body(image=True))

    assert rounds == 3
    assert result["decision"]["reason"] == (
        "agent_image_reply_ready" if accepted else "generic_ai_transaction_claim_rejected"
    )
    if accepted:
        assert result["decision"]["actions"][0]["text"] == final_reply
    else:
        assert result["decision"]["actions"] == []
    assert len(records) == 1
    assert records[0]["source"] == "agent_tool_quote"
    assert records[0]["delivery_state"] == "pending"


@pytest.mark.asyncio
async def test_full_mode_uses_fixed_wplus_quote_flow_without_agent() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True,
        city="深圳", cinema_name="南山万达", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        selected_seats=[],
        selected_count_visible=0,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        base_unit_cents=4_600, unit_quote_cents=4_900, total_quote_cents=4_900,
        ticket_count=1, matched_city_name="深圳", matched_cinema_name="南山万达",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            return quote

    rounds = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            content = json.dumps({
                "action": "tool_call", "tool": "recognize_screenshot",
                "arguments": {},
            })
        elif rounds == 2:
            content = json.dumps({
                "action": "tool_call", "tool": "get_authoritative_quote",
                "arguments": recognition.model_dump(mode="json"),
            }, ensure_ascii=False)
        else:
            content = json.dumps({"action": "reply", "message": "已完成权威核价。"}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": content,
        }}]})

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")

    def keyword_only_mode_provider(*, tenant_id: str, shop_id: str, buyer_id: str, chat_id: str) -> str:
        assert tenant_id and shop_id and buyer_id and chat_id
        return "full"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=keyword_only_mode_provider,
        )
        result = await automation.process_event(_message_body(image=True))

    decision = result["decision"]
    assert rounds == 0
    assert decision["reason"] == "wplus_quote_ready"
    assert decision["reply_route"] == "rule"
    assert decision["ai_called"] is False
    assert decision["actions"][0]["text"] == "49.00 一张"
    assert "截图是否已标记需要出票的位置" in decision["actions"][1]["text"]


@pytest.mark.asyncio
async def test_full_mode_enforces_quote_after_agent_stops_at_recognition() -> None:
    recognition = MovieImageInfo(
        is_seat_selection=True,
        city="深圳", cinema_name="南山万达影城", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        match_level="EXACT", selected_seats=[], selected_count_visible=0,
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
        matched_city_name="深圳", matched_cinema_name="南山万达影城",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.cinema_name == "南山万达影城"
            return quote

    rounds = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            plan = {"action": "tool_call", "tool": "recognize_screenshot", "arguments": {}}
        else:
            plan = {"action": "reply", "message": "请把想要的排座发我。"}
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(plan, ensure_ascii=False),
        }}]})

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=lambda _identity: "full",
        )
        result = await automation.process_event(_message_body(image=True))

    decision = result["decision"]
    assert rounds == 0
    assert decision["reason"] == "wplus_quote_ready"
    assert decision["reply_route"] == "rule"
    assert decision["ai_called"] is False
    assert decision["actions"][0]["text"] == "49.00 一张"
    assert "截图是否已标记需要出票的位置" in decision["actions"][1]["text"]
    assert len(decision["actions"]) == 2


@pytest.mark.asyncio
async def test_agent_image_candidate_uses_global_cinema_tool_then_request_quote_tool() -> None:
    candidate = MovieImageInfo(
        city="深圳", cinema_name="万达影城", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        match_level="CANDIDATE", candidate_cinemas=[
            CinemaCandidate(cinema_id=1001, name="南山万达影城", city_name="深圳", score=.95),
        ],
    )
    exact = candidate.model_copy(update={
        "cinema_id": 1001, "cinema_name": "南山万达影城",
        "match_level": "EXACT", "candidate_cinemas": [],
    })
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
        matched_city_name="深圳", matched_cinema_name="南山万达影城",
        matched_movie_name="测试电影", matched_showtime_start="19:30",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return candidate

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.cinema_id == 1001
            return quote

    rounds = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            plan = {"action": "tool_call", "tool": "recognize_screenshot", "arguments": {"image_url": "https://img.alicdn.com/seat.png"}}
        elif rounds == 2:
            plan = {"action": "tool_call", "tool": "cinema.list", "arguments": {"city_name": "深圳", "keyword": "万达影城"}}
        elif rounds == 3:
            plan = {"action": "tool_call", "tool": "get_authoritative_quote", "arguments": exact.model_dump(mode="json")}
        else:
            plan = {"action": "reply", "message": "已确认南山万达影城，当前报价49元。"}
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(plan, ensure_ascii=False),
        }}]})

    global_calls: list[str] = []
    async def global_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        global_calls.append(name)
        assert name == "cinema.list"
        return {"ok": True, "cinemas": [{"cinema_id": 1001, "name": "南山万达影城"}]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client, tool_executor=global_executor)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=lambda _identity: "hybrid",
            quote_recorder=lambda _value: None,
        )
        result = await automation.process_event(_message_body(image=True))

    assert rounds == 4
    # Request-scoped executors are authoritative; the process-wide executor
    # must never be used as a tool_not_allowed fallback.
    assert global_calls == []
    assert result["decision"]["reason"] == "agent_image_reply_ready"
    assert result["decision"]["actions"][0]["text"] == "已确认南山万达影城，当前报价49元。"


@pytest.mark.asyncio
async def test_agent_image_uses_unique_authoritative_show_then_quotes() -> None:
    recognition = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start=None,
        missing_fields=["showtime_start"], confidence=.9, match_level="EXACT",
    )
    resolved = recognition.model_copy(update={
        "show_id": "show-1", "showtime_start": "19:30", "missing_fields": [],
    })
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
        matched_cinema_name="南山万达影城", matched_movie_name="测试电影",
        matched_showtime_start="19:30",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            assert request.show_id == "show-1"
            assert request.showtime_start == "19:30"
            return quote

    rounds = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            plan = {"action": "tool_call", "tool": "recognize_screenshot", "arguments": {"image_url": "https://img.alicdn.com/seat.png"}}
        elif rounds == 2:
            plan = {"action": "tool_call", "tool": "show.list", "arguments": {"cinemaId": 1001, "movieName": "测试电影", "date": "今天"}}
        elif rounds == 3:
            plan = {"action": "tool_call", "tool": "get_authoritative_quote", "arguments": resolved.model_dump(mode="json")}
        else:
            plan = {"action": "reply", "message": "已核到19:30场次，当前报价49元。"}
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(plan, ensure_ascii=False),
        }}]})

    global_calls: list[str] = []
    async def global_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        global_calls.append(name)
        assert name == "show.list"
        return {"ok": True, "shows": [{
            "show_id": "show-1", "showtime_start": "19:30", "hall_name": "1号厅",
        }]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client, tool_executor=global_executor)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=lambda _identity: "hybrid",
            quote_recorder=lambda _value: None,
        )
        result = await automation.process_event(_message_body(image=True))

    assert rounds == 4
    assert global_calls == []
    assert result["decision"]["reason"] == "agent_image_reply_ready"
    assert result["decision"]["actions"][0]["text"] == "已核到19:30场次，当前报价49元。"


@pytest.mark.asyncio
async def test_missing_show_recognition_survives_restart_for_the_next_buyer_turn() -> None:
    recognition = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start=None,
        missing_fields=["showtime_start"], confidence=.91, match_level="EXACT",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class PendingStore:
        def __init__(self) -> None:
            self.values: dict[str, MovieImageInfo] = {}
        def get(self, key: str) -> MovieImageInfo | None:
            return self.values.get(key)
        def save(self, key: str, value: MovieImageInfo) -> None:
            self.values[key] = value
        def delete(self, key: str) -> None:
            self.values.pop(key, None)

    store = PendingStore()
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-image",
    }
    first_engine = PluginAutomation(
        RecognitionTool(), _Quote(), pending_cinema_candidate_store=store,
    )
    await first_engine._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"payload": {"imageUrls": ["https://img.alicdn.com/seat.png"]}},
    )
    restarted_engine = PluginAutomation(
        RecognitionTool(), _Quote(), pending_cinema_candidate_store=store,
    )

    pending = restarted_engine._get_pending_candidate(identity)

    assert pending is not None
    assert pending.missing_fields == ["showtime_start"]
    assert pending.cinema_id == 1001
    assert pending.movie_name == "测试电影"


@pytest.mark.asyncio
async def test_multiple_authoritative_shows_require_a_later_buyer_confirmed_choice(
    tmp_path,
) -> None:
    recognition = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start=None,
        missing_fields=["showtime_start"], confidence=.9, match_level="EXACT",
    )
    quote_requests: list[MovieImageInfo] = []

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            quote_requests.append(request)
            return RealQuote(
                quote_scope="area_preview", seat_zone_type="W+",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    store = PendingCinemaCandidateStore(tmp_path / "pending.json")
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-image",
    }
    first_engine = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    recognized = await first_engine._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"payload": {"imageUrls": ["https://img.alicdn.com/seat.png"]}},
    )
    first_engine._observe_agent_tool_result(
        "show.list", {}, {"ok": True, "shows": [
            {"showId": "show-1", "startTime": "19:30", "hallName": "1号厅"},
            {"showId": "show-2", "startTime": "20:30", "hallName": "2号厅"},
        ]}, identity,
    )

    restarted_engine = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    blocked = await restarted_engine._execute_agent_tool(
        "get_authoritative_quote",
        {**recognized["recognition"], "show_id": "show-1", "showtime_start": "19:30"},
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "选20:30", "messageType": 1}},
    )
    guessed = await restarted_engine._execute_agent_tool(
        "resolve_showtime", {"show_id": "show-1", "buyer_message": "选19:30"},
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "选20:30", "messageType": 1}},
    )
    resolved = await restarted_engine._execute_agent_tool(
        "resolve_showtime", {"show_id": "show-2", "buyer_message": "选20:30"},
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "选20:30", "messageType": 1}},
    )
    quoted = await restarted_engine._execute_agent_tool(
        "get_authoritative_quote", resolved["recognition"],
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "选20:30", "messageType": 1}},
    )

    assert blocked == {"ok": False, "error": "showtime_choice_required"}
    assert guessed == {
        "ok": False, "error": "showtime_choice_buyer_confirmation_required",
    }
    assert resolved["recognition"]["show_id"] == "show-2"
    assert resolved["recognition"]["showtime_start"] == "20:30"
    assert quote_requests[0].show_id == "show-2"
    assert quoted["ok"] is True
    assert store.get_show_candidates(restarted_engine._candidate_key(identity)) == []


@pytest.mark.asyncio
async def test_agent_two_event_image_to_multi_show_choice_to_quote_flow(tmp_path) -> None:
    recognition = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start=None,
        missing_fields=["showtime_start"], confidence=.91, match_level="EXACT",
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", seat_type="wplus",
        unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
        matched_cinema_name="南山万达影城", matched_movie_name="测试电影",
        matched_showtime_start="20:30",
    )
    quote_requests: list[MovieImageInfo] = []

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognition

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            quote_requests.append(request)
            return quote

    plans = [
        {"action": "tool_call", "tool": "recognize_screenshot", "arguments": {"image_url": "https://img.alicdn.com/seat.png"}},
        {"action": "tool_call", "tool": "show.list", "arguments": {"cinemaId": 1001, "movieName": "测试电影", "date": "今天"}},
        {"action": "reply", "message": "查到19:30和20:30两个场次，请告诉我选择哪一场。"},
        {"action": "tool_call", "tool": "resolve_showtime", "arguments": {"show_id": "show-2", "buyer_message": "选20:30"}},
        {"action": "tool_call", "tool": "get_authoritative_quote", "arguments": {
            **recognition.model_dump(mode="json"), "show_id": "show-2",
            "showtime_start": "20:30", "hall_name": "2号厅", "missing_fields": [],
        }},
        {"action": "reply", "message": "已确认20:30场次，当前报价49元。"},
    ]
    provider_calls = 0
    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        plan = plans[provider_calls]
        provider_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(plan, ensure_ascii=False),
        }}]})

    global_calls: list[str] = []
    async def global_executor(name: str, _arguments: dict[str, object]) -> dict[str, object]:
        global_calls.append(name)
        assert name == "show.list"
        return {"ok": True, "shows": [
            {"showId": "show-1", "startTime": "19:30", "hallName": "1号厅"},
            {"showId": "show-2", "startTime": "20:30", "hallName": "2号厅"},
        ]}

    settings = Settings(chat_api_key="key", chat_base_url="https://example.test/v1")
    store = PendingCinemaCandidateStore(tmp_path / "pending-two-events.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chat = CustomerServiceChatService(settings, client=client, tool_executor=global_executor)
        automation = PluginAutomation(
            RecognitionTool(), QuoteTool(), chat_service=chat,
            automation_mode_provider=lambda _identity: "hybrid",
            pending_cinema_candidate_store=store,
            quote_recorder=lambda _value: None,
        )
        first_result = await automation.process_event(_message_body(image=True))
        choice_body = _message_body()
        choice_body["envelope"]["id"] = "mode-event-choice"
        choice_body["envelope"]["timestamp"] = 1787580001000
        choice_body["envelope"]["payload"]["remoteMessageId"] = "buyer-choice"
        choice_body["envelope"]["payload"]["content"] = "选20:30"
        choice_body["recent_messages"][0]["messageId"] = "buyer-choice"
        choice_body["recent_messages"][0]["content"] = "选20:30"
        choice_body["recent_messages"][0]["sentAtMs"] = 1787580000500
        second_result = await automation.process_event(choice_body)

    assert provider_calls == 6
    assert global_calls == []
    assert quote_requests[0].show_id == "show-2"
    assert quote_requests[0].showtime_start == "20:30"
    assert first_result["decision"]["actions"][0]["text"] == "查到19:30和20:30两个场次，请告诉我选择哪一场。"
    assert second_result["decision"]["actions"][0]["text"] == "已确认20:30场次，当前报价49元。"
    key = automation._candidate_key({
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1",
    })
    assert store.get(key) is None
    assert store.get_show_candidates(key) == []


@pytest.mark.asyncio
async def test_unique_show_candidate_cannot_be_substituted_before_quote() -> None:
    recognition = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start=None,
        missing_fields=["showtime_start"], match_level="EXACT",
    )

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("mismatched show must not reach quote provider")

    automation = PluginAutomation(_Recognition(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-image",
    }
    automation._remember_pending_candidate(identity, recognition)
    automation._observe_agent_tool_result(
        "show.list", {}, {"ok": True, "shows": [
            {"showId": "show-1", "startTime": "19:30"},
        ]}, identity,
    )

    result = await automation._execute_agent_tool(
        "get_authoritative_quote",
        {**recognition.model_dump(mode="json"), "show_id": "invented-show", "showtime_start": "20:30"},
        identity, envelope={"payload": {"messageType": 2}},
    )

    assert result == {"ok": False, "error": "showtime_candidate_mismatch"}


@pytest.mark.asyncio
async def test_resolve_showtime_without_live_candidates_fails_closed() -> None:
    automation = PluginAutomation(_Recognition(), _Quote())
    result = await automation._execute_agent_tool(
        "resolve_showtime", {"show_id": "show-expired", "buyer_message": "选这个"},
        {
            "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
            "chat_id": "chat-1", "event_id": "event-choice",
        },
        envelope={"payload": {"content": "选这个", "messageType": 1}},
    )

    assert result == {"ok": False, "error": "showtime_candidates_unavailable"}


@pytest.mark.asyncio
async def test_ambiguous_cinema_blocks_quote_until_a_later_buyer_confirmed_resolution() -> None:
    candidate = MovieImageInfo(
        city="深圳", cinema_name="万达影城", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", hall_name="1号厅",
        match_level="CANDIDATE", candidate_cinemas=[
            CinemaCandidate(cinema_id=1001, name="南山万达影城", city_name="深圳", score=.95),
            CinemaCandidate(cinema_id=1002, name="宝安万达影城", city_name="深圳", score=.82),
        ],
    )
    quote = RealQuote(
        quote_scope="area_preview", seat_zone_type="W+", unit_quote_cents=4_900,
        total_quote_cents=4_900, ticket_count=1,
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return candidate

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            return quote

    automation = PluginAutomation(RecognitionTool(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-image",
    }
    image_envelope = {"payload": {"content": "", "messageType": 2}}
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope=image_envelope,
    )
    blocked = await automation._execute_agent_tool(
        "get_authoritative_quote", candidate.model_dump(mode="json"),
        identity, envelope=image_envelope,
    )
    guessed = await automation._execute_agent_tool(
        "resolve_cinema", {"cinema_id": 1001, "buyer_message": "选第一个"},
        identity, envelope=image_envelope,
    )

    buyer_envelope = {"payload": {"content": "选第一个", "messageType": 1}}
    resolved = await automation._execute_agent_tool(
        "resolve_cinema", {"cinema_id": 1001, "buyer_message": "选第一个"},
        {**identity, "event_id": "event-choice"}, envelope=buyer_envelope,
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", resolved["recognition"],
        {**identity, "event_id": "event-choice"}, envelope=buyer_envelope,
    )

    assert recognized["ok"] is True
    assert blocked == {"ok": False, "error": "cinema_choice_required"}
    assert guessed == {"ok": False, "error": "cinema_choice_buyer_confirmation_required"}
    assert resolved["recognition"]["cinema_id"] == 1001
    assert quoted["ok"] is True


@pytest.mark.asyncio
async def test_agent_merges_multiple_images_once_before_authoritative_quote() -> None:
    first = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start="19:30",
        hall_name="1号厅", confidence=.96, match_level="EXACT",
    )
    second = first.model_copy(update={
        "selected_seats": [SelectedSeat(seat_number="5排6座", row_no=5, col_no=6)],
        "selected_count_visible": 1, "confidence": .91,
    })
    calls: list[str] = []
    quote_requests: list[MovieImageInfo] = []

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            calls.append(image_url)
            return first if image_url.endswith("/one.png") else second

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            quote_requests.append(request)
            return RealQuote(
                quote_scope="exact_seats", seat_zone_type="selected",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    automation = PluginAutomation(RecognitionTool(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-multi-image",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", recognized["recognition"], identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )

    assert calls == urls
    assert recognized["image_count"] == 2
    assert recognized["image_types"] == ["show_selection", "seat_selection"]
    assert recognized["conflict_fields"] == []
    assert recognized["recognition"]["selected_seats"][0]["seat_number"] == "5排6座"
    assert quote_requests[0].selected_seats[0].seat_number == "5排6座"
    assert quoted["ok"] is True


def test_two_seat_screenshots_create_two_independent_quote_targets() -> None:
    base = MovieImageInfo(
        is_seat_selection=True, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", match_level="EXACT",
    )
    first = base.model_copy(update={
        "selected_seats": [SelectedSeat(seat_number="5排6座")],
        "selected_count_visible": 1,
    })
    second = base.model_copy(update={
        "selected_seats": [SelectedSeat(seat_number="6排7座")],
        "selected_count_visible": 1,
    })

    targets = _build_image_quote_targets([first, second])

    assert len(targets) == 2
    assert [target["image_indexes"] for target in targets] == [[0], [1]]
    assert [
        target["recognition"].selected_seats[0].seat_number for target in targets
    ] == ["5排6座", "6排7座"]
    assert all(target["conflict_fields"] == [] for target in targets)


def test_seat_screenshot_and_show_screenshot_create_one_enriched_quote_target() -> None:
    show = MovieImageInfo(
        is_seat_selection=False, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", hall_name="1号厅",
        match_level="EXACT",
    )
    seat = show.model_copy(update={
        "is_seat_selection": True,
        "selected_seats": [SelectedSeat(seat_number="5排6座")],
        "selected_count_visible": 1,
        "hall_name": None,
    })

    targets = _build_image_quote_targets([seat, show])

    assert len(targets) == 1
    assert targets[0]["image_indexes"] == [0, 1]
    assert targets[0]["conflict_fields"] == []
    assert targets[0]["recognition"].hall_name == "1号厅"
    assert targets[0]["recognition"].selected_seats[0].seat_number == "5排6座"


@pytest.mark.asyncio
async def test_agent_tool_quotes_two_seat_screenshots_independently() -> None:
    base = MovieImageInfo(
        is_seat_selection=True, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", match_level="EXACT",
    )
    recognitions = {
        "one.png": base.model_copy(update={
            "selected_seats": [SelectedSeat(seat_number="5排6座")],
            "selected_count_visible": 1,
        }),
        "two.png": base.model_copy(update={
            "selected_seats": [SelectedSeat(seat_number="6排7座")],
            "selected_count_visible": 1,
        }),
    }
    quoted_seats: list[str] = []

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return recognitions[image_url.rsplit("/", 1)[-1]]

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            quoted_seats.append(request.selected_seats[0].seat_number)
            return RealQuote(
                quote_scope="exact_seats", seat_zone_type="selected",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    automation = PluginAutomation(RecognitionTool(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-two-seat-images",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]

    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )
    quoted = [
        await automation._execute_agent_tool(
            "get_authoritative_quote", target["recognition"], identity,
            envelope={"payload": {"imageUrls": urls, "messageType": 2}},
        )
        for target in recognized["quote_targets"]
    ]

    assert len(recognized["quote_targets"]) == 2
    assert recognized["conflict_fields"] == []
    assert quoted_seats == ["5排6座", "6排7座"]
    assert all(result["ok"] is True for result in quoted)


@pytest.mark.asyncio
async def test_agent_multi_image_keeps_success_when_another_image_recognition_fails() -> None:
    success = MovieImageInfo(
        is_seat_selection=True, cinema_id=1001, cinema_name="南山万达影城",
        movie_id=88, movie_name="测试电影", date_text="2026-09-01",
        show_id="10001", showtime_start="19:30", match_level="EXACT",
        selected_seats=[SelectedSeat(seat_number="5排6座", row_no=5, col_no=6)],
        selected_count_visible=1,
    )
    calls: list[str] = []

    class RecognitionTool:
        async def recognize_from_url(
            self, image_url: str, *, buyer_message: str = "",
            out_trade_no: str | None = None,
        ) -> MovieImageInfo:
            calls.append(image_url)
            if image_url.endswith("/two.png"):
                raise TimeoutError("provider timeout")
            return success

    automation = PluginAutomation(RecognitionTool(), _Quote())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-partial-images",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]

    result = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )

    assert result["ok"] is True
    assert result["image_count"] == 2
    assert result["recognized_image_count"] == 1
    assert result["partial_failure"] is True
    assert result["image_types"] == ["seat_selection", "failed"]
    assert result["image_results"] == [
        {"image_index": 0, "status": "success"},
        {"image_index": 1, "status": "error", "error": "recognition_failed"},
    ]
    assert len(result["quote_targets"]) == 1
    assert result["quote_targets"][0]["image_indexes"] == [0]
    assert result["quote_targets"][0]["recognition"]["selected_seats"][0]["seat_number"] == "5排6座"
    assert calls == urls


@pytest.mark.asyncio
async def test_agent_recognition_uses_stable_per_event_image_trade_number() -> None:
    trade_numbers: list[str] = []

    class RecognitionTool:
        async def recognize_from_url(
            self, _image_url: str, *, buyer_message: str = "",
            out_trade_no: str | None = None,
        ) -> MovieImageInfo:
            trade_numbers.append(str(out_trade_no))
            return MovieImageInfo(
                is_seat_selection=True, cinema_name="南山万达影城",
                movie_name="测试电影", date_text="2026-09-01",
                showtime_start="19:30", selected_seats=[SelectedSeat(seat_number="5排6座")],
                selected_count_visible=1, match_level="EXACT",
            )

    automation = PluginAutomation(RecognitionTool(), _Quote())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "stable-event",
    }
    await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"payload": {"messageType": 2}},
    )

    assert len(trade_numbers) == 1
    assert trade_numbers[0].startswith("rec-")
    assert len(trade_numbers[0]) <= 64


@pytest.mark.asyncio
async def test_movie_candidate_choice_uses_liangpiao_official_confirmation(tmp_path) -> None:
    candidate = MovieImageInfo(
        recognition_id="recognize-1", is_seat_selection=True,
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        date_text="2026-09-01", showtime_start="19:30", match_level="CANDIDATE",
        selected_seats=[SelectedSeat(seat_number="5排6座")], selected_count_visible=1,
        candidate_movies=[
            MovieCandidate(movie_id=88, name="测试电影A"),
            MovieCandidate(movie_id=89, name="测试电影B"),
        ],
    )
    confirmed = candidate.model_copy(update={
        "movie_id": 89, "movie_name": "测试电影B", "match_level": "EXACT",
        "candidate_movies": [],
    })
    confirmations: list[dict[str, object]] = []

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return candidate

        async def confirm_recognition_candidate(
            self, recognition_id: str, cinema_id: int | None = None, *,
            movie_id: int | None = None, show_id: str | None = None,
            city_name: str | None = None,
        ) -> MovieImageInfo:
            confirmations.append({
                "recognition_id": recognition_id, "movie_id": movie_id,
                "city_name": city_name,
            })
            return confirmed

    store = PendingCinemaCandidateStore(tmp_path / "movie-candidates.json")
    automation = PluginAutomation(
        RecognitionTool(), _Quote(), pending_cinema_candidate_store=store,
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "movie-candidate-event",
    }
    await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/seat.png"},
        identity, envelope={"payload": {"messageType": 2}},
    )

    result = await automation._execute_agent_tool(
        "resolve_movie", {"movie_id": 89, "buyer_message": "选测试电影B"},
        {**identity, "event_id": "movie-choice-event"},
        envelope={"payload": {"messageType": 1, "content": "选测试电影B"}},
    )

    assert result["ok"] is True
    assert result["recognition"]["movie_id"] == 89
    assert confirmations == [{
        "recognition_id": "recognize-1", "movie_id": 89, "city_name": "深圳",
    }]


@pytest.mark.asyncio
async def test_agent_blocks_quote_when_multiple_images_conflict_on_transaction_fields() -> None:
    first = MovieImageInfo(
        cinema_id=1001, cinema_name="南山万达影城", city="深圳",
        movie_name="测试电影", date_text="今天", showtime_start="19:30",
        confidence=.95, match_level="EXACT",
    )
    second = first.model_copy(update={"cinema_id": 1002, "cinema_name": "宝安万达影城"})

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return first if image_url.endswith("/one.png") else second

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("conflicting images must not reach quote provider")

    automation = PluginAutomation(RecognitionTool(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-conflict",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )
    blocked = await automation._execute_agent_tool(
        "get_authoritative_quote", first.model_dump(mode="json"), identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )

    assert set(recognized["conflict_fields"]) == {"cinema_id", "cinema_name"}
    assert recognized["recognition"]["cinema_id"] is None
    assert recognized["recognition"]["cinema_name"] is None
    assert blocked == {
        "ok": False, "error": "image_fields_conflict",
        "conflict_fields": ["cinema_id", "cinema_name"],
    }


@pytest.mark.asyncio
async def test_multi_image_conflict_gate_survives_engine_restart() -> None:
    first = MovieImageInfo(
        cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", match_level="EXACT",
    )
    second = first.model_copy(update={"showtime_start": "20:30"})

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return first if image_url.endswith("/one.png") else second

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("durable conflict must block quote")

    class PendingStore:
        def __init__(self) -> None:
            self.values: dict[str, MovieImageInfo] = {}
        def get(self, key: str) -> MovieImageInfo | None:
            return self.values.get(key)
        def save(self, key: str, recognition: MovieImageInfo) -> None:
            self.values[key] = recognition
        def delete(self, key: str) -> None:
            self.values.pop(key, None)

    store = PendingStore()
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-conflict",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]
    first_engine = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    recognized = await first_engine._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )
    restarted_engine = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    blocked = await restarted_engine._execute_agent_tool(
        "get_authoritative_quote", first.model_dump(mode="json"), identity,
        envelope={"payload": {"content": "继续", "messageType": 1}},
    )

    assert recognized["conflict_fields"] == ["showtime_start"]
    assert blocked == {
        "ok": False, "error": "image_fields_conflict",
        "conflict_fields": ["showtime_start"],
    }


@pytest.mark.asyncio
async def test_unsupported_or_ticket_voucher_image_cannot_be_used_for_quote(tmp_path) -> None:
    voucher = MovieImageInfo(
        platform="良票", ticket_codes=["123456"], confidence=.95, match_level="EXACT",
    )

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return voucher

    class QuoteTool:
        async def quote(self, _request: MovieImageInfo) -> RealQuote:
            raise AssertionError("non-quotable image must not reach quote provider")

    store = PendingCinemaCandidateStore(tmp_path / "pending-voucher.json")
    automation = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-voucher",
    }
    recognized = await automation._execute_agent_tool(
        "recognize_screenshot", {"image_url": "https://img.alicdn.com/voucher.png"},
        identity, envelope={"payload": {"imageUrls": ["https://img.alicdn.com/voucher.png"]}},
    )
    restarted = PluginAutomation(
        RecognitionTool(), QuoteTool(), pending_cinema_candidate_store=store,
    )
    blocked = await restarted._execute_agent_tool(
        "get_authoritative_quote",
        {
            "cinema_name": "模型填写的影院", "movie_name": "模型填写的影片",
            "date_text": "今天", "showtime_start": "19:30",
        },
        identity, envelope={"payload": {"messageType": 2}},
    )

    assert recognized["image_types"] == ["ticket_voucher"]
    assert "ticket_codes" not in recognized["recognition"]
    assert blocked == {"ok": False, "error": "image_type_not_quotable"}


@pytest.mark.asyncio
async def test_high_confidence_image_cannot_be_reprocessed_in_the_same_event() -> None:
    recognition = MovieImageInfo(
        cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", confidence=.96, match_level="EXACT",
    )
    calls = 0

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            nonlocal calls
            calls += 1
            return recognition

    automation = PluginAutomation(RecognitionTool(), _Quote())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-review",
    }
    arguments = {"image_url": "https://img.alicdn.com/one.png"}
    first = await automation._execute_agent_tool("recognize_screenshot", arguments, identity)
    second = await automation._execute_agent_tool("recognize_screenshot", arguments, identity)

    assert first["ok"] is True
    assert second == {"ok": False, "error": "image_review_not_required"}
    assert calls == 1


@pytest.mark.asyncio
async def test_low_confidence_image_allows_only_one_review_and_merges_disagreement() -> None:
    recognitions = [
        MovieImageInfo(
            cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
            date_text="今天", showtime_start="19:30", confidence=.6, match_level="NONE",
        ),
        MovieImageInfo(
            cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
            date_text="今天", showtime_start="20:30", confidence=.92, match_level="EXACT",
        ),
    ]
    calls = 0

    class RecognitionTool:
        async def recognize_from_url(self, _image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            nonlocal calls
            result = recognitions[calls]
            calls += 1
            return result

    automation = PluginAutomation(RecognitionTool(), _Quote())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-review",
    }
    arguments = {"image_url": "https://img.alicdn.com/one.png"}
    first = await automation._execute_agent_tool("recognize_screenshot", arguments, identity)
    reviewed = await automation._execute_agent_tool("recognize_screenshot", arguments, identity)
    third = await automation._execute_agent_tool("recognize_screenshot", arguments, identity)

    assert first["ok"] is True
    assert reviewed["conflict_fields"] == ["showtime_start"]
    assert reviewed["recognition"]["showtime_start"] is None
    assert third == {"ok": False, "error": "image_review_limit_reached"}
    assert calls == 2


@pytest.mark.asyncio
async def test_buyer_can_clarify_a_text_safe_image_conflict_before_quote() -> None:
    first = MovieImageInfo(
        cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
        date_text="今天", showtime_start="19:30", confidence=.9, match_level="EXACT",
    )
    second = first.model_copy(update={"showtime_start": "20:30"})
    quote_requests: list[MovieImageInfo] = []

    class RecognitionTool:
        async def recognize_from_url(self, image_url: str, *, buyer_message: str = "") -> MovieImageInfo:
            return first if image_url.endswith("/one.png") else second

    class QuoteTool:
        async def quote(self, request: MovieImageInfo) -> RealQuote:
            quote_requests.append(request)
            return RealQuote(
                quote_scope="area_preview", seat_zone_type="W+",
                unit_quote_cents=4_900, total_quote_cents=4_900, ticket_count=1,
            )

    automation = PluginAutomation(RecognitionTool(), QuoteTool())
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-conflict",
    }
    urls = ["https://img.alicdn.com/one.png", "https://img.alicdn.com/two.png"]
    await automation._execute_agent_tool(
        "recognize_screenshot", {"image_urls": urls}, identity,
        envelope={"payload": {"imageUrls": urls, "messageType": 2}},
    )
    mismatch = await automation._execute_agent_tool(
        "resolve_image_conflict",
        {"buyer_message": "是20:30", "field_updates": {"showtime_start": "20:30"}},
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "不是这个", "messageType": 1}},
    )
    resolved = await automation._execute_agent_tool(
        "resolve_image_conflict",
        {"buyer_message": "是20:30", "field_updates": {"showtime_start": "20:30"}},
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "是20:30", "messageType": 1}},
    )
    quoted = await automation._execute_agent_tool(
        "get_authoritative_quote", resolved["recognition"],
        {**identity, "event_id": "event-choice"},
        envelope={"payload": {"content": "是20:30", "messageType": 1}},
    )

    assert mismatch == {
        "ok": False, "error": "image_conflict_buyer_confirmation_required",
    }
    assert resolved["ok"] is True
    assert resolved["remaining_conflict_fields"] == []
    assert resolved["recognition"]["showtime_start"] == "20:30"
    assert quote_requests[0].showtime_start == "20:30"
    assert quoted["ok"] is True


@pytest.mark.asyncio
async def test_image_conflict_rejects_non_conflicted_or_transaction_sensitive_updates() -> None:
    pending = MovieImageInfo(
        cinema_name="南山万达影城", city="深圳", movie_name="测试电影",
        date_text="今天", showtime_start=None, match_level="NONE",
        missing_fields=["showtime_start"], warnings=["conflict:showtime_start"],
    )

    class PendingStore:
        def __init__(self) -> None:
            self.value = pending
        def get(self, _key: str) -> MovieImageInfo | None:
            return self.value
        def save(self, _key: str, recognition: MovieImageInfo) -> None:
            self.value = recognition
        def delete(self, _key: str) -> None:
            self.value = None

    automation = PluginAutomation(
        _Recognition(), _Quote(), pending_cinema_candidate_store=PendingStore(),
    )
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "event_id": "event-choice",
    }
    envelope = {"payload": {"content": "20:30，一张票", "messageType": 1}}
    result = await automation._execute_agent_tool(
        "resolve_image_conflict",
        {
            "buyer_message": "20:30，一张票",
            "field_updates": {"showtime_start": "20:30", "selected_seats": []},
        },
        identity, envelope=envelope,
    )

    assert result == {"ok": False, "error": "image_conflict_updates_not_allowed"}


def test_paid_event_creates_only_a_verified_liangpiao_fulfillment_action() -> None:
    record = {
        "quote_route": "liangpiao_exact", "provider_quote_id": "quote-1",
        "provider_quote_hash": "a" * 64, "confirmation_id": "confirm-1",
        "quote_generation": 2, "confirmed_ticket_count": 1,
        "delivery_state": "delivered", "status": "succeeded",
        "quote_scope": "exact_seats", "total_quote_cents": 4_900,
        "selected_offer": {"offer_id": "primary", "total_quote_cents": 4_900},
    }
    automation = PluginAutomation(
        _Recognition(), _Quote(), quote_finder=lambda confirmed=False, **_: record if confirmed else None,
        liangpiao_order_phone="13800138000",
    )
    action = automation._paid_liangpiao_action(
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "paid-1", "order_id": "order-1"},
        {"event": "order.paid", "timestamp": 1787580000000, "payload": {}},
        {"orderStatus": "paid", "payment": 4_900},
    )

    assert action is not None
    assert action["type"] == "create_liangpiao_order"
    assert action["quote_id"] == "quote-1"
    assert action["generation"] == 2
    assert automation._paid_liangpiao_action(
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "paid-2", "order_id": "order-1"},
        {"event": "order.paid", "timestamp": 1787580000000, "payload": {}},
        {"orderStatus": "paid", "payment": 5_000},
    ) is None


def test_paid_liangpiao_requires_selection_when_quote_has_multiple_offers() -> None:
    record = {
        "quote_route": "liangpiao_exact", "provider_quote_id": "quote-1",
        "provider_quote_hash": "a" * 64, "confirmation_id": "confirm-1",
        "quote_generation": 2, "confirmed_ticket_count": 1,
        "delivery_state": "delivered", "status": "succeeded",
        "quote_scope": "exact_seats", "total_quote_cents": 4_900,
        "offers": [
            {"offer_id": "standard", "total_quote_cents": 4_900},
            {"offer_id": "fixed", "total_quote_cents": 5_500},
        ],
    }
    automation = PluginAutomation(
        _Recognition(), _Quote(), quote_finder=lambda confirmed=False, **_: record if confirmed else None,
        liangpiao_order_phone="13800138000",
    )

    action = automation._paid_liangpiao_action(
        {"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c", "event_id": "paid-1", "order_id": "order-1"},
        {"event": "order.paid", "timestamp": 1787580000000, "payload": {}},
        {"orderStatus": "paid", "payment": 4_900},
    )

    assert action is None


def test_mode_api_persists_shop_default_and_conversation_override(tmp_path) -> None:
    from app.main import create_app

    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    app = create_app(service=_UnusedRecognitionService(), rules_first_store=store)
    headers = {"x-wanda-tenant-id": "tenant-1"}
    with TestClient(app) as client:
        shop = client.put("/api/plugin/shops/shop-1/automation-mode", headers=headers, json={"mode": "agent"})
        assert shop.status_code == 200
        assert shop.json()["mode"] == "agent"
        conversation = client.put(
            "/api/plugin/conversations/buyer-1/chat-1/automation-mode",
            headers=headers, json={"shop_id": "shop-1", "mode": "rules"},
        )
        assert conversation.status_code == 200
        assert conversation.json()["mode"] == "rules"
        current = client.get(
            "/api/plugin/conversations/buyer-1/chat-1/automation-mode?shop_id=shop-1",
            headers=headers,
        )
        assert current.json()["scope"] == "conversation"
        cleared = client.delete(
            "/api/plugin/conversations/buyer-1/chat-1/automation-mode?shop_id=shop-1",
            headers=headers,
        )
        assert cleared.status_code == 200
        assert cleared.json() == {"mode": "agent", "scope": "shop"}
        inherited = client.get(
            "/api/plugin/conversations/buyer-1/chat-1/automation-mode?shop_id=shop-1",
            headers=headers,
        )
        assert inherited.json() == {"mode": "agent", "scope": "shop"}


def test_manual_task_panel_api_hides_lease_and_protected_details(tmp_path) -> None:
    from app.main import create_app

    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    task = store.create_manual_task(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        transaction_id="state-1", transaction_revision=2, reason="order_unverified",
        details={"event_id": "event-1", "private": "hidden"},
    )
    store.claim_manual_task(
        "tenant-1", task["task_id"], expected_revision=2, operator_id="operator-1",
    )
    app = create_app(service=_UnusedRecognitionService(), rules_first_store=store)
    with TestClient(app) as client:
        response = client.get(
            "/api/rules-first/manual-tasks", headers={"x-wanda-tenant-id": "tenant-1"},
        )

    assert response.status_code == 200
    public = response.json()["tasks"][0]
    assert public["claimed_by"] == "operator-1"
    assert "lease_token" not in public
    assert "details" not in public


def test_manual_task_claim_uses_signed_operator_header_and_scopes_lease(tmp_path) -> None:
    from app.main import create_app

    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    task = store.create_manual_task(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        transaction_id="state-1", transaction_revision=2, reason="order_unverified",
    )
    app = create_app(service=_UnusedRecognitionService(), rules_first_store=store)
    tenant = {"x-wanda-tenant-id": "tenant-1"}
    operator = {**tenant, "x-wanda-operator-id": "operator-real"}
    with TestClient(app) as client:
        missing = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/claim",
            headers=tenant, json={"expected_revision": 2, "operator_id": "spoofed"},
        )
        claimed = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/claim",
            headers=operator, json={"expected_revision": 2, "operator_id": "spoofed"},
        )
        owner_view = client.get("/api/rules-first/manual-tasks", headers=operator).json()["tasks"][0]
        other_operator = {**tenant, "x-wanda-operator-id": "operator-other"}
        other_view = client.get(
            "/api/rules-first/manual-tasks", headers=other_operator,
        ).json()["tasks"][0]
        wrong_complete = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/complete",
            headers=other_operator,
            json={"expected_revision": 2, "lease_token": owner_view["lease_token"], "resolution": "resolved"},
        )

    assert missing.status_code == 401
    assert claimed.status_code == 200
    assert claimed.json()["task"]["claimed_by"] == "operator-real"
    assert owner_view["lease_token"]
    assert "lease_token" not in other_view
    assert wrong_complete.status_code == 409


def test_manual_task_cannot_be_marked_resolved_while_transaction_is_still_on_hold(tmp_path) -> None:
    from app.main import create_app
    from app.rules_first_state_store import SqliteTransactionStateStore

    database = tmp_path / "rules.sqlite3"
    store = RulesFirstStore(database)
    states = SqliteTransactionStateStore(database)
    current = states.transition(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        expected_revision=0, event_id="hold-1", transition_code="manual_hold",
        flow_state="MANUAL_HOLD", updates={"automation_control": "human_hold"},
        allow_compatible_bootstrap=True,
    )
    task = store.create_manual_task(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        transaction_id=current.state_id, transaction_revision=current.revision,
        reason="order_unverified",
    )
    app = create_app(
        service=_UnusedRecognitionService(), rules_first_store=store,
        transaction_state_store=states,
    )
    headers = {
        "x-wanda-tenant-id": "tenant-1", "x-wanda-operator-id": "operator-1",
    }
    with TestClient(app) as client:
        claimed = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/claim", headers=headers,
            json={"expected_revision": current.revision},
        ).json()["task"]
        unresolved = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/complete", headers=headers,
            json={
                "expected_revision": current.revision,
                "lease_token": claimed["lease_token"], "resolution": "resolved",
            },
        )
        resumed = client.post(
            f"/api/rules-first/manual-tasks/{task['task_id']}/complete", headers=headers,
            json={
                "expected_revision": current.revision,
                "lease_token": claimed["lease_token"], "resolution": "resume",
            },
        )

    assert unresolved.status_code == 409
    assert unresolved.json()["detail"] == "manual_resolution_state_still_on_hold"
    assert resumed.status_code == 200
    state = states.get(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
    )
    assert state is not None and state.flow_state == "ORDER_UNVERIFIED"


def test_shop_and_conversation_modes_resolve_with_conversation_override(tmp_path) -> None:
    store = RulesFirstStore(tmp_path / "rules.sqlite3")
    identity = {
        "tenant_id": "tenant-1", "shop_id": "shop-1",
        "buyer_id": "buyer-1", "chat_id": "chat-1",
    }

    assert store.resolve_automation_mode(**identity) == "hybrid"
    store.set_shop_automation_mode("tenant-1", "shop-1", "agent")
    assert store.resolve_automation_mode(**identity) == "agent"
    store.set_conversation_automation_mode(**identity, mode="rules")
    assert store.resolve_automation_mode(**identity) == "rules"
    assert store.clear_conversation_automation_mode(**identity) is True
    assert store.resolve_automation_mode(**identity) == "agent"
    assert store.clear_conversation_automation_mode(**identity) is False
