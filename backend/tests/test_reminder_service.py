from __future__ import annotations

from datetime import datetime, timezone

from app.reminder_service import plan_shipped_order_reminders


class Quotes:
    def find_by_order(self, **bindings):
        assert bindings == {
            "tenant_id": "tenant-1", "order_id": "order-1", "shop_id": "shop-1",
            "buyer_id": "buyer-1", "chat_id": "chat-1",
        }
        return {
            **bindings, "movie": "奥德赛", "cinema": "济南魏家庄万达广场店",
            "quote_date": "2026-08-26", "showtime_start": "19:30", "showtime_end": "22:10",
            "hall": "4号IMAX厅", "seat_display": "8排7座、8排8座",
        }


class Reminders:
    def __init__(self): self.facts = None
    def plan_order(self, facts, *, now):
        self.facts = facts
        assert now == datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
        return [{"task_id": "rem-1"}]


def body(status=3):
    return {
        "envelope": {"tenantId": "tenant-1", "event": "order.shipped", "timestamp": 1787659200000, "payload": {"orderId": "order-1", "accountUnb": "shop-1"}},
        "session": {"accountUnb": "shop-1", "peerUnb": "buyer-1", "chatId": "chat-1"},
        "order": {"orderId": "order-1", "accountUnb": "shop-1", "orderStatus": status},
    }


def test_authoritative_shipped_event_plans_from_durably_bound_quote_facts() -> None:
    reminders = Reminders()

    tasks = plan_shipped_order_reminders(
        body(), quote_store=Quotes(), reminder_store=reminders,
        now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
    )

    assert tasks == [{"task_id": "rem-1"}]
    assert reminders.facts["showtime_end"] == "22:10"
    assert reminders.facts["buyer_id"] == "buyer-1"


def test_non_shipped_or_unbound_event_creates_no_tasks() -> None:
    assert plan_shipped_order_reminders(body(status=2), quote_store=Quotes(), reminder_store=Reminders()) == []
