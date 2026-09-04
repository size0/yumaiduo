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
