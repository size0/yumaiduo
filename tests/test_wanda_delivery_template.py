import unittest
from unittest.mock import patch
from types import SimpleNamespace
from application import plugin_runtime, ledger
from integrations.yumaiduo.commands import CommandStore, Command, dedupe_for

class WandaDeliveryTemplateTests(unittest.TestCase):
    def setUp(self):
        self.q=CommandStore();self.addCleanup(self.q.close)
        self.order=dict(order_id="local",provider_order_id="123",provider="WANDA",tenant_id="t",shop_id="s",buyer_id="b",chat_id="c",ticket_codes=[],wanda_ticket_image={"imageUrl":"https://img.alicdn.com/qr.png","width":100,"height":100},payment_state="CONFIRMED",state="DELIVERED")
        self.parts=["出票成功！","请关注"]
        self.created=[]
        original=self.q.enqueue
        def enqueue(c):
            result=original(c)
            if result[0]:self.created.append(result[1])
            return result
        self.q.enqueue=enqueue
    def run_queue(self):
        with patch.object(ledger,"stores",return_value=SimpleNamespace(orders=SimpleNamespace(lock=__import__("threading").RLock(),get=lambda _:None))),patch.object(plugin_runtime,"queue",return_value=self.q),patch.object(ledger,"orders_payload",return_value={"orders":[self.order]}),patch("settings.load",return_value=SimpleNamespace(order_ship=SimpleNamespace(auto_ship=True))),patch("templates.render_messages",return_value=self.parts) as render:
            made=plugin_runtime.queue_shipping_for_delivered()
        return made,render
    def test_split_order_identity_dependency_dedupe(self):
        made,render=self.run_queue();self.assertEqual(made,4)
        self.assertEqual(render.call_args.kwargs["channel"],"wanda")
        self.assertEqual(render.call_args.args[1]["ticket_code"],"")
        ship,qr,a,b=self.created
        self.assertEqual(ship.event_id,"123");self.assertEqual(ship.extra["order_id"],"123")
        self.assertEqual(a.depends_on_command_id,qr.command_id);self.assertEqual(b.depends_on_command_id,a.command_id)
        self.parts=["changed"]
        self.assertEqual(self.run_queue()[0],0)
        self.assertEqual(self.q.get(a.command_id).text,"出票成功！")
    def test_unpaid_has_no_commands(self):
        self.order["payment_state"]="UNPAID"
        self.assertEqual(self.run_queue()[0],0)
    def test_empty_template_does_not_fallback(self):
        self.parts=[];self.assertEqual(self.run_queue()[0],2)
        self.assertEqual(len(self.created),2)
    def test_historical_ship_does_not_get_new_template_parts(self):
        ship=Command(command_id="old",type="SHIP_ORDER",event_id="local",dedupe_key=dedupe_for("local",kind="ship"))
        self.q.enqueue(ship)
        self.run_queue()
        self.assertFalse(any("CODE1" in (c.text or "") for c in self.created))

    def test_claim_receipt_claim_sequence(self):
        self.run_queue();ship,qr,a,b=self.created
        self.assertEqual([c.command_id for c in self.q.claim_pending()], [ship.command_id])
        self.assertEqual(self.q.get(a.command_id).status,"PENDING")
        self.assertEqual(self.q.get(b.command_id).status,"PENDING")
        from application.plugin_authorize import _token_for
        self.q.record_result(ship.command_id,status="SUCCEEDED",execution_token=_token_for(ship),detail={"orderId":"123","accountUnb":"s","orderStatus":3})
        self.assertEqual(self.q.get(ship.command_id).status,"SUCCEEDED")
        self.assertEqual([c.command_id for c in self.q.claim_pending()], [qr.command_id])
        self.q._set_status(qr.command_id,"SENT")
        self.assertEqual([c.command_id for c in self.q.claim_pending()], [a.command_id])
        self.assertEqual(self.q.get(b.command_id).status,"PENDING")
        self.q._set_status(a.command_id,"SENT")
        self.assertEqual([c.command_id for c in self.q.claim_pending()], [b.command_id])
    def test_unknown_parent_never_sends_notice(self):
        self.run_queue();ship,qr,a,b=self.created
        self.q._set_status(ship.command_id,"UNKNOWN")
        self.assertEqual(self.q.claim_pending(),[])
        self.assertEqual(self.q.get(a.command_id).status,"SUPPRESSED")

    def test_unverified_ship_receipt_stops_notices(self):
        self.run_queue();ship,qr,a,b=self.created
        self.q.record_result(ship.command_id,status="SUCCEEDED",detail={"orderId":"wrong","accountUnb":"s","orderStatus":3})
        self.assertEqual(self.q.get(ship.command_id).status,"UNKNOWN")
        self.assertEqual(self.q.claim_pending(),[])
