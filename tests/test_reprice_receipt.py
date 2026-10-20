"""Confirmed reprice receipts must reach exactly one buyer notice."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from application import plugin_api, plugin_runtime, store as app_store
import templates
from integrations.yumaiduo.commands import Command, CommandStore


class RepriceReceiptTest(unittest.TestCase):
    def setUp(self):
        self.store = CommandStore(":memory:")
        self.command = Command(
            command_id="reprice-1", type="CHANGE_ORDER_PRICE",
            status="CLAIMED", event_id="3316946449037036559",
            tenant_id="107", shop_id="2313315754",
            buyer_id="2464035965", chat_id="66050332180",
            dedupe_key="reprice:3316946449037036559",
            extra={"order_id": "3316946449037036559",
                   "target_amount_fen": 2460})
        self.store.enqueue(self.command)
        self.order = SimpleNamespace(
            provider_order_id=self.command.event_id,
            tenant_id=self.command.tenant_id, shop_id=self.command.shop_id,
            buyer_id=self.command.buyer_id, chat_id=self.command.chat_id,
            amount_fen=2460, quote_id="quote-1")
        self.quote = SimpleNamespace(
            quote_id="quote-1", status="READY", total_sell_price_fen=2460,
            tenant_id=self.command.tenant_id, shop_id=self.command.shop_id,
            buyer_id=self.command.buyer_id, chat_id=self.command.chat_id,
            cinema="UME影城", movie="神探之痕迹", show="2026-10-17 11:40",
            selected_seats=["H排11座"], provider="LIANGPIAO",
            metadata={"city_name": "重庆", "hall_name": "3号厅",
                      "cinema_address": "商场6楼"})

    def tearDown(self):
        self.store.close()

    def test_success_is_acknowledged_and_notice_is_deduplicated(self):
        stores = SimpleNamespace(
            orders=SimpleNamespace(find_by_provider_order=lambda _: self.order),
            quotes=SimpleNamespace(get=lambda _: self.quote))
        body = {"status": "SUCCEEDED", "price_after_fen": 2460,
                "provider_result_status": "succeeded"}
        with (patch.object(plugin_api, "queue", return_value=self.store),
              patch.object(plugin_runtime, "queue", return_value=self.store),
              patch("application.ledger.stores", return_value=stores),
              patch.object(app_store, "shop_send_enabled", return_value=True)):
            first = plugin_api.handle_result(self.command.command_id, body)
            second = plugin_api.handle_result(self.command.command_id, body)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["price_after_fen"], 2460)
        self.assertEqual(second["status"], "SUCCEEDED")
        notices = [c for c in self.store.by_status("PENDING")
                   if c.type == "SEND_MESSAGE"]
        self.assertEqual(len(notices), 1)  # full template and QA text stay together
        self.assertIn("付款后不支持退改签", notices[0].text)
        self.assertIn("H排11座", notices[0].text)
        self.assertEqual(notices[0].extra["history_message_ids"], [])

    def test_revision_uses_existing_again_template_and_preserves_empty(self):
        self.command.extra["replacement_quote_id"] = self.quote.quote_id
        original = templates.text_for
        with patch.object(templates, "text_for", wraps=original) as selected:
            notices = plugin_runtime.build_reprice_success_commands(
                self.command, self.order, self.quote, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("已为您重新改价", notices[0].text)
        self.assertIn("24.60", notices[0].text)
        self.assertNotIn("dh17o4vx6ew", notices[0].text)
        self.assertIn(unittest.mock.call("REPRICED_AGAIN", "liangpiao"), selected.call_args_list)
        with patch.object(templates, "text_for", return_value=""):
            self.assertEqual(plugin_runtime.build_reprice_success_commands(
                self.command, self.order, self.quote, []), [])

    def test_mismatched_amount_never_becomes_success(self):
        result = self.store.record_result(
            self.command.command_id, status="SUCCEEDED",
            price_after_fen=2000)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.failure_reason, "REPRICE_RESULT_AMOUNT_MISMATCH")

    def test_missing_display_facts_use_factual_short_notice(self):
        self.quote.metadata = {}
        notices = plugin_runtime.build_reprice_success_commands(
            self.command, self.order, self.quote, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("24.60 元", notices[0].text)
        self.assertIn("付款后不支持退改签", notices[0].text)
        self.assertNotIn("{", notices[0].text)

    def test_expired_quote_never_invites_payment(self):
        self.quote.status = "EXPIRED"
        notices = plugin_runtime.build_reprice_success_commands(
            self.command, self.order, self.quote, [])
        self.assertEqual(notices, [])
        self.quote.status = "READY"
        self.quote.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        notices = plugin_runtime.build_reprice_success_commands(
            self.command, self.order, self.quote, [])
        self.assertEqual(notices, [])

    def test_empty_notice_suppresses_even_missing_metadata_fallback(self):
        row = templates.find("REPRICED_CHECK")
        with patch.dict(row, {"channel_text": {"liangpiao": ""}}):
            self.assertEqual(plugin_runtime.build_reprice_success_commands(
                self.command, self.order, self.quote, []), [])
            self.quote.metadata = {}
            self.assertEqual(plugin_runtime.build_reprice_success_commands(
                self.command, self.order, self.quote, []), [])

    def test_empty_payment_template_does_not_trigger_hardcoded_fallback(self):
        self.order.payload, self.order.order_id = {}, "local-order"
        stores = SimpleNamespace(orders=SimpleNamespace(find_by_provider_order=lambda _: self.order))
        conv = SimpleNamespace(channel="wanda")
        svc = SimpleNamespace(session=lambda _: SimpleNamespace(conversation=conv),
                              flow=SimpleNamespace(on_payment_confirmed=lambda c, out, **kw: out.add("TICKETING")))
        event = {"tenantId": "107", "payload": {"chatId": "66050332180", "peerUnb": "2464035965",
                 "accountUnb": "2313315754", "orderId": self.command.event_id},
                 "_order_detail": {"payment": "2460", "orderStatus": 2}}
        with (patch.dict(templates.find("TICKETING"), {"channel_text": {"wanda": ""}}),
              patch("application.ledger.stores", return_value=stores),
              patch("application.ledger.confirm_payment", return_value={"ok": True}),
              patch.object(plugin_runtime, "get_local_service", return_value=svc),
              patch.object(plugin_runtime, "_sent_message_ids", return_value=[]),
              patch.object(plugin_runtime, "_shop_allows_send", return_value=True)):
            commands = plugin_runtime._from_order_paid(event)
        self.assertTrue(commands)
        self.assertTrue(all(not c.text for c in commands))


if __name__ == "__main__":
    unittest.main()
