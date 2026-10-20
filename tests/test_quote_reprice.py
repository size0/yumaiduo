"""New screenshot replaces an unpaid order only after verified repricing."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent

from application import ledger, plugin_api, plugin_authorize, plugin_runtime
from application.quote_reprice import build_quote_reprice, authorize_quote_revision, settle_quote_revision
from domain.order import Order, OrderStore
from domain.quote import QuoteRecord, QuoteStore
from domain.conversation import Conversation, ConvState
from integrations.yumaiduo.commands import CommandStore


class QuoteRepriceTests(unittest.TestCase):
    def setUp(self):
        self.ident = dict(tenant_id="t", shop_id="s", buyer_id="b", chat_id="c")
        self.stores = SimpleNamespace(orders=OrderStore(), quotes=QuoteStore())
        self.commands = CommandStore()
        self.order = Order(order_id="o", quote_id="old", idempotency_key="key",
                           provider="LIANGPIAO", provider_order_id="platform",
                           amount_fen=2460, movie="旧电影", state="AWAITING_PAYMENT",
                           payload={"upstream_created": False}, **self.ident)
        self.stores.orders.create(self.order)
        self.quote = QuoteRecord(quote_id="new", provider="LIANGPIAO", movie="新电影",
                                 cinema="影院", show="2026-10-17 15:20", selected_seats=["F排6座"],
                                 ticket_count=1, total_sell_price_fen=1850,
                                 expires_at=datetime.now(timezone.utc)+timedelta(minutes=10),
                                 metadata={"price_mode": "FIXED"}, **self.ident)
        self.stores.quotes.create(self.quote)

    def tearDown(self):
        self.stores.orders.close()
        self.stores.quotes.close()
        self.commands.close()

    def command(self):
        return build_quote_reprice(self.stores, self.ident, "new")[0]

    def test_reserve_preserves_old_facts_then_success_updates_sql_and_json(self):
        cmd = self.command()
        authorize_quote_revision(self.stores, cmd)
        old = self.stores.orders.get("o")
        self.assertEqual((old.quote_id, old.amount_fen, old.movie), ("old", 2460, "旧电影"))
        self.assertEqual(old.state, "REPRICE_PENDING")
        self.assertEqual(build_quote_reprice(self.stores, self.ident, "new"), [])
        with patch.object(ledger, "stores", return_value=self.stores):
            self.assertEqual(ledger.confirm_payment("o")["error"], "REPRICE_REVISION_PENDING")
        cmd.status = "SUCCEEDED"
        cmd.extra["price_after_fen"] = 1850
        settle_quote_revision(self.stores, cmd)
        settle_quote_revision(self.stores, cmd)
        new = self.stores.orders.get("o")
        self.assertEqual((new.quote_id, new.amount_fen, new.movie, new.seats),
                         ("new", 1850, "新电影", ["F排6座"]))
        self.assertEqual(self.stores.orders.find_by_quote("new").order_id, "o")
        self.assertIsNone(self.stores.orders.find_by_quote("old"))
        self.assertEqual(len(new.payload["quote_revisions"]), 1)

    def test_unknown_and_refusal_never_commit_new_facts(self):
        cmd = self.command()
        authorize_quote_revision(self.stores, cmd)
        for status in ("UNKNOWN", "FAILED", "GATE_REJECTED"):
            cmd.status = status
            with self.assertRaisesRegex(ValueError, "UNVERIFIED"):
                settle_quote_revision(self.stores, cmd)
            self.assertEqual(self.stores.orders.get("o").amount_fen, 2460)

    def test_paid_upstream_unknown_and_ambiguous_orders_do_not_plan(self):
        for changes in ({"payment_state": "CONFIRMED"}, {"state": "CREATING"},
                        {"failure_code": "RECOVERY_REQUIRED"},
                        {"payload": {"upstream_created": True}}, {"payload": {}}):
            order = self.stores.orders.get("o")
            for key, value in changes.items():
                setattr(order, key, value)
            self.stores.orders.put(order)
            self.assertFalse(build_quote_reprice(self.stores, self.ident, "new"))
            self.stores.orders.put(self.order)
        other = Order.from_dict(self.order.to_dict())
        other.order_id, other.quote_id, other.idempotency_key = "o2", "other", "key2"
        self.stores.orders.create(other)
        self.assertFalse(build_quote_reprice(self.stores, self.ident, "new"))

    def test_expired_and_wrong_identity_and_third_quote_are_rejected(self):
        cmd = self.command()
        self.quote.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.stores.quotes.put(self.quote)
        self.assertFalse(build_quote_reprice(self.stores, self.ident, "new"))
        with self.assertRaises(ValueError):
            authorize_quote_revision(self.stores, cmd)
        self.quote.expires_at = datetime.now(timezone.utc)+timedelta(minutes=10)
        self.stores.quotes.put(self.quote)
        authorize_quote_revision(self.stores, cmd)
        third = QuoteRecord.from_dict(self.quote.to_dict())
        third.quote_id = "third"
        third.created_at = datetime.now(timezone.utc)+timedelta(seconds=1)
        self.stores.quotes.create(third)
        another = type(cmd).from_dict(cmd.to_dict())
        another.command_id = "another"
        another.extra["replacement_quote_id"] = "third"
        with self.assertRaisesRegex(ValueError, "IN_PROGRESS"):
            authorize_quote_revision(self.stores, another)
        cmd.status, cmd.extra["price_after_fen"] = "SUCCEEDED", 1850
        settle_quote_revision(self.stores, cmd)
        self.assertEqual(self.stores.orders.get("o").quote_id, "new")

    def test_unconfirmed_revision_receipt_is_factual_and_deduplicated(self):
        cmd = self.command()
        cmd.status = "CLAIMED"
        self.commands.enqueue(cmd)
        authorize_quote_revision(self.stores, cmd)
        with (patch.object(plugin_api, "queue", return_value=self.commands),
              patch.object(ledger, "stores", return_value=self.stores),
              patch.object(plugin_runtime, "_sent_message_ids", return_value=[]),
              patch("application.store.shop_send_enabled", return_value=True)):
            for _ in range(2):
                plugin_api.handle_result(cmd.command_id, {"status": "UNKNOWN"})
        notices = [c for c in self.commands.by_status("PENDING") if c.type == "SEND_MESSAGE"]
        self.assertEqual(len(notices), 1)
        self.assertIn("尚未确认完成", notices[0].text)
        self.assertNotIn("已为您重新改价", notices[0].text)
        self.assertEqual(self.stores.orders.get("o").quote_id, "old")
        import templates
        cmd.status = "FAILED"
        with patch.object(templates, "text_for", return_value=""):
            self.assertEqual(plugin_runtime.build_revision_unconfirmed_commands(
                cmd, self.stores.orders.get("o"), []), [])
        cmd.extra["previous_quote_id"] = "stale"
        self.assertEqual(plugin_runtime.build_revision_unconfirmed_commands(
            cmd, self.stores.orders.get("o"), []), [])

    def test_platform_paid_gate_cannot_reserve(self):
        cmd = self.command()
        cmd.status = "CLAIMED"
        self.commands.enqueue(cmd)
        with (patch.object(plugin_authorize, "queue", return_value=self.commands),
              patch.object(ledger, "stores", return_value=self.stores)):
            gate = plugin_authorize.authorize_execution(cmd.command_id,
                         {"orderId": "platform", "accountUnb": "s", "orderStatus": 2})
        self.assertFalse(gate["allowed"])
        self.assertNotIn("quote_revision_pending", self.stores.orders.get("o").payload)

    def test_success_receipt_settles_even_when_shop_messages_are_disabled(self):
        cmd = self.command()
        cmd.status = "CLAIMED"
        self.commands.enqueue(cmd)
        authorize_quote_revision(self.stores, cmd)
        with (patch.object(plugin_api, "queue", return_value=self.commands),
              patch.object(ledger, "stores", return_value=self.stores),
              patch.object(plugin_runtime, "get_local_service", return_value=None),
              patch("application.store.shop_send_enabled", return_value=False)):
            result = plugin_api.handle_result(cmd.command_id,
                                  {"status": "SUCCEEDED", "price_after_fen": 1850})
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(self.stores.orders.get("o").quote_id, "new")

    def test_payment_lock_prevents_revision_from_overwriting_confirmed_payment(self):
        entered, attempted, finished = ThreadEvent(), ThreadEvent(), ThreadEvent()
        real = ledger._confirm_payment_locked

        def blocked(*args, **kwargs):
            entered.set()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(finished.wait(.05))
            return real(*args, **kwargs)

        def reserve():
            self.assertTrue(entered.wait(2))
            attempted.set()
            try:
                authorize_quote_revision(self.stores, cmd)
            finally:
                finished.set()

        cmd = self.command()
        with (patch.object(ledger, "stores", return_value=self.stores),
              patch.object(ledger, "_confirm_payment_locked", side_effect=blocked),
              ThreadPoolExecutor(max_workers=2) as pool):
            payment = pool.submit(ledger.confirm_payment, "o", detail={"orderStatus": 2, "payment": "2460"})
            revision = pool.submit(reserve)
            self.assertTrue(payment.result(timeout=3)["ok"])
            with self.assertRaisesRegex(ValueError, "NOT_UNPAID"):
                revision.result(timeout=3)
        self.assertEqual(self.stores.orders.get("o").payment_state, "CONFIRMED")

    def test_image_fixed_sell_price_can_use_estimated_supplier_cost(self):
        conv = SimpleNamespace(quote_id="old")
        result = SimpleNamespace(ok=True, need_human=False, price_is_estimate=True,
                                 template_keys=["QUOTE_CARD"], messages=["识别中", "首次报价", "要吗", "补座位"])

        def quote(*args, **kwargs):
            conv.quote_id = "new"
            return result

        svc = SimpleNamespace(session=lambda _: SimpleNamespace(conversation=conv, pending_order_card={}), on_message=quote)
        event = {"id": "image-2", "tenantId": "t", "payload": {"accountUnb": "s",
                 "peerUnb": "b", "chatId": "c", "messageType": 2,
                 "imageUrls": ["https://example.com/second.png"]}}
        with (patch.object(plugin_runtime, "get_local_service", return_value=svc),
              patch.object(plugin_runtime, "_sent_message_ids", return_value=[]),
              patch.object(plugin_runtime, "_shop_allows_send", return_value=True),
              patch.object(ledger, "stores", return_value=self.stores),
              patch("settings.load", return_value=SimpleNamespace(fish_reprice=SimpleNamespace(auto_reprice=True)))):
            commands = plugin_runtime._from_buyer_message(event)
            conv.quote_id = "old"
            self.quote.metadata["price_mode"] = "LIMIT"
            self.stores.quotes.put(self.quote)
            self.assertEqual([c.text for c in plugin_runtime._from_buyer_message(event)], result.messages)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].extra["target_amount_fen"], 1850)

    def test_committed_revision_uses_matching_payment_snapshot_and_blocks_third_image(self):
        cmd = self.command()
        authorize_quote_revision(self.stores, cmd)
        cmd.status, cmd.extra["price_after_fen"] = "SUCCEEDED", 1850
        settle_quote_revision(self.stores, cmd)
        conv = Conversation(**self.ident, state=ConvState.QUOTED)
        conv.quote_id = "new"
        conv.quote = SimpleNamespace(total_price_fen=1850)
        conv.recognition = SimpleNamespace(cinema_name="影院", movie_name="新电影",
                                           start_time="2026-10-17T15:20:00+08:00")
        conv.seats = ["F排6座"]
        session = SimpleNamespace(conversation=conv)
        seen = []

        def fulfill(snapshot, out, **kwargs):
            seen.append(snapshot)

        svc = SimpleNamespace(sessions={"c": session}, session=lambda _: session,
                              persist=False, flow=SimpleNamespace(on_payment_confirmed=fulfill))
        event = {"tenantId": "t", "payload": {"accountUnb": "s", "peerUnb": "b",
                 "chatId": "c", "orderId": "platform"},
                 "_order_detail": {"orderId": "platform", "accountUnb": "s",
                                   "orderStatus": 2, "payment": "1850"}}
        with (patch.object(ledger, "stores", return_value=self.stores),
              patch.object(plugin_runtime, "get_local_service", return_value=svc)):
            plugin_runtime.sync_repriced_conversation(cmd)
            self.assertEqual(conv.state, ConvState.AWAIT_PAY)
            plugin_runtime._from_order_paid(event)
            self.assertEqual(seen[0].quote_id, "new")
            self.assertIsNot(seen[0], conv)
            plugin_runtime._from_order_paid(event)
            self.assertEqual(len(seen), 1)
            conv.quote_id = "third"
            from application.quote_reprice import revision_conversation_matches
            self.assertFalse(revision_conversation_matches(self.stores.orders.get("o"), conv))


if __name__ == "__main__":
    unittest.main()
