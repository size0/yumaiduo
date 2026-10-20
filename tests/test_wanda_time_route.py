"""Supplier routing must precede Liangpiao timing gates (offline)."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from application.ticket_flow import TicketFlow, FlowResult


class RouteTimingTests(unittest.TestCase):
    def run_quote(self, route, minutes):
        flow = TicketFlow.__new__(TicketFlow)
        flow._resolve_channel = Mock(return_value=route)
        flow._quote_via_wanda = Mock(return_value="wanda")
        flow._quote_via_liangpiao = Mock(return_value="liangpiao")
        flow._pick_ticket_mode = Mock(return_value="STANDARD")
        result = SimpleNamespace(start_time=(datetime.now() + timedelta(minutes=minutes)).isoformat(), seats=[])
        out = FlowResult()
        out.add = Mock()
        cfg = SimpleNamespace(showtime_limit=SimpleNamespace(remaining_minutes=40), speed_tiers=lambda: [])
        with patch("application.ticket_flow._fallback_quote_enabled", return_value=True), patch("settings.load", return_value=cfg):
            actual = flow._build_quote(SimpleNamespace(), result, out)
        return flow, out, actual

    def test_wanda_near_start_skips_liangpiao_gate_and_tier(self):
        flow, out, actual = self.run_quote(object(), 5)
        self.assertEqual(actual, "wanda")
        flow._quote_via_wanda.assert_called_once()
        flow._quote_via_liangpiao.assert_not_called()
        flow._pick_ticket_mode.assert_not_called()
        out.add.assert_not_called()

    def test_liangpiao_near_start_remains_blocked(self):
        flow, out, actual = self.run_quote(None, 5)
        self.assertIs(actual, out)
        self.assertFalse(out.ok)
        out.add.assert_called_once_with("SHOWTIME_LIMIT", {"limit_minutes": "40"})
        flow._quote_via_liangpiao.assert_not_called()

    def test_liangpiao_under_40_minutes_uses_fast_when_enabled(self):
        flow = TicketFlow.__new__(TicketFlow)
        flow._resolve_channel = Mock(return_value=None)
        flow._quote_via_wanda = Mock(return_value="wanda")
        flow._quote_via_liangpiao = Mock(return_value="liangpiao")
        flow._pick_ticket_mode = Mock(return_value="FAST")
        result = SimpleNamespace(start_time=(datetime.now() + timedelta(minutes=30)).isoformat(), seats=[])
        out = FlowResult(); out.add = Mock()
        cfg = SimpleNamespace(showtime_limit=SimpleNamespace(remaining_minutes=40), speed_tiers=lambda: [("FAST", 15)])
        with patch("application.ticket_flow._fallback_quote_enabled", return_value=True), patch("settings.load", return_value=cfg):
            actual = flow._build_quote(SimpleNamespace(), result, out)
        self.assertEqual(actual, "liangpiao")
        flow._quote_via_liangpiao.assert_called_once()
        out.add.assert_not_called()

    def test_liangpiao_later_show_still_quotes(self):
        flow, out, actual = self.run_quote(None, 90)
        self.assertEqual(actual, "liangpiao")
        flow._quote_via_liangpiao.assert_called_once()
        flow._quote_via_wanda.assert_not_called()


if __name__ == "__main__":
    unittest.main()
