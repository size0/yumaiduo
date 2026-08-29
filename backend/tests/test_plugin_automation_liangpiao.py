from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.models import CinemaCandidate, MovieImageInfo, RealQuote, SelectedSeat
from app.pending_cinema_candidate_store import PendingCinemaCandidateStore
from app.plugin_automation import PluginAutomation


@dataclass
class LiangpiaoRecognitionStub:
    candidate: MovieImageInfo
    confirmed: MovieImageInfo
    confirm_calls: list[tuple[str, int]]

    async def recognize_from_url(self, image_url: str, *, city_name: str | None = None) -> MovieImageInfo:
        assert image_url.startswith("https://img.alicdn.com/")
        return self.candidate

    async def confirm_recognition_candidate(self, recognition_id: str, cinema_id: int) -> MovieImageInfo:
        self.confirm_calls.append((recognition_id, cinema_id))
        return self.confirmed


@dataclass
class QuoteStub:
    calls: list[str]

    async def quote(self, recognition: MovieImageInfo) -> RealQuote:
        self.calls.append(recognition.cinema_name or "")
        return RealQuote(
            quote_scope="exact_seats", seat_zone_type="W+", total_quote_cents=9180,
            ticket_count=2, seat_quotes=[
                {"seat_number": "6排10座", "seat_zone_type": "W+", "original_price_cents": 4590, "unit_quote_cents": 4590},
                {"seat_number": "6排11座", "seat_zone_type": "W+", "original_price_cents": 4590, "unit_quote_cents": 4590},
            ], pricing_source="良票确认后的官方实时数据", pricing_rule_version="test",
        )


def _event(content: str, message_id: str, messages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "envelope": {"id": f"event-{message_id}", "tenantId": "tenant-1", "event": "im.message.received", "timestamp": 1787580000000,
                      "payload": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1", "remoteMessageId": message_id,
                                  "messageType": 1, "content": content}},
        "session": {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        "recent_messages": messages,
    }


@pytest.mark.asyncio
async def test_exact_match_with_advisory_candidates_continues_to_quote() -> None:
    exact = MovieImageInfo(
        platform="万达", city="厦门", cinema_name="万达影城（鹭港广场CINITY店）", movie_name="欢迎来龙餐馆",
        date="2026-08-28", date_text="2026-08-28", showtime_start="22:40", hall_name="1号CINITY厅",
        selected_seats=[SelectedSeat(seat_number="6排10座")], selected_count_visible=1,
        recognition_id="exact-1", match_level="EXACT", show_id="13927898", candidate_cinemas=[
            CinemaCandidate(cinema_id=10027, name="万达影城（鹭港广场CINITY店）", city_name="厦门", score=1),
            CinemaCandidate(cinema_id=1900, name="万达影城（正翔广场CINITY店）", city_name="包头", score=.79),
        ], confidence=.95,
    )
    recognizer = LiangpiaoRecognitionStub(exact, exact, [])
    quoter = QuoteStub([])
    automation = PluginAutomation(recognizer, quoter, mode="auto")
    image_messages = [{"direction": "inbound", "messageType": 2, "imageUrls": ["https://img.alicdn.com/exact.webp"],
                       "sentAtMs": 1787579900000, "content": "[图片]", "messageId": "image-exact"}]

    result = await automation.process_event(_event("", "image-exact", image_messages))

    action = result["decision"]["actions"][0]
    assert quoter.calls == ["万达影城（鹭港广场CINITY店）"]
    assert "回复序号" not in action["text"]
    assert "报价" in action["text"]


@pytest.mark.asyncio
async def test_explicit_seat_list_reprices_and_sends_order_guide() -> None:
    recognition = MovieImageInfo(
        platform="万达", city="厦门", cinema_name="万达影城（鹭港广场CINITY店）", movie_name="欢迎来龙餐馆",
        date="2026-08-28", date_text="2026-08-28", showtime_start="22:40", hall_name="1号CINITY厅",
    )
    quote = QuoteStub([])
    recorded: list[dict[str, object]] = []
    automation = PluginAutomation(
        LiangpiaoRecognitionStub(recognition, recognition, []), quote, mode="auto",
        quote_finder=lambda **_: {
            "record_id": "old-quote", "tenant_id": "tenant-1", "shop_id": "shop-1",
            "buyer_id": "buyer-1", "chat_id": "chat-1", "item_id": None,
            "status": "succeeded", "delivery_state": "delivered", "city": "厦门",
            "cinema": "万达影城（鹭港广场CINITY店）", "movie": "欢迎来龙餐馆",
            "quote_date": "2026-08-28", "showtime_start": "22:40", "hall": "1号CINITY厅",
        },
        quote_recorder=recorded.append,
    )
    messages = [
        {"direction": "outbound", "messageType": 1, "content": "报价：64.00/张", "messageId": "quote-1", "sentAtMs": 1787579910000},
        {"direction": "inbound", "messageType": 1, "content": "7排13、14", "messageId": "seat-1", "sentAtMs": 1787579920000},
    ]

    result = await automation.process_event(_event("7排13、14", "seat-1", messages))

    action = result["decision"]["actions"][0]
    assert quote.calls == ["万达影城（鹭港广场CINITY店）"]
    assert recorded[0]["source"] == "buyer_seat_selection"
    assert recorded[0]["ticket_count"] == 2
    assert "请直接提交订单" in action["text"]


@pytest.mark.asyncio
async def test_candidate_is_sent_and_buyer_choice_confirms_then_reprices(tmp_path) -> None:
    candidate = MovieImageInfo(
        platform="万达", city="厦门", cinema_name="万达影城（鹭港广场CINITY店）", movie_name="欢迎来龙餐馆",
        date="2026-08-28", date_text="2026-08-28", showtime_start="22:40", hall_name="1号CINITY厅",
        selected_seats=[SelectedSeat(seat_number="6排10座"), SelectedSeat(seat_number="6排11座")], selected_count_visible=2,
        recognition_id="13576", match_level="CANDIDATE", candidate_cinemas=[
            CinemaCandidate(cinema_id=10027, name="万达影城（鹭港广场CINITY店）", city_name="厦门", address="地址一", score=1),
            CinemaCandidate(cinema_id=1900, name="万达影城（正翔广场CINITY店）", city_name="包头", address="地址二", score=.79),
        ], confidence=.95,
    )
    # Liangpiao may keep matchLevel=CANDIDATE after an explicit buyer choice,
    # while narrowing the result to the exact selected cinema and omitting showId.
    # V4 can safely continue because the buyer selected this cinema explicitly
    # and Wanda's official quote matcher resolves the current show from fields.
    confirmed = candidate.model_copy(update={
        "match_level": "CANDIDATE", "show_id": None,
        "candidate_cinemas": [candidate.candidate_cinemas[0]],
    })
    recognizer = LiangpiaoRecognitionStub(candidate, confirmed, [])
    quoter = QuoteStub([])
    pending_store = PendingCinemaCandidateStore(tmp_path / "pending.json")
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", pending_cinema_candidate_store=pending_store,
    )
    image_messages = [{"direction": "inbound", "messageType": 2, "imageUrls": ["https://img.alicdn.com/ticket.webp"],
                       "sentAtMs": 1787579900000, "content": "[图片]", "messageId": "image-1"}]

    first = await automation.process_event(_event("", "image-1", image_messages))
    first_action = first["decision"]["actions"][0]
    assert first_action["type"] == "send_message"
    assert "1、厦门 万达影城（鹭港广场CINITY店）" in first_action["text"]
    assert "2、包头 万达影城（正翔广场CINITY店）" in first_action["text"]

    choice_messages = [*image_messages,
                       {"direction": "outbound", "messageType": 1, "content": first_action["text"], "messageId": "reply-1", "sentAtMs": 1787579910000},
                       {"direction": "inbound", "messageType": 1, "content": "1", "messageId": "choice-1", "sentAtMs": 1787579920000}]
    # Simulate a worker restart between the candidate reply and the buyer's
    # choice. The persisted context must still bind the same explicit choice.
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", pending_cinema_candidate_store=pending_store,
    )
    second = await automation.process_event(_event("1", "choice-1", choice_messages))
    second_action = second["decision"]["actions"][0]
    assert recognizer.confirm_calls == [("13576", 10027)]
    assert quoter.calls == ["万达影城（鹭港广场CINITY店）"]
    assert second_action["type"] == "send_message"
    assert "报价" in second_action["text"]
