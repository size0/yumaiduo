"""Purchase notice belongs to a placed unpaid order, once per order."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from application import ledger, plugin_runtime
from domain.order import Order, OrderStore
from domain.quote import QuoteRecord, QuoteStore
from integrations.yumaiduo.commands import CommandStore
import templates


class PurchaseNoticeTests(unittest.TestCase):
    def setUp(self):
        self.ident = dict(tenant_id="t", shop_id="s", buyer_id="b", chat_id="c")
        self.orders, self.quotes, self.queue = OrderStore(), QuoteStore(), CommandStore()
        self.order = Order(order_id="o", quote_id="q", idempotency_key="k",
                           provider="LIANGPIAO", provider_order_id="platform",
                           amount_fen=1850, state="AWAITING_PAYMENT", **self.ident)
        self.orders.create(self.order)
        self.quotes.create(QuoteRecord(quote_id="q", provider="LIANGPIAO",
                                      metadata={"ticket_mode": "STANDARD"}, **self.ident))

    def tearDown(self):
        self.orders.close()
        self.quotes.close()
        self.queue.close()

    def test_card_sends_once_even_with_auto_reprice_off(self):
        event = {"event":"im.message.received", "tenantId":"t", "payload":{
            "messageType":26,"orderId":"platform","accountUnb":"s","peerUnb":"b","chatId":"c"}}
        with (patch.object(ledger,"stores",return_value=SimpleNamespace(orders=self.orders,quotes=self.quotes)),
              patch.object(plugin_runtime,"_shop_allows_send",return_value=True),
              patch.object(plugin_runtime,"_sent_message_ids",return_value=[]),
              patch.object(plugin_runtime,"get_local_service",return_value=None),
              patch("settings.load",return_value=SimpleNamespace(fish_reprice=SimpleNamespace(auto_reprice=False)))):
            for i in range(2):
                event["eventId"] = f"card-{i}"
                for command in plugin_runtime.events_to_commands(event):
                    self.queue.enqueue(command)
        notices = [c for c in self.queue.by_status("PENDING") if c.type == "SEND_MESSAGE" and c.text]
        self.assertEqual(len(notices),1)
        self.assertIn("购票须知",notices[0].text)
        self.assertNotIn("{ticket_window}",notices[0].text)
        self.assertFalse(any(c.type == "CHANGE_ORDER_PRICE" for c in self.queue.by_status("PENDING")))

    def test_existing_order_notice_survives_incomplete_new_demand(self):
        self.quotes.create(QuoteRecord(quote_id="new-unit", provider="WANDA",
                                      metadata={"quote_kind": "UNIT_PRICE"}, **self.ident))
        session = SimpleNamespace(conversation=SimpleNamespace(**self.ident),
                                  image_context={}, pending_order_card={})
        service = SimpleNamespace(session=lambda _: session, _save=lambda: None)
        event = {"tenantId": "t", "payload": {"orderId": "platform",
                 "accountUnb": "s", "peerUnb": "b", "chatId": "c"}}
        with (patch.object(ledger, "stores", return_value=SimpleNamespace(orders=self.orders, quotes=self.quotes)),
              patch.object(plugin_runtime, "get_local_service", return_value=service),
              patch.object(plugin_runtime, "_shop_allows_send", return_value=True),
              patch.object(plugin_runtime, "_sent_message_ids", return_value=[])):
            commands = plugin_runtime._from_order_card(event)
        notices = [c for c in commands if c.type == "SEND_MESSAGE" and c.text]
        self.assertEqual(len(notices), 1)
        self.assertIn("购票须知", notices[0].text)
        self.assertFalse(any(c.type == "CHANGE_ORDER_PRICE" for c in commands))
        self.assertEqual(self.orders.get("o").quote_id, "q")

    def test_empty_and_disabled_and_paid_do_not_send(self):
        with patch.object(plugin_runtime,"_shop_allows_send",return_value=False):
            self.assertEqual(plugin_runtime.build_purchase_notice_commands(self.ident,self.order,self.quotes),[])
        with (patch.object(plugin_runtime,"_shop_allows_send",return_value=True),
              patch.object(templates,"render_messages",return_value=[])):
            self.assertEqual(plugin_runtime.build_purchase_notice_commands(self.ident,self.order,self.quotes),[])
        self.order.payment_state = "CONFIRMED"
        with patch.object(plugin_runtime,"_shop_allows_send",return_value=True):
            self.assertEqual(plugin_runtime.build_purchase_notice_commands(self.ident,self.order,self.quotes),[])

    def test_other_buyer_cannot_receive_notice(self):
        with patch.object(plugin_runtime,"_shop_allows_send",return_value=True):
            self.assertEqual(plugin_runtime.build_purchase_notice_commands(
                dict(self.ident,buyer_id="other"),self.order,self.quotes),[])


if __name__ == "__main__":
    unittest.main()
