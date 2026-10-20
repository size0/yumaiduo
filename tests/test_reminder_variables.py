"""Reminder command text must contain order facts, not settings tokens."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings
from application.reminders import build_reminder_commands, render_reminder_message


class ReminderVariablesTests(unittest.TestCase):
    def test_configured_urge_order_command(self):
        now = dt.datetime(2026, 10, 15, 20, 0).astimezone()
        cfg = settings.Settings()
        cfg.urge_order.delay_minutes = 30
        order = dict(order_id="test", chat_id="chat", state="AWAITING_PAYMENT",
                     payment_state="UNPAID", cinema="测试万达影城", movie="测试影片",
                     show="2026-10-16 19:30", created_at=(now - dt.timedelta(minutes=31)).isoformat())
        commands = build_reminder_commands(now=now, orders=[order], cfg=cfg)
        reminder = next(c for c in commands if c.extra["reminder"] == "urge_order")
        self.assertEqual(reminder.text,
                         "您查询的在测试万达影城于2026-10-16 19:30放映的影片测试影片，"
                         "优惠购票通道即将关闭，如需观影请尽快按提示下单")
        del order["movie"]
        self.assertFalse(any(c.extra["reminder"] == "urge_order" for c in
                             build_reminder_commands(now=now, orders=[order], cfg=cfg)))

    def test_emoji_custom_text_and_single_pass(self):
        self.assertEqual(render_reminder_message("【[影片名]】[大哭]", {"movie": "[影院名]"}),
                         "【[影院名]】[大哭]")
        self.assertEqual(render_reminder_message("我的新文案[送花]", {}), "我的新文案[送花]")

    def test_optional_tokens_require_real_facts(self):
        self.assertEqual(render_reminder_message("[座位编号]", {"seats": ["1排1座", "1排2座"]}),
                         "1排1座、1排2座")
        for token in ("场次名称", "影院地址", "粉丝昵称"):
            self.assertIsNone(render_reminder_message(f"[{token}]", {}))


if __name__ == "__main__":
    unittest.main()
