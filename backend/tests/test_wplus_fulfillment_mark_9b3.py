from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from app.rules_first_state_store import SqliteTransactionStateStore
from app.transaction_state_store import TransactionState
from app.wplus_fulfillment import (
    WPLUS_MARK_REPLY_TEMPLATE_AUTHORITY,
    WplusFulfillmentMarkService,
)


IDENTITY = {
    "tenant_id": "tenant-a", "shop_id": "shop-a", "buyer_id": "buyer-a", "chat_id": "chat-a",
}


class Detector:
    def __init__(self, *values: bool | None):
        self.values = list(values)
        self.calls = 0

    async def detect(self, image_url: str) -> bool | None:
        assert image_url.startswith("https://img.alicdn.com/")
        self.calls += 1
        return self.values.pop(0) if self.values else None


class QuoteBook:
    def __init__(self, request_type: str = "WPLUS_AREA"):
        self.record = {
            "record_id": "record-1", "quote_id": "quote-1", "tenant_id": IDENTITY["tenant_id"],
            "shop_id": IDENTITY["shop_id"], "buyer_id": IDENTITY["buyer_id"], "chat_id": IDENTITY["chat_id"],
            "purchase_context_id": "purchase-1", "request_type": request_type,
            "quote_generation": 3, "binding_revision": 2, "total_sell_price_fen": 8980,
        }
        self.calls = 0

    def get_record(self, *, tenant_id: str, record_id: str):
        self.calls += 1
        if tenant_id == self.record["tenant_id"] and record_id == self.record["record_id"]:
            return deepcopy(self.record)
        return None


def seed(
    store: SqliteTransactionStateStore,
    *,
    identity: dict[str, str] = IDENTITY,
    flow_state: str = "WAITING_PAYMENT",
    request_type: str = "WPLUS_AREA",
    context: str = "purchase-1",
    mark: dict[str, object] | None = None,
) -> TransactionState:
    current = store.get_or_create(**identity)
    return store.transition(
        **identity, expected_revision=current.revision, event_id="seed-event",
        transition_code="compat.bootstrap.wplus_fixture", flow_state=flow_state,
        updates={
            "quote_request_type": request_type, "purchase_context_id": context,
            "active_quote_record_id": "record-1", "confirmed_quote_record_id": "record-1",
            "order_id": "order-1", "target_amount_cents": 8980,
            "price_change_status": "succeeded", "payment_status": "unpaid",
            "current_wplus_mark": mark,
            "wplus_mark_status": "submitted" if mark else "not_submitted",
            "wplus_mark_revision": 1 if mark else 0,
            "wplus_mark_history": [mark] if mark else [],
        }, allow_compatible_bootstrap=True,
    )


def service(tmp_path: Path, *, detector: Detector, quote_book: QuoteBook | None = None):
    states = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    quotes = quote_book or QuoteBook()
    return states, quotes, WplusFulfillmentMarkService(
        states, quote_store=quotes, mark_detector=detector,
    )


def test_canonical_quote_context_is_recorded_without_changing_quote_data(tmp_path: Path):
    states, quotes, marks = service(tmp_path, detector=Detector())
    result = marks.record_quote_context(
        {
            "envelope": {"id": "quote-event", "tenantId": "tenant-a", "payload": {
                "accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
            }},
            "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
        },
        {"status": "QUOTED", "quote": deepcopy(quotes.record)},
    )
    assert result is not None and result.flow_state == "QUOTED"
    assert result.quote_request_type == "WPLUS_AREA"
    assert result.purchase_context_id == "purchase-1"
    assert result.active_quote_record_id == "record-1"
    assert quotes.calls == 0


@pytest.mark.asyncio
async def test_prepayment_mark_is_saved_without_new_quote_or_binding_change(tmp_path: Path):
    assert WPLUS_MARK_REPLY_TEMPLATE_AUTHORITY == "wplus_mark_required_template"
    detector = Detector(True)
    states, quotes, marks = service(tmp_path, detector=detector)
    quote_before = deepcopy(quotes.record)
    before = seed(states)
    result = await marks.submit_mark(
        **IDENTITY, purchase_context_id="purchase-1", event_id="mark-1",
        image_url="https://img.alicdn.com/mark-1.png", message_id="message-1",
    )
    after = states.get(**IDENTITY)
    assert result["status"] == "MARK_SUBMITTED"
    assert after is not None and after.flow_state == "WAITING_PAYMENT"
    assert after.wplus_mark_status == "submitted"
    assert after.wplus_mark_revision == 1
    assert after.current_wplus_mark.image_reference.endswith("mark-1.png")
    assert after.generation == before.generation
    assert after.active_quote_record_id == before.active_quote_record_id
    assert after.target_amount_cents == before.target_amount_cents
    assert quotes.calls == 0
    assert quotes.record == quote_before
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_prepayment_image_is_fulfillment_mark_not_new_quote(tmp_path: Path):
    detector = Detector(True)
    states, quotes, marks = service(tmp_path, detector=detector)
    seed(states, flow_state="WAITING_PAYMENT")
    result = await marks.process_event({
        "envelope": {"id": "prepay-image", "tenantId": "tenant-a", "event": "im.message.received",
                     "payload": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
                                 "itemId": "purchase-1", "imageUrls": ["https://img.alicdn.com/prepay.png"]}},
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    })
    current = states.get(**IDENTITY)
    assert result["status"] == "MARK_SUBMITTED"
    assert current is not None and current.flow_state == "WAITING_PAYMENT"
    assert current.wplus_mark_revision == 1
    assert quotes.calls == 0


@pytest.mark.asyncio
async def test_second_valid_mark_wins_and_history_survives_restart(tmp_path: Path):
    detector = Detector(True, True)
    states, _, marks = service(tmp_path, detector=detector)
    seed(states)
    await marks.submit_mark(
        **IDENTITY, purchase_context_id="purchase-1", event_id="mark-1",
        image_url="https://img.alicdn.com/mark-1.png", message_id="message-1",
    )
    second = await marks.submit_mark(
        **IDENTITY, purchase_context_id="purchase-1", event_id="mark-2",
        image_url="https://img.alicdn.com/mark-2.png", message_id="message-2",
    )
    restarted = SqliteTransactionStateStore(tmp_path / "rules.sqlite3")
    current = restarted.get(**IDENTITY)
    assert second["status"] == "MARK_SUBMITTED"
    assert current is not None and current.wplus_mark_revision == 2
    assert current.current_wplus_mark.image_reference.endswith("mark-2.png")
    assert [item.image_reference for item in current.wplus_mark_history] == [
        "https://img.alicdn.com/mark-1.png", "https://img.alicdn.com/mark-2.png",
    ]


@pytest.mark.asyncio
async def test_payment_validated_without_mark_enters_waiting_state(tmp_path: Path):
    states, _, marks = service(tmp_path, detector=Detector())
    seed(states)
    result = await marks.apply_payment_validated(
        **IDENTITY, purchase_context_id="purchase-1", event_id="payment-fixture-1",
        platform_order_id="order-1",
    )
    current = states.get(**IDENTITY)
    assert result["status"] == "WAITING_WPLUS_MARK"
    assert current is not None and current.flow_state == "WAITING_WPLUS_MARK"
    assert current.payment_status == "verified_paid"


@pytest.mark.asyncio
async def test_payment_validated_with_mark_is_ready_without_second_question(tmp_path: Path):
    mark = {
        "revision": 1, "tenant_id": "tenant-a", "shop_id": "shop-a", "buyer_id": "buyer-a",
        "chat_id": "chat-a", "purchase_context_id": "purchase-1",
        "image_reference": "https://img.alicdn.com/mark-1.png", "message_id": "message-1",
        "event_id": "mark-1", "submitted_at": "2026-09-05T12:00:00+00:00",
    }
    states, _, marks = service(tmp_path, detector=Detector(), quote_book=QuoteBook())
    seed(states, mark=mark)
    result = await marks.apply_payment_validated(
        **IDENTITY, purchase_context_id="purchase-1", event_id="payment-fixture-2",
        platform_order_id="order-1",
    )
    current = states.get(**IDENTITY)
    assert result["status"] == "READY_FOR_MANUAL_TICKETING"
    assert current is not None and current.flow_state == "READY_FOR_MANUAL_TICKETING"
    assert result["reply"] is None


@pytest.mark.asyncio
async def test_waiting_marked_image_becomes_ready_without_quote_or_reprice(tmp_path: Path):
    detector = Detector(True)
    states, quotes, marks = service(tmp_path, detector=detector)
    seed(states, flow_state="WAITING_WPLUS_MARK")
    before = states.get(**IDENTITY)
    result = await marks.process_event({
        "envelope": {"id": "mark-event", "tenantId": "tenant-a", "event": "im.message.received",
                     "payload": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
                                 "itemId": "purchase-1", "messageId": "message-2",
                                 "imageUrls": ["https://img.alicdn.com/mark-2.png"]}},
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    })
    after = states.get(**IDENTITY)
    assert result["status"] == "READY_FOR_MANUAL_TICKETING"
    assert after is not None and after.flow_state == "READY_FOR_MANUAL_TICKETING"
    assert after.revision == before.revision + 1
    assert quotes.calls == 0
    assert detector.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("detector_value", [False, None])
async def test_waiting_unmarked_or_unknown_stays_waiting_and_uses_exact_reply(tmp_path: Path, detector_value: bool | None):
    detector = Detector(detector_value)
    states, _, marks = service(tmp_path, detector=detector)
    seed(states, flow_state="WAITING_WPLUS_MARK")
    result = await marks.process_event({
        "envelope": {"id": f"unmarked-{detector_value}", "tenantId": "tenant-a", "event": "im.message.received",
                     "payload": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
                                 "itemId": "purchase-1", "imageUrls": ["https://img.alicdn.com/plain.png"]}},
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    })
    current = states.get(**IDENTITY)
    assert current is not None and current.flow_state == "WAITING_WPLUS_MARK"
    assert result["reply"] == "辛苦标记一下位置截图发我哈"
    assert result["status"] == "WAITING_WPLUS_MARK"
    assert result["mark_result"] is detector_value


@pytest.mark.asyncio
@pytest.mark.parametrize("request_type", ["EXACT_SEATS", "LIANGPIAO"])
async def test_non_wplus_transactions_do_not_enter_wplus_mark_flow(tmp_path: Path, request_type: str):
    states, _, marks = service(tmp_path, detector=Detector())
    seed(states, request_type=request_type)
    result = await marks.apply_payment_validated(
        **IDENTITY, purchase_context_id="purchase-1", event_id=f"payment-{request_type}",
        platform_order_id="order-1",
    )
    current = states.get(**IDENTITY)
    assert result["status"] == "NOT_WPLUS_AREA"
    assert current is not None and current.flow_state == "WAITING_PAYMENT"


@pytest.mark.asyncio
async def test_duplicate_mark_event_is_idempotent_and_does_not_process_twice(tmp_path: Path):
    detector = Detector(True, True)
    states, _, marks = service(tmp_path, detector=detector)
    seed(states, flow_state="WAITING_WPLUS_MARK")
    body = {
        "envelope": {"id": "mark-duplicate", "tenantId": "tenant-a", "event": "im.message.received",
                     "payload": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a",
                                 "itemId": "purchase-1", "imageUrls": ["https://img.alicdn.com/mark.png"]}},
        "session": {"accountUnb": "shop-a", "peerUnb": "buyer-a", "chatId": "chat-a"},
    }
    first = await marks.process_event(body)
    second = await marks.process_event(body)
    current = states.get(**IDENTITY)
    assert first["status"] == second["status"] == "READY_FOR_MANUAL_TICKETING"
    assert current is not None and current.wplus_mark_revision == 1
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_mark_context_isolated_by_tenant_shop_buyer_chat_and_purchase_context(tmp_path: Path):
    states, _, marks = service(tmp_path, detector=Detector(True))
    seed(states)
    other = {"tenant_id": "tenant-b", "shop_id": "shop-b", "buyer_id": "buyer-b", "chat_id": "chat-b"}
    seed(states, identity=other, context="purchase-2")
    assert states.get(**IDENTITY).current_wplus_mark is None
    assert states.get(**other).current_wplus_mark is None
    result = await marks.submit_mark(
        **other, purchase_context_id="purchase-2", event_id="other-mark",
        image_url="https://img.alicdn.com/other.png", message_id="other-message",
    )
    assert result["status"] == "MARK_SUBMITTED"
    assert states.get(**IDENTITY).current_wplus_mark is None
    assert states.get(**other).current_wplus_mark.image_reference.endswith("other.png")


@pytest.mark.asyncio
async def test_wrong_purchase_context_cannot_overwrite_current_mark(tmp_path: Path):
    states, _, marks = service(tmp_path, detector=Detector(True))
    seed(states)
    result = await marks.submit_mark(
        **IDENTITY, purchase_context_id="purchase-2", event_id="wrong-context",
        image_url="https://img.alicdn.com/wrong.png", message_id="wrong",
    )
    current = states.get(**IDENTITY)
    assert result["status"] == "PURCHASE_CONTEXT_MISMATCH"
    assert current is not None and current.current_wplus_mark is None
