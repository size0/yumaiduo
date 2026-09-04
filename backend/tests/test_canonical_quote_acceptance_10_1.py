from __future__ import annotations

from pathlib import Path

import pytest

from app.canonical_buyer_reply import CanonicalBuyerReplyRenderer
from app.recognition_v2.liangpiao import LiangpiaoV2Transport
from app.recognition_v2.models import RecognitionResult
from app.rule_state_coordinator import RuleStateCoordinator
from app.rules_first_runtime import RulesFirstRuntime
from app.rules_first_state_store import SqliteTransactionStateStore
from app.rules_first_store import RulesFirstStore


class PlainProtector:
    def protect(self, value: str) -> str:
        return f"enc:{value}"

    def unprotect(self, value: str) -> str:
        return value.removeprefix("enc:")


def event() -> dict[str, object]:
    return {
        "envelope": {
            "id": "canonical-event", "tenantId": "tenant-1", "event": "im.message.received",
            "payload": {
                "imageUrls": ["https://img.example/1"], "accountUnb": "shop-1",
                "peerUnb": "buyer-1", "chatId": "chat-1",
            },
        },
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
    }


def test_canonical_renderer_uses_structured_quote_without_keyword_parsing() -> None:
    renderer = CanonicalBuyerReplyRenderer()
    result = renderer.render({
        "status": "QUOTED", "quote": {
            "provider_route": "LIANGPIAO", "request_type": "EXACT_SEATS",
            "cinema": "上海青浦万达茂店", "movie": "奥德赛",
            "quote_date": "2026-09-06", "showtime_start": "18:50",
            "selected_seats": [{"seat_no": "10排16座"}],
            "unit_sell_price_fen": 4640, "total_sell_price_fen": 4640,
        },
    })
    assert result == {"kind": "QUOTE_READY_EXACT", "text": "上海青浦万达茂店《奥德赛》9月6日18:50这场，10排16座，46.4/张，共46.4，直接拍就行哈"}


def test_canonical_renderer_uses_buyer_safe_wanda_exact_quote() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "QUOTED", "quote": {
            "provider_route": "WANDA_SELF", "request_type": "EXACT_SEATS",
            "cinema": "牡丹江万达", "movie": "坠落2",
            "quote_date": "2026-09-05", "showtime_start": "19:55",
            "selected_seats": [{"seat_number": "8排8座"}],
            "seat_quotes": [{"seat_label": "8排8座", "sell_price_fen": 3910}],
            "unit_sell_price_fen": 3910, "total_sell_price_fen": 3910,
        },
    })
    assert result["text"] == "牡丹江万达《坠落2》9月5日19:55这场，8排8座，39.1/张，共39.1，直接拍就行哈"


def test_canonical_renderer_uses_buyer_safe_wplus_preview() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "QUOTED", "quote": {
            "provider_route": "WANDA_SELF", "request_type": "WPLUS_AREA",
            "cinema": "牡丹江万达", "movie": "坠落2",
            "quote_date": "2026-09-05", "showtime_start": "19:55",
            "unit_sell_price_fen": 3730,
        },
    })
    assert result["text"] == "牡丹江万达《坠落2》9月5日19:55这场，W+ 37.3/张，需要几张呀"


def test_canonical_renderer_includes_wplus_purchase_summary_when_ready() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "QUOTED", "quote": {
            "provider_route": "WANDA_SELF", "request_type": "WPLUS_AREA",
            "cinema": "牡丹江万达", "movie": "坠落2",
            "quote_date": "2026-09-05", "showtime_start": "19:55",
            "unit_sell_price_fen": 2800, "total_sell_price_fen": 5600,
            "ticket_count": 2,
        },
    })
    assert result["text"] == "牡丹江万达《坠落2》9月5日19:55这场，W+ 28/张，共2张56，直接拍就行哈"
    assert "座" not in result["text"]


def test_canonical_renderer_keeps_seat_failure_buyer_safe_without_price() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "SEAT_FACTS_UNAVAILABLE", "reason": "TARGET_SEAT_NOT_AVAILABLE",
    })
    assert result == {
        "kind": "SEAT_FACTS_UNAVAILABLE",
        "text": "这几个座位现在已经没有了，可以重新选一下座位发我哈",
    }


def test_canonical_renderer_asks_liangpiao_buyer_to_select_seats() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "LIANGPIAO_FACTS_INCOMPLETE", "route": "LIANGPIAO",
        "reason": "PROVIDER_IDS_OR_SELECTED_SEATS_REQUIRED",
    })
    assert result["text"] == "这场需要先选好座位，把选座截图发我就可以哈"


def test_canonical_renderer_does_not_use_fulfillment_mark_template_for_unknown_mark() -> None:
    result = CanonicalBuyerReplyRenderer().render({
        "status": "MANUAL_MARK_REQUIRED", "reason": "MANUAL_MARK_UNAVAILABLE",
    })
    assert result["kind"] == "MANUAL_MARK_UNKNOWN"
    assert result["text"] != "辛苦标记一下位置截图发我哈"


def test_canonical_reply_command_is_durable_and_deduplicated(tmp_path: Path) -> None:
    protector = PlainProtector()
    outbox = RulesFirstStore(tmp_path / "rules.sqlite3", protector=protector)
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3", protector=protector)
    runtime = RulesFirstRuntime(outbox, object(), RuleStateCoordinator(states), states)
    result = {
        "status": "ROUTE_UNRESOLVED", "route": "UNRESOLVED",
        "reason": "CITY_REQUIRED", "current_runtime_reply": "请补充城市。",
        "canonical_reply_kind": "UNRESOLVED_REQUIRED_FIELD",
    }
    first = runtime.accept_canonical_result(event(), result)
    second = runtime.accept_canonical_result(event(), result)
    assert first["duplicate"] is False
    assert first["commands"][0]["command_type"] == "send_message"
    assert second["duplicate"] is True
    assert second["commands"] == []


@pytest.mark.asyncio
async def test_liangpiao_enrichment_uses_read_details_without_promoting_ids() -> None:
    transport = object.__new__(LiangpiaoV2Transport)

    class ReadClient:
        async def cinema_detail(self, **_: object) -> dict[str, object]:
            return {"cityName": "常州", "name": "常州溧阳万达广场店"}

        async def show_detail(self, **_: object) -> dict[str, object]:
            return {"showId": "17203688", "hallName": "1号厅"}

    transport._client = ReadClient()
    recognition = RecognitionResult(
        city_text=None, cinema_text="万达影城...", cinema_truncated=True,
        movie="电影", show_date="2026-09-04", start_time="19:30", hall=None,
        raw_provider_result={"data": {"finalResults": {"cinemaId": 4748, "showId": "17203688"}}},
    )
    enriched = await transport.enrich(recognition)
    assert enriched.city_text == "常州"
    assert enriched.cinema_text == "常州溧阳万达广场店"
    assert enriched.cinema_truncated is False
    assert enriched.hall == "1号厅"
    assert not hasattr(enriched, "wanda_store_id")
