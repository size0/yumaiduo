from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import MovieImageInfo
from app.quote_record_store import QuoteRecordStore


class UnusedRecognitionService:
    async def recognize(
        self, image: bytes, content_type: str, buyer_message: str = "", *,
        prior_recognitions: list[MovieImageInfo] | None = None,
    ) -> MovieImageInfo:
        raise AssertionError("recognition should not run")


class PlainProtector:
    def protect(self, value: str) -> str:
        return "protected:" + base64.b64encode(value.encode("utf-8")).decode("ascii")

    def unprotect(self, value: str) -> str:
        if not value.startswith("protected:"):
            raise ValueError("invalid")
        return base64.b64decode(value.removeprefix("protected:"), validate=True).decode("utf-8")


def record(record_id: str, tenant_id: str, created_at: str) -> dict[str, object]:
    return {
        "record_id": record_id,
        "tenant_id": tenant_id,
        "shop_id": "shop-1",
        "buyer_id": "buyer-1",
        "chat_id": "chat-1",
        "created_at": created_at,
        "cinema": "测试影城",
        "original_unit_price_cents": 7290,
        "member_unit_price_cents": 6190,
        "unit_quote_cents": 7150,
    }


def test_bound_order_facts_are_found_only_with_matching_ownership(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    store.save({
        **record("bound-1", "tenant-1", "2026-08-25T12:00:00+00:00"),
        "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1",
        "order_id": "order-1", "status": "succeeded", "movie": "奥德赛",
        "quote_date": "2026-08-26", "showtime_start": "19:30", "showtime_end": "22:10",
    })

    found = store.find_by_order(
        tenant_id="tenant-1", order_id="order-1", shop_id="shop-1",
        buyer_id="buyer-1", chat_id="chat-1",
    )
    assert found is not None and found["showtime_end"] == "22:10"
    assert store.find_by_order(
        tenant_id="tenant-1", order_id="order-1", shop_id="other",
        buyer_id="buyer-1", chat_id="chat-1",
    ) is None


def test_quote_records_are_encrypted_deduplicated_and_tenant_scoped(tmp_path: Path) -> None:
    path = tmp_path / "quote-records.json"
    store = QuoteRecordStore(path, protector=PlainProtector(), max_records=10)
    store.save(record("quote-1", "tenant-a", "2026-08-25T01:00:00+00:00"))
    store.save(record("quote-2", "tenant-b", "2026-08-25T03:00:00+00:00"))
    store.save({**record("quote-1", "tenant-a", "2026-08-25T04:00:00+00:00"), "unit_quote_cents": 7250})

    assert "测试影城" not in path.read_text(encoding="utf-8")
    stored = store.list("tenant-a")
    assert stored[0]["record_id"] == "quote-1"
    assert stored[0]["unit_quote_cents"] == 7250
    assert stored[0]["original_unit_price_cents"] == 7290
    assert stored[0]["member_unit_price_cents"] == 6190
    assert stored[0]["quote_id"] == "quote-1"
    assert stored[0]["quote_version"].startswith("qv-")
    assert stored[0]["terms_fingerprint"] is None
    assert stored[0]["supersedes_quote_id"] is None
    assert stored[0]["invalidated_reason"] is None
    assert [item["record_id"] for item in store.list("tenant-b")] == ["quote-2"]


def test_quote_confirmation_is_durable_scoped_and_expires(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    store.save({
        **record("quote-a", "tenant-a", "2026-08-25T09:00:00+00:00"),
        "status": "succeeded", "item_id": "item-1", "quote_scope": "area_probe",
        "seat_zone_type": "W+", "unit_quote_cents": 5990,
    })
    assert store.mark_delivered(
        tenant_id="tenant-a", record_id="quote-a",
        delivered_at=datetime(2026, 8, 25, 9, 0, 1, tzinfo=timezone.utc),
        message_id="seller-message-a",
    ) is not None

    confirmed = store.confirm_latest(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", confirmation_id="event-confirm", ticket_count=2,
        confirmed_at=datetime(2026, 8, 25, 9, 5, tzinfo=timezone.utc),
    )

    assert confirmed is not None
    assert confirmed["confirmed_ticket_count"] == 2
    assert confirmed["confirmation_version"].startswith("v4c-")
    repeated = store.confirm_latest(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", confirmation_id="event-confirm-again", ticket_count=None,
        confirmed_at=datetime(2026, 8, 25, 9, 5, 30, tzinfo=timezone.utc),
    )
    assert repeated is not None
    assert repeated["confirmed_ticket_count"] == 2
    bound = store.bind_order(
        record_id="quote-a", tenant_id="tenant-a", shop_id="shop-1",
        buyer_id="buyer-1", chat_id="chat-1", order_id="order-1",
        bound_at=datetime(2026, 8, 25, 9, 6, tzinfo=timezone.utc),
    )
    assert bound is not None
    assert bound["order_id"] == "order-1"
    assert bound["binding_state"] == "order_bound"
    assert store.find_confirmed(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=datetime(2026, 8, 25, 9, 10, tzinfo=timezone.utc),
    )["record_id"] == "quote-a"
    assert store.find_confirmed(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=datetime(2026, 8, 25, 9, 16, tzinfo=timezone.utc),
    ) is None
    assert store.find_confirmed(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="other", at=datetime(2026, 8, 25, 9, 10, tzinfo=timezone.utc),
    ) is None


def test_refunded_order_releases_quote_binding_for_a_replacement_order(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    store.save({
        **record("quote-refunded", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "ticket_count": 1, "unit_quote_cents": 5_080,
    })
    store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-refunded",
        delivered_at=created + timedelta(seconds=3), message_id="seller-message-1",
    )
    assert store.confirm_latest(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", confirmation_id="confirm-1", ticket_count=1,
        confirmed_at=created + timedelta(minutes=1),
    ) is not None
    assert store.bind_order(
        record_id="quote-refunded", tenant_id="tenant-1", shop_id="shop-1",
        buyer_id="buyer-1", chat_id="chat-1", order_id="order-old",
        bound_at=created + timedelta(minutes=2),
    ) is not None

    released = store.release_order_binding(
        record_id="quote-refunded", tenant_id="tenant-1", shop_id="shop-1",
        buyer_id="buyer-1", chat_id="chat-1", order_id="order-old",
        released_at=created + timedelta(minutes=3),
    )

    assert released is not None
    assert released["order_id"] is None
    assert released["binding_state"] == "confirmed"
    assert store.find_confirmed(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(minutes=4),
    )["record_id"] == "quote-refunded"


def test_undelivered_quote_cannot_be_confirmed_or_found_as_confirmed(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    store.save({
        **record("quote-undelivered", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "unit_quote_cents": 5_000,
    })

    assert store.confirm_latest(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", confirmation_id="buyer-confirm", ticket_count=1,
        confirmed_at=created + timedelta(seconds=5),
    ) is None
    assert store.find_confirmed(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(seconds=5),
    ) is None


def test_delivered_quote_can_be_atomically_confirmed_and_bound_by_later_order(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    store.save({
        **record("quote-delivered", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "exact_seats",
        "ticket_count": 2, "total_quote_cents": 8_800, "seat_display": "8排12座、8排13座",
    })
    arguments = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "item_id": "item-1", "order_id": "order-1",
        "order_created_at": created + timedelta(minutes=2), "ticket_count": 2,
        "confirmation_id": "order-event-1",
    }
    assert store.confirm_for_order(**arguments) is None

    delivered = store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-delivered",
        delivered_at=created + timedelta(seconds=3), message_id="seller-message-1",
    )
    assert delivered is not None and delivered["delivery_state"] == "delivered"
    assert store.confirm_for_order(
        **{**arguments, "order_id": "order-early", "order_created_at": created - timedelta(seconds=1)},
    ) is None
    assert store.confirm_for_order(
        **{**arguments, "order_id": "order-wrong-count", "ticket_count": 1},
    ) is None

    confirmed = store.confirm_for_order(**arguments)
    duplicate = store.confirm_for_order(**arguments)

    assert confirmed is not None
    assert duplicate == confirmed
    assert confirmed["confirmation_source"] == "order_created"
    assert confirmed["confirmation_event_id"] == "order-event-1"
    assert confirmed["confirmed_ticket_count"] == 2
    assert confirmed["order_id"] == "order-1"
    assert confirmed["binding_state"] == "order_bound"


def test_later_order_can_supply_count_after_explicit_countless_confirmation(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    store.save({
        **record("quote-explicit", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "unit_quote_cents": 4_400, "ticket_count": None,
        "delivery_state": "delivered", "delivered_at": (created + timedelta(seconds=1)).isoformat(),
        "confirmation_version": "v4c-without-count", "confirmation_source": "buyer_message",
        "confirmation_event_id": "buyer-confirm", "confirmed_ticket_count": None,
        "confirmed_at": (created + timedelta(seconds=10)).isoformat(),
        "quote_expires_at": (created + timedelta(minutes=15)).isoformat(),
    })

    confirmed = store.confirm_for_order(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", order_id="order-1", order_created_at=created + timedelta(minutes=2),
        ticket_count=2, confirmation_id="order-event-1",
    )

    assert confirmed is not None
    assert confirmed["confirmation_source"] == "buyer_message"
    assert confirmed["confirmation_event_id"] == "buyer-confirm"
    assert confirmed["confirmed_ticket_count"] == 2
    assert confirmed["confirmation_version"] != "v4c-without-count"
    assert confirmed["order_id"] == "order-1"


def test_a_later_failed_quote_attempt_preserves_older_delivery(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    store.save({
        **record("quote-old", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "exact_seats",
        "ticket_count": 2, "total_quote_cents": 8_800, "seat_display": "8排12座、8排13座",
        "delivery_state": "delivered", "delivered_at": (created + timedelta(seconds=1)).isoformat(),
    })
    store.save({
        **record("quote-new-failed", "tenant-1", (created + timedelta(minutes=1)).isoformat()),
        "item_id": "item-1", "status": "failed", "failure_reason": "new screenshot ambiguous",
    })

    confirmed = store.confirm_for_order(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", order_id="order-1", order_created_at=created + timedelta(minutes=2),
        ticket_count=2, confirmation_id="order-event-1",
    )

    assert confirmed is not None
    assert confirmed["record_id"] == "quote-old"


def test_failed_attempt_with_same_terms_does_not_discard_delivered_quote(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    terms = {
        "item_id": "item-1", "status": "succeeded", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "unit_quote_cents": 5_000, "city": "广州",
        "cinema": "测试影城", "movie": "奥德赛", "quote_date": "2026-08-26",
        "showtime_start": "19:30", "showtime_end": "22:10", "hall": "IMAX",
        "seat_display": "W+座位",
    }
    store.save({**record("quote-old", "tenant-1", created.isoformat()), **terms})
    store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-old",
        delivered_at=created + timedelta(seconds=1), message_id="seller-old",
    )
    failed = store.save({
        **record("quote-retry", "tenant-1", (created + timedelta(minutes=1)).isoformat()),
        **{key: value for key, value in terms.items() if key not in {"status", "unit_quote_cents"}},
        "status": "failed", "failure_reason": "temporary_provider_error",
    })

    assert failed["terms_fingerprint"] == store.get_record(
        tenant_id="tenant-1", record_id="quote-old",
    )["terms_fingerprint"]
    assert failed["supersedes_quote_id"] is None
    found = store.find_recent(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(minutes=2),
    )
    assert found is not None and found["quote_id"] == "quote-old"


def test_failed_attempt_with_changed_terms_does_not_invalidate_previous_quote(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    old = {
        **record("quote-old", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "exact_seats",
        "city": "广州", "cinema": "测试影城", "movie": "奥德赛",
        "quote_date": "2026-08-26", "showtime_start": "19:30", "hall": "IMAX",
        "seat_display": "8排12座", "ticket_count": 1, "total_quote_cents": 5_000,
    }
    store.save(old)
    store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-old",
        delivered_at=created + timedelta(seconds=1), message_id="seller-old",
    )
    failed = store.save({
        **record("quote-new", "tenant-1", (created + timedelta(minutes=1)).isoformat()),
        "item_id": "item-1", "status": "failed", "city": "广州", "cinema": "测试影城",
        "movie": "奥德赛", "quote_date": "2026-08-26", "showtime_start": "19:30",
        "hall": "IMAX", "seat_display": "9排12座", "quote_scope": "exact_seats",
        "failure_reason": "selected_seat_unavailable",
    })

    assert failed["supersedes_quote_id"] is None
    assert store.get_record(tenant_id="tenant-1", record_id="quote-old")["invalidated_reason"] is None
    found = store.find_recent(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(minutes=2),
    )
    assert found is not None and found["record_id"] == "quote-old"


def test_confirmed_quote_survives_same_terms_replacement_race(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    common = {
        **record("quote-old", "tenant-1", created.isoformat()),
        "item_id": "item-1", "status": "succeeded", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "ticket_count": None, "unit_quote_cents": 5_640,
        "total_quote_cents": None, "delivery_state": "delivered",
        "delivered_at": (created + timedelta(seconds=1)).isoformat(),
    }
    store.save(common)
    confirmed = store.confirm_latest(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", confirmation_id="buyer-confirmation", ticket_count=3,
        confirmed_at=created + timedelta(seconds=2),
    )
    assert confirmed is not None and confirmed["record_id"] == "quote-old"

    replacement = store.save({
        **common, "record_id": "quote-new",
        "created_at": (created + timedelta(seconds=3)).isoformat(),
        "delivery_state": None, "delivered_at": None,
    })
    assert replacement["supersedes_quote_id"] == "quote-old"
    store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-new",
        delivered_at=created + timedelta(seconds=4), message_id="seller-new",
    )

    found = store.find_confirmed(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(seconds=5),
    )
    assert found is not None and found["record_id"] == "quote-old"


def test_successful_replacement_waits_for_delivery_before_invalidating_old_quote(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
    common = {
        "item_id": "item-1", "status": "succeeded", "quote_scope": "exact_seats",
        "city": "广州", "cinema": "测试影城", "movie": "奥德赛",
        "quote_date": "2026-08-26", "showtime_start": "19:30", "showtime_end": "22:10",
        "hall": "IMAX", "seat_display": "8排12座", "ticket_count": 1,
        "total_quote_cents": 5_000,
    }
    store.save({**record("quote-old", "tenant-1", created.isoformat()), **common})
    store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-old",
        delivered_at=created + timedelta(seconds=1), message_id="seller-old",
    )
    replacement = store.save({
        **record("quote-new", "tenant-1", (created + timedelta(minutes=1)).isoformat()),
        **{**common, "seat_display": "8排13座", "total_quote_cents": 5_100},
    })

    assert replacement["supersedes_quote_id"] == "quote-old"
    assert store.get_record(tenant_id="tenant-1", record_id="quote-old")["invalidated_reason"] is None
    found_before_delivery = store.find_recent(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(minutes=2),
    )
    assert found_before_delivery is not None and found_before_delivery["record_id"] == "quote-old"

    delivered = store.mark_delivered(
        tenant_id="tenant-1", record_id="quote-new",
        delivered_at=created + timedelta(minutes=1, seconds=1), message_id="seller-new",
    )
    assert delivered is not None
    assert store.get_record(tenant_id="tenant-1", record_id="quote-old")["invalidated_reason"] == "superseded_by_new_quote"
    found_after_delivery = store.find_recent(
        tenant_id="tenant-1", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id="item-1", at=created + timedelta(minutes=2),
    )
    assert found_after_delivery is not None and found_after_delivery["record_id"] == "quote-new"


def test_agent_quote_records_are_delivered_with_the_final_event_message(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime.now(timezone.utc)
    store.save({
        **record("event-1:agent:abc", "tenant-a", created.isoformat()),
        "status": "succeeded", "quote_scope": "exact_seats", "total_quote_cents": 5_800,
    })

    changed = store.mark_event_quotes_delivered(
        tenant_id="tenant-a", event_id="event-1", delivered_at=created, message_id="sent-1",
    )

    assert changed == 1
    saved = store.get_record(tenant_id="tenant-a", record_id="event-1:agent:abc")
    assert saved["delivery_state"] == "delivered"
    assert saved["delivery_message_id"] == "sent-1"


def test_select_offer_is_tenant_scoped_and_does_not_change_quote_amount(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime.now(timezone.utc)
    saved = store.save({
        **record("quote-offers", "tenant-a", created.isoformat()),
        "offers": [
            {"offer_id": "standard", "total_quote_cents": 5_800, "price_mode": "LIMIT"},
            {"offer_id": "fast", "total_quote_cents": 6_800, "price_mode": "FIXED"},
        ],
        "quote_expires_at": (created + timedelta(minutes=10)).isoformat(),
        "total_quote_cents": 5_800,
    })

    selected = store.select_offer(
        tenant_id="tenant-a", record_id=saved["record_id"], offer_id="fast",
        selection_source="buyer_message",
    )

    assert selected is not None
    assert selected["selected_offer_id"] == "fast"
    assert selected["selected_offer"]["total_quote_cents"] == 6_800
    assert selected["selection_source"] == "buyer_message"
    assert selected["total_quote_cents"] == 5_800
    assert store.select_offer(
        tenant_id="tenant-b", record_id=saved["record_id"], offer_id="standard",
    ) is None


def test_authoritative_offer_refresh_replaces_selected_lineage_and_clears_old_confirmation(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    created = datetime.now(timezone.utc)
    saved = store.save({
        **record("quote-refresh", "tenant-a", created.isoformat()),
        "offers": [
            {"offer_id": "standard", "total_quote_cents": 5_800, "price_mode": "LIMIT"},
            {"offer_id": "fixed", "total_quote_cents": 6_800, "price_mode": "FIXED"},
        ],
        "quote_expires_at": (created + timedelta(minutes=10)).isoformat(),
        "total_quote_cents": 5_800,
        "confirmation_id": "old-confirmation", "confirmed_at": created.isoformat(),
    })
    refreshed_expiry = created + timedelta(minutes=20)

    selected = store.select_offer(
        tenant_id="tenant-a", record_id=saved["record_id"], offer_id="fixed",
        selection_source="operator_panel",
        authoritative_offer={
            "total_quote_cents": 6_900, "quote_id": "lpq-new", "quote_hash": "b" * 64,
            "generation": 2, "quote_expires_at": refreshed_expiry.isoformat(),
            "price_mode": "FIXED", "preflight_verified": True,
        },
    )

    assert selected["selected_offer"]["total_quote_cents"] == 6_900
    assert selected["selected_offer"]["quote_id"] == "lpq-new"
    assert selected["selected_offer"]["generation"] == 2
    assert selected["total_quote_cents"] == 5_800
    assert selected.get("confirmation_id") is None
    assert selected["quote_expires_at"] == refreshed_expiry.isoformat()


def test_refreshed_offer_uses_selection_time_for_confirmation_ttl(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quotes.json", protector=PlainProtector())
    now = datetime.now(timezone.utc)
    saved = store.save({
        **record("quote-old-refreshed", "tenant-a", (now - timedelta(minutes=20)).isoformat()),
        "status": "succeeded", "delivery_state": "delivered", "quote_scope": "exact_seats",
        "seat_display": "5排6座", "ticket_count": 1,
        "offers": [
            {"offer_id": "standard", "total_quote_cents": 5_800, "price_mode": "LIMIT"},
            {"offer_id": "fixed", "total_quote_cents": 6_800, "price_mode": "FIXED"},
        ],
        "quote_expires_at": (now + timedelta(minutes=5)).isoformat(),
    })
    store.select_offer(
        tenant_id="tenant-a", record_id=saved["record_id"], offer_id="fixed",
        selection_source="operator_panel", selected_at=now,
        authoritative_offer={
            "total_quote_cents": 6_900, "quote_id": "lpq-new", "quote_hash": "d" * 64,
            "generation": 2, "quote_expires_at": (now + timedelta(minutes=10)).isoformat(),
            "price_mode": "FIXED", "preflight_verified": True,
        },
    )

    confirmed = store.confirm_latest(
        tenant_id="tenant-a", shop_id="shop-1", buyer_id="buyer-1", chat_id="chat-1",
        item_id=None, confirmation_id="new-confirmation", ticket_count=1, confirmed_at=now,
    )

    assert confirmed is not None
    assert confirmed["confirmation_id"] == "new-confirmation"
    assert confirmed["quote_expires_at"] == (now + timedelta(minutes=10)).isoformat()


def test_select_offer_api_repreflights_liangpiao_offer_before_selection(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    created = datetime.now(timezone.utc)
    store.save({
        **record("quote-preflight", "tenant-a", created.isoformat()),
        "shop_id": "shop-a", "chat_id": "chat-a", "quote_route": "liangpiao_exact",
        "cinema_id": 1001, "show_id": "show-1", "movie": "电影",
        "quote_date": "2026-09-02", "showtime_start": "19:30", "hall": "1号厅",
        "selected_seats": [{"row_no": 5, "col_no": 6, "seat_no": "5排6座", "area_id": "a"}],
        "quote_generation": 1, "confirmation_id": "old-confirmation",
        "offers": [
            {"offer_id": "standard", "total_quote_cents": 5_800, "price_mode": "LIMIT", "ticket_mode": "STANDARD"},
            {"offer_id": "fixed", "total_quote_cents": 6_800, "price_mode": "FIXED", "ticket_mode": "STANDARD"},
        ],
        "quote_expires_at": (created + timedelta(minutes=10)).isoformat(),
        "total_quote_cents": 5_800,
    })

    class PreflightService:
        def __init__(self) -> None:
            self.requests = []

        async def quote(self, request):
            self.requests.append(request)
            return {
                "quote_id": "lpq-refreshed", "quote_hash": "c" * 64,
                "price_mode": "FIXED", "seats": [{"row_no": 5, "col_no": 6}],
                "buyer_amount_fen": 6_900, "provider_amount_fen": 6_500,
                "max_price_fen": 6_900, "pricing_rule_version": "rules-2",
                "expires_at": (created + timedelta(minutes=20)).isoformat(),
                "preflight_verified": True, "generation": 2,
            }

    preflight = PreflightService()
    app = create_app(
        service=UnusedRecognitionService(), quote_record_store=store,
        selected_seat_quote_service=preflight,
    )
    with TestClient(app) as client:
        response = client.put(
            "/api/plugin/quote-records/quote-preflight/selected-offer",
            headers={"x-wanda-tenant-id": "tenant-a"}, json={"offer_id": "fixed"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["selected_offer"]["total_quote_cents"] == 6_900
    assert body["selected_offer"]["quote_id"] == "lpq-refreshed"
    assert body.get("confirmation_id") is None
    assert preflight.requests[0].price_mode == "FIXED"
    assert preflight.requests[0].generation == 2


def test_select_offer_api_returns_conflict_for_unknown_offer(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    created = datetime.now(timezone.utc)
    store.save({
        **record("quote-api", "tenant-a", created.isoformat()),
        "offers": [{"offer_id": "standard", "total_quote_cents": 5_800}],
        "quote_expires_at": (created + timedelta(minutes=10)).isoformat(),
        "total_quote_cents": 5_800,
    })
    app = create_app(service=UnusedRecognitionService(), quote_record_store=store)

    with TestClient(app) as client:
        response = client.put(
            "/api/plugin/quote-records/quote-api/selected-offer",
            headers={"x-wanda-tenant-id": "tenant-a"},
            json={"offer_id": "unknown"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "quote_offer_unavailable"


def test_quote_records_api_requires_and_filters_panel_tenant(tmp_path: Path) -> None:
    store = QuoteRecordStore(tmp_path / "quote-records.json", protector=PlainProtector())
    store.save(record("quote-a", "tenant-a", "2026-08-25T01:00:00+00:00"))
    store.save(record("quote-b", "tenant-b", "2026-08-25T02:00:00+00:00"))
    app = create_app(service=UnusedRecognitionService(), quote_record_store=store)

    with TestClient(app) as client:
        assert client.get("/api/plugin/quote-records").status_code == 401
        response = client.get("/api/plugin/quote-records", headers={"x-wanda-tenant-id": "tenant-a"})

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["records"][0]["record_id"] == "quote-a"
