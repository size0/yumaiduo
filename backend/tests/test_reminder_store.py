from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.reminder_store import ReminderSettings, ReminderStore
from app.reply_template_store import ReplyTemplates


class PlainProtector:
    def protect(self, value: str) -> str:
        return value

    def unprotect(self, value: str) -> str:
        return value


def store(tmp_path: Path) -> ReminderStore:
    return ReminderStore(tmp_path / "reminders.json", protector=PlainProtector())


def facts(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1",
        "chat_id": "chat-1", "order_id": "order-1", "movie": "奥德赛",
        "cinema": "济南魏家庄万达广场店", "quote_date": "2026-08-26",
        "showtime_start": "23:20", "showtime_end": "02:10", "hall": "4号IMAX厅",
        "seat_display": "8排7座、8排8座",
    }
    value.update(overrides)
    return value


def test_disabled_reminders_create_no_tasks(tmp_path: Path) -> None:
    reminders = store(tmp_path)

    assert reminders.settings() == ReminderSettings()
    assert reminders.plan_order(facts(), now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc)) == []


def test_planning_creates_idempotent_pre_show_and_cross_midnight_post_show_tasks(tmp_path: Path) -> None:
    reminders = store(tmp_path)
    reminders.save_settings({
        "enabled": True, "pre_show_minutes": 5, "post_show_minutes": 5,
        "pre_show_template": "观影提醒：{movie} 将于 {showtime} 放映，影院：{cinema}",
        "post_show_template": "",
    })
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)

    first = reminders.plan_order(facts(), now=now)
    second = reminders.plan_order(facts(), now=now)

    assert len(first) == 2
    assert [task["kind"] for task in first] == ["pre_show_text", "post_show_receipt"]
    assert first[0]["due_at"] == "2026-08-26T15:15:00+00:00"
    assert first[0]["expires_at"] == "2026-08-26T15:20:00+00:00"
    assert first[0]["message"] == "观影提醒：奥德赛 将于 23:20 放映，影院：济南魏家庄万达广场店"
    assert first[1]["due_at"] == "2026-08-26T18:15:00+00:00"
    assert {task["task_id"] for task in second} == {task["task_id"] for task in first}
    assert len(reminders.list("tenant-1")) == 2


def test_reply_template_can_supply_movie_reminder_message(tmp_path: Path) -> None:
    reminders = store(tmp_path)
    reminders.save_settings({"enabled": True})
    templates = ReplyTemplates(movie_reminder_template="提醒：{movie} {showtime} 到场")

    tasks = reminders.plan_order(
        facts(showtime_end=None),
        now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
        pre_show_template=templates.movie_reminder_template,
    )

    assert tasks[0]["message"] == "提醒：奥德赛 23:20 到场"


def test_missing_authoritative_end_time_only_prepares_opening_reminder(tmp_path: Path) -> None:
    reminders = store(tmp_path)
    reminders.save_settings({"enabled": True})

    tasks = reminders.plan_order(facts(showtime_end=None), now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc))

    assert [task["kind"] for task in tasks] == ["pre_show_text"]


def test_due_task_claim_completion_and_lease_are_idempotent(tmp_path: Path) -> None:
    reminders = store(tmp_path)
    reminders.save_settings({"enabled": True})
    reminders.plan_order(facts(), now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc))
    due = datetime(2026, 8, 26, 15, 16, tzinfo=timezone.utc)

    claimed = reminders.claim_due(now=due, limit=10)
    assert len(claimed) == 1
    assert reminders.claim_due(now=due, limit=10) == []
    task = claimed[0]
    assert reminders.complete(task["task_id"], task["lease_token"], {"status": "sent"}) is True
    assert reminders.complete(task["task_id"], task["lease_token"], {"status": "sent"}) is True
    saved = next(item for item in reminders.list("tenant-1") if item["task_id"] == task["task_id"])
    assert saved["status"] == "completed"


def test_unsupported_template_variable_is_rejected(tmp_path: Path) -> None:
    reminders = store(tmp_path)

    with pytest.raises(ValueError, match="unsupported_reminder_template_variable"):
        reminders.save_settings({"pre_show_template": "价格是{price}"})
