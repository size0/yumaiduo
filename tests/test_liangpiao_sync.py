import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import settings
from application import liangpiao_sync as sync, ledger, plugin_runtime
from domain.order import Order, OrderStore
from integrations.yumaiduo.commands import CommandStore


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name)/'queue.sqlite')
        self.q = CommandStore(self.db)
        self.orders = OrderStore(str(Path(self.temp.name)/'orders.sqlite'))
        self.order = Order(order_id='local', quote_id='quote', tenant_id='t', shop_id='s',
            buyer_id='b', chat_id='c', provider='LIANGPIAO', provider_order_id='LP',
            platform_order_id='XY', state='PAID', payment_state='CONFIRMED',
            amount_fen=1000, paid_fen=1000, idempotency_key='key', payload={'out_order_no':'out'})
        self.orders.create(self.order)

    def tearDown(self):
        self.q.connection.close(); self.orders.close(); self.temp.cleanup()

    def receive(self, *, event='order.ticketed', version=1, code='CODE', order_no='LP', secret='secret'):
        raw = json.dumps({'event':event,'data':{'orderNo':order_no,'outOrderNo':'out',
            'version':version,'tickets':[{'ticketCode':code}] if code else []}})
        headers = {'X-App-Key':'key','X-Timestamp':'1792080000','X-Nonce':'nonce'}
        headers['X-Sign'] = hmac.new(secret.encode(), ('key1792080000nonce'+raw).encode(), hashlib.sha256).hexdigest()
        return sync.receive(raw, headers, 'key','secret',self.q,now=1792080000)

    def test_durable_restart_replay(self):
        self.receive();self.q.connection.close();self.q=CommandStore(self.db)
        self.assertEqual(sync.process(self.q,self.orders),1)
        self.assertEqual(self.orders.get('local').ticket_codes,['CODE'])
        self.receive();self.assertEqual(sync.process(self.q,self.orders),0)

    def test_bad_signature_and_conflicting_duplicate(self):
        with self.assertRaises(Exception):self.receive(secret='wrong')
        self.receive()
        with self.assertRaisesRegex(ValueError,'CONTENT_CONFLICT'):self.receive(code='OTHER')

    def test_empty_and_stale_tickets(self):
        self.receive(version=0,code='');sync.process(self.q,self.orders)
        self.assertEqual(self.orders.get('local').state,'PAID')
        self.receive(event='ticket.updated',version=2,code='NEW');sync.process(self.q,self.orders)
        self.receive(version=1);sync.process(self.q,self.orders)
        self.assertEqual(self.orders.get('local').ticket_codes,['NEW'])

    def test_binding_can_arrive_later(self):
        self.receive(order_no='FAST');self.assertEqual(sync.process(self.q,self.orders),0)
        o=self.orders.get('local');o.provider_order_id='FAST';self.orders.put(o)
        self.assertEqual(sync.process(self.q,self.orders),1)

    def test_updated_code_needs_new_delivery_receipt(self):
        self.receive();sync.process(self.q,self.orders)
        order=self.orders.get('local');order.state='DELIVERED';order.shipped=True;self.orders.put(order)
        self.receive(event='ticket.updated',version=2,code='NEW');sync.process(self.q,self.orders)
        self.assertEqual(self.orders.get('local').state,'TICKETED')
        self.assertTrue(self.orders.get('local').shipped)

    def test_supplier_refund_does_not_refund_buyer(self):
        self.receive(event='order.refunded');sync.process(self.q,self.orders)
        o=self.orders.get('local');self.assertEqual(o.payment_state,'CONFIRMED')
        self.assertTrue(o.payload['buyer_refund_required']);self.assertEqual(o.state,'FAILED')

    def test_queue_uses_platform_id_and_dedupes(self):
        self.receive();sync.process(self.q,self.orders)
        stores=SimpleNamespace(orders=self.orders)
        with patch('application.ledger.stores',return_value=stores), \
             patch('application.ledger.orders_payload',side_effect=lambda **kw:{'orders':[ledger._order_row(self.orders.get('local'))]}), \
             patch('application.plugin_runtime.queue',return_value=self.q), \
             patch('application.plugin_runtime._shop_allows_send',return_value=True), \
             patch('settings.load',return_value=settings.Settings()), \
             patch('templates.render_messages',return_value=['票码CODE','谢谢']):
            self.assertEqual(plugin_runtime.queue_shipping_for_delivered(),3)
            self.assertEqual(plugin_runtime.queue_shipping_for_delivered(),0)
            with patch('templates.render_messages',return_value=['changed','extra','another']):
                self.assertEqual(plugin_runtime.queue_shipping_for_delivered(),0)
        commands=self.q.by_status('PENDING')
        self.assertTrue(all(c.event_id=='XY' for c in commands))
        self.assertTrue(all(c.extra.get('delivery_provider')=='LIANGPIAO' for c in commands))
        self.assertEqual(len([c for c in commands if c.depends_on_command_id]),2)
        claimed = self.q.claim_pending(10)
        self.assertEqual([c.type for c in claimed], ['SHIP_ORDER'])
        ship = claimed[0]
        from application.plugin_authorize import _token_for
        self.q.record_result(ship.command_id,status='SUCCEEDED',execution_token=_token_for(ship),
                             detail={'orderId':'XY','accountUnb':'s','orderStatus':3})
        first = self.q.claim_pending(10)
        self.assertEqual(len(first),1)
        self.assertEqual(first[0].text,'票码CODE')
        self.q.record_result(first[0].command_id,status='SENT',sent_message_id='real-message')
        second = self.q.claim_pending(10)
        self.assertEqual(len(second),1)
        self.assertEqual(second[0].text,'谢谢')

    def test_missing_bindings_do_not_starve_new_order(self):
        for i in range(50):self.receive(order_no=f'unknown-{i}')
        self.receive()
        self.assertEqual(sync.process(self.q,self.orders),0)
        self.assertEqual(sync.process(self.q,self.orders),1)

    def test_identity_and_stale_delivery_gate(self):
        from application import plugin_authorize
        self.receive();sync.process(self.q,self.orders)
        command=SimpleNamespace(type='TICKET_DELIVERY_NOTICE',extra={'delivery_provider':'LIANGPIAO','local_order_id':'local','ticket_version':0},
                                event_id='XY',tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
        with patch('application.plugin_authorize._load',return_value=(command,None)), \
             patch('application.ledger.stores',return_value=SimpleNamespace(orders=self.orders)), \
             patch('application.store.shop_send_enabled',return_value=True):
            self.assertEqual(plugin_authorize.authorize_ticket_delivery('c')['reason'],'TICKET_VERSION_SUPERSEDED')
            command.extra['ticket_version']=1
            self.assertEqual(plugin_authorize.authorize_ticket_delivery('c')['reason'],'OFFICIAL_DELIVERY_FACTS_REQUIRED')
            result=plugin_authorize.authorize_ticket_delivery('c',detail={'orderId':'XY','accountUnb':'s','orderStatus':2},session={'peerUnb':'wrong'})
            self.assertEqual(result['reason'],'OFFICIAL_BUYER_MISMATCH')

    def test_unpaid_and_failed_do_not_queue(self):
        self.receive();sync.process(self.q,self.orders)
        for state,payment in [('TICKETED','UNPAID'),('FAILED','CONFIRMED')]:
            o=self.orders.get('local');o.state=state;o.payment_state=payment;self.orders.put(o)
            with patch('application.ledger.orders_payload',return_value={'orders':[ledger._order_row(o)]}), \
                 patch('application.plugin_runtime.queue',return_value=self.q),patch('settings.load',return_value=settings.Settings()):
                self.assertEqual(plugin_runtime.queue_shipping_for_delivered(),0)


if __name__=='__main__':unittest.main()
