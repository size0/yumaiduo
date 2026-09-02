from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
@pytest.mark.xfail(reason="seat corrections are now Agent tool calls, not local phrase routing", strict=False)
@pytest.mark.parametrize("buyer_seat_text", [
    "9排的13 14",
    "9排的13和14",
    "9排13 14",
    "第9排13、14",
])
async def test_natural_language_seat_correction_reprices_from_existing_quote_context(
    buyer_seat_text: str,
) -> None:
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
        {"direction": "inbound", "messageType": 1, "content": buyer_seat_text, "messageId": "seat-1", "sentAtMs": 1787579920000},
    ]

    result = await automation.process_event(_event(buyer_seat_text, "seat-1", messages))

    action = result["decision"]["actions"][0]
    assert quote.calls == ["万达影城（鹭港广场CINITY店）"]
    assert recorded[0]["source"] == "buyer_seat_selection"
    assert recorded[0]["ticket_count"] == 2
    assert recorded[0]["seat_display"] == "9排13座、9排14座"
    assert "重新发送截图" not in action["text"]
    assert "请直接提交订单" in action["text"]


@pytest.mark.asyncio
async def test_single_candidate_is_confirmed_automatically_instead_of_asking_for_one(tmp_path) -> None:
    candidate = MovieImageInfo(
        platform="万达", city="北京", cinema_name="万达影城（延庆万达广场CINITY店）", movie_name="欢迎来龙餐馆",
        date="2026-08-30", date_text="2026-08-30", showtime_start="17:05", hall_name="2号厅",
        selected_seats=[SelectedSeat(seat_number="5排6座"), SelectedSeat(seat_number="5排5座")],
        selected_count_visible=2, recognition_id="15358", match_level="CANDIDATE",
        candidate_cinemas=[CinemaCandidate(cinema_id=287, name="万达影城（延庆万达广场CINITY店）", city_name="北京", score=1)],
        confidence=.95,
    )
    confirmed = candidate.model_copy(update={
        "match_level": "EXACT", "show_id": "show-1", "candidate_cinemas": [],
    })
    recognizer = LiangpiaoRecognitionStub(candidate, confirmed, [])
    quoter = QuoteStub([])
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", pending_cinema_candidate_store=PendingCinemaCandidateStore(tmp_path / "pending.json"),
    )
    image_messages = [{"direction": "inbound", "messageType": 2, "imageUrls": ["https://img.alicdn.com/ticket.webp"],
                       "sentAtMs": 1787579900000, "content": "[图片]", "messageId": "single-candidate"}]

    result = await automation.process_event(_event("", "single-candidate", image_messages))

    action = result["decision"]["actions"][0]
    assert recognizer.confirm_calls == [("15358", 287)]
    assert quoter.calls == ["万达影城（延庆万达广场CINITY店）"]
    assert "回复序号" not in action["text"]
    assert "报价" in action["text"]


@pytest.mark.asyncio
@pytest.mark.xfail(reason="cinema candidate selection is now Agent tool orchestration", strict=False)
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
    assert f"1、{candidate.candidate_cinemas[0].name}" in first_action["text"]
    assert f"2、{candidate.candidate_cinemas[1].name}" in first_action["text"]

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
@pytest.mark.asyncio
async def test_failed_limit_requires_explicit_fixed_consent_then_quantity_confirmation() -> None:
    state = SimpleNamespace(
        fixed_switch_status="pending",
        fixed_switch_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
        fixed_switch_source_order_no="lp-failed-1", order_id="fish-1", generation=1,
        fixed_switch_source_order_status="closed",
        fixed_switch_source_platform_order_id="fish-1",
        fixed_switch_replacement_order_id=None,
        fixed_switch_quote_confirmation_status="none", fixed_switch_quote_id=None,
        fixed_switch_quote_hash=None, fixed_switch_quote_generation=None,
        confirmed_ticket_count=None, target_amount_cents=None,
    )
    class StateStore:
        def get(self, **_):
            return state
    created: list[dict[str, object]] = []
    async def create_fixed(payload):
        created.append(payload)
        return {"quote_id": "lpq-fixed-1", "quote_hash": "f" * 64, "buyer_amount_fen": 8370, "generation": 1}
    source_quote = {"quote_id": "lpq-limit-1", "snapshot": {"cinema_id": 10, "show_id": "show-1",
        "seats": [{"row_no": 9, "col_no": 13}, {"row_no": 9, "col_no": 14}], "ticket_mode": "STANDARD", "generation": 1}}
    automation = PluginAutomation(
        LiangpiaoRecognitionStub(MovieImageInfo(), MovieImageInfo(), []), QuoteStub([]), mode="auto",
        transaction_state_store=StateStore(), liangpiao_order_finder=lambda **_: {"quote_id": "lpq-limit-1"},
        liangpiao_quote_finder=lambda _quote_id: source_quote, liangpiao_fixed_quote_creator=create_fixed,
        liangpiao_order_phone="13800138000",
    )
    no_consent = await automation.process_event(_event("多少钱", "m1", [{"direction": "inbound", "messageType": 1, "content": "多少钱", "messageId": "m1"}]))
    assert no_consent["decision"]["reason"] != "fixed_switch_quote_ready"
    assert not any(action.get("type") == "create_liangpiao_order" for action in no_consent["decision"]["actions"])
    consent = await automation.process_event(_event("换一口价继续", "m2", [{"direction": "inbound", "messageType": 1, "content": "换一口价继续", "messageId": "m2"}]))
    assert consent["decision"]["reason"] == "fixed_switch_quote_ready"
    assert consent["decision"]["actions"][0]["fixed_switch_quote"]["quote_id"] == "lpq-fixed-1"
    assert created[0]["price_mode"] == "FIXED"
    state.fixed_switch_status = "confirmed"
    state.fixed_switch_quote_confirmation_status = "pending"
    state.fixed_switch_quote_id = "lpq-fixed-1"
    state.fixed_switch_quote_hash = "f" * 64
    state.fixed_switch_quote_generation = 1
    state.confirmed_ticket_count = 2
    state.target_amount_cents = 8370
    confirmed = await automation.process_event(_event("确认一口价2张", "m3", [{"direction": "inbound", "messageType": 1, "content": "确认一口价2张", "messageId": "m3"}]))
    assert confirmed["decision"]["reason"] == "fixed_switch_price_confirmed"
    assert confirmed["decision"]["actions"][0]["type"] == "send_message"
    assert not any(action.get("type") == "create_liangpiao_order" for action in confirmed["decision"]["actions"])

    state.fixed_switch_quote_confirmation_status = "confirmed"
    created_event = _event("", "new-order", [])
    created_event["envelope"]["event"] = "order.created"
    created_event["envelope"]["payload"]["orderId"] = "fish-fixed-2"
    created_event["order"] = {
        "orderId": "fish-fixed-2", "accountUnb": "shop-1", "buyerUnb": "buyer-1",
        "chatId": "chat-1", "orderStatus": 1, "payment": 9999,
    }
    bound = await automation.process_event(created_event)
    assert bound["decision"]["reason"] == "fixed_switch_quote_bound_to_new_order"
    assert bound["decision"]["actions"][0]["type"] == "change_order_price"
    assert bound["decision"]["actions"][0]["quote_snapshot"]["target_amount_cents"] == 8370
    assert not any(action.get("type") == "create_liangpiao_order" for action in bound["decision"]["actions"])


@pytest.mark.asyncio
async def test_fixed_switch_waits_until_original_marketplace_order_is_closed() -> None:
    state = SimpleNamespace(
        fixed_switch_status="pending",
        fixed_switch_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
        fixed_switch_source_order_no="lp-failed-1", fixed_switch_source_order_status="refund_pending",
    )

    class StateStore:
        def get(self, **_):
            return state

    calls = 0

    async def creator(_payload):
        nonlocal calls
        calls += 1
        return {}

    automation = PluginAutomation(
        LiangpiaoRecognitionStub(MovieImageInfo(), MovieImageInfo(), []), QuoteStub([]), mode="auto",
        transaction_state_store=StateStore(), liangpiao_fixed_quote_creator=creator,
    )
    result = await automation.process_event(_event(
        "换一口价继续", "waiting-refund",
        [{"direction": "inbound", "messageType": 1, "content": "换一口价继续", "messageId": "waiting-refund"}],
    ))
    assert result["decision"]["reason"] == "fixed_switch_source_order_not_closed"
    assert calls == 0
    assert not any(action.get("type") == "create_liangpiao_order" for action in result["decision"]["actions"])


@pytest.mark.asyncio
async def test_fixed_switch_expired_or_rejected_never_calls_quote_creator() -> None:
    calls = 0
    class StateStore:
        def __init__(self, status, expiry):
            self.state = SimpleNamespace(fixed_switch_status=status, fixed_switch_expires_at=expiry)
        def get(self, **_):
            return self.state
    async def creator(_payload):
        nonlocal calls
        calls += 1
        return {}
    expired = PluginAutomation(
        LiangpiaoRecognitionStub(MovieImageInfo(), MovieImageInfo(), []), QuoteStub([]), mode="auto",
        transaction_state_store=StateStore("pending", "2000-01-01T00:00:00+00:00"),
        liangpiao_fixed_quote_creator=creator,
    )
    result = await expired.process_event(_event("换一口价", "expired-1", [{"direction": "inbound", "messageType": 1, "content": "换一口价", "messageId": "expired-1"}]))
    assert result["decision"]["reason"] == "fixed_switch_expired"
    assert calls == 0
