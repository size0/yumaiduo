from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import MovieImageInfo
from app.reminder_store import ReminderStore


class Recognition:
    async def recognize(self, image: bytes, content_type: str, **kwargs) -> MovieImageInfo:
        raise AssertionError("not used")


class PlainProtector:
    def protect(self, value: str) -> str: return value
    def unprotect(self, value: str) -> str: return value


def test_reminder_settings_panel_listing_and_plugin_claim_flow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WANDA_AI_V2_BRIDGE_KEY", "bridge-secret")
    reminders = ReminderStore(tmp_path / "reminders.json", protector=PlainProtector())
    client = TestClient(create_app(service=Recognition(), reminder_store=reminders))

    saved = client.put("/api/settings/reminders", json={
        "enabled": True, "pre_show_minutes": 5, "post_show_minutes": 5,
        "pre_show_template": "{movie} {showtime}",
    })
    assert saved.status_code == 200 and saved.json()["enabled"] is True
    reminders.plan_order({
        "tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1",
        "order_id": "order-1", "movie": "奥德赛", "cinema": "测试影院", "quote_date": "2026-08-26",
        "showtime_start": "19:30", "showtime_end": "22:10", "hall": "IMAX", "seat_display": "W+",
    }, now=datetime(2026, 8, 25, 12, tzinfo=timezone.utc))

    listed = client.get("/api/plugin/reminders", headers={"x-wanda-tenant-id": "tenant-1"})
    assert listed.status_code == 200 and listed.json()["count"] == 2
    claimed = client.post(
        "/api/wanda-ai-v2/plugin/reminders/claim",
        headers={"x-wanda-ai-v2-bridge-key": "bridge-secret"},
        json={"now": "2026-08-26T11:26:00+00:00", "limit": 10},
    )
    task = claimed.json()["tasks"][0]
    completed = client.post(
        f'/api/wanda-ai-v2/plugin/reminders/{task["task_id"]}/complete',
        headers={"x-wanda-ai-v2-bridge-key": "bridge-secret"},
        json={"lease_token": task["lease_token"], "result": {"status": "sent"}},
    )
    assert completed.status_code == 200
