"""Manual settlement is a scoped reconciliation, never an Agent permission."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from domain.order import Order, OrderStore
from domain.quote import QuoteRecord, QuoteStore
from application import payment_sync


class ManualSettlementTests(unittest.TestCase):
    def setUp(self):
        self.ident = dict(tenant_id='t', shop_id='s', buyer_id='b', chat_id='c')
        self.stores = SimpleNamespace(orders=OrderStore(), quotes=QuoteStore())
        self.addCleanup(self.stores.orders.close)
        self.addCleanup(self.stores.quotes.close)
        self.q = QuoteRecord(quote_id='q', provider='WANDA', ticket_count=1,
            total_sell_price_fen=3400, unit_sell_price_fen=3400, total_cost_fen=3300,
            metadata={'quote_kind': 'ORDER_TOTAL'}, **self.ident)
        self.stores.quotes.create(self.q)
        self.o = Order(order_id='o', quote_id='q', idempotency_key='order-q', provider='WANDA', provider_order_id='p',
            amount_fen=3400, ticket_count=1, state='AWAITING_PAYMENT', payment_state='UNPAID',
            payload={'manual_ticketing': True, 'upstream_created': False}, **self.ident)
        self.stores.orders.create(self.o)
        self.detail = dict(orderId='p', accountUnb='s', orderStatus=3, payment='3300',
                           payTime='2026-10-17T07:37:38Z', grouponPending=False)
        self.read = Mock(return_value=self.detail)

    def confirm(self, **changes):
        args = dict(order_id='o', tenant_id='t', shop_id='s', amount_fen=3300,
                    expected_quote_id='q', expected_amount_fen=3400,
                    expected_updated_at=self.o.updated_at.isoformat(), actor='verified-admin',
                    approval_id='approval-1', platform_read=self.read)
        args.update(changes)
        return payment_sync.confirm_manual_price(self.stores, **args)

    def test_observation_does_not_authorize_fulfillment(self):
        payment_sync.observe(self.stores.orders, self.o, self.detail, source='order.paid')
        saved = self.stores.orders.get('o')
        self.assertEqual(saved.payment_state, 'UNPAID')
        self.assertEqual(saved.amount_fen, 3400)
        self.assertEqual(saved.payload['platform_payment_observation']['amount_fen'], 3300)
        self.assertFalse(saved.can_deliver()[0])

    def test_wrong_identity_unpaid_or_missing_timestamp_cannot_be_observed(self):
        for field, value in [('accountUnb','other'), ('orderId','other'), ('orderStatus',1),
                              ('orderStatus',True), ('payTime',None), ('payment','bad')]:
            with self.subTest(field=field, value=value):
                self.assertFalse(payment_sync.observe(self.stores.orders, self.o,
                    dict(self.detail, **{field:value}), source='event'))
                self.assertFalse(self.stores.orders.get('o').payload.get('platform_payment_observation'))

    def test_manual_confirmation_preserves_original_and_stays_on_hold(self):
        result = self.confirm()
        saved = self.stores.orders.get('o')
        self.assertTrue(result['ok'])
        self.assertEqual(saved.amount_fen, 3300)
        self.assertEqual(saved.paid_fen, 3300)
        self.assertEqual(saved.payment_state, 'AMBIGUOUS')
        self.assertTrue(saved.shipped)
        self.assertFalse(saved.can_deliver()[0])
        self.assertEqual(self.stores.quotes.get('q').total_sell_price_fen, 3400)
        settled = self.stores.quotes.get(saved.quote_id)
        self.assertEqual(settled.total_sell_price_fen, 3300)
        self.assertEqual(settled.metadata['purpose'], 'MANUAL_SETTLEMENT')
        self.assertFalse(settled.is_usable())
        self.assertEqual([q.quote_id for q in self.stores.quotes.by_identity(self.ident)], ['q'])
        sql_quote = self.stores.orders.connection.execute('SELECT quote_id FROM orders WHERE order_id=?',('o',)).fetchone()[0]
        self.assertEqual(sql_quote, saved.quote_id)
        self.assertFalse(saved.ticket_codes)
        self.assertEqual(len(self.stores.orders.payments_for('o')), 1)
        self.assertEqual(self.stores.orders.payments_for('o')[0].state, 'AMBIGUOUS')
        self.assertEqual(self.read.call_count, 1)

    def test_wrong_amount_scope_version_or_missing_actor_is_rejected(self):
        for changes in ({'amount_fen':3200}, {'tenant_id':'other'}, {'shop_id':'other'},
                        {'expected_quote_id':'old'}, {'expected_amount_fen':3300},
                        {'expected_updated_at':'old'}, {'actor':''}, {'amount_fen':True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.confirm(**changes)
            self.assertEqual(self.stores.orders.get('o').quote_id, 'q')

    def test_concurrent_change_is_not_overwritten(self):
        def changed(*args):
            current = self.stores.orders.get('o')
            current.payload['concurrent'] = True
            self.stores.orders.put(current)
            return self.detail
        self.read.side_effect = changed
        with self.assertRaises(ValueError):
            self.confirm()
        self.assertTrue(self.stores.orders.get('o').payload['concurrent'])
        self.assertEqual(self.stores.orders.get('o').quote_id, 'q')

    def test_repeat_does_not_create_more_settlements(self):
        self.confirm()
        self.assertTrue(self.confirm()['already'])
        self.assertEqual(self.stores.quotes.connection.execute('SELECT count(*) FROM quote_records').fetchone()[0], 2)
        self.assertEqual(len(self.stores.orders.payments_for('o')), 1)

    def test_paid_observation_is_visible_without_confirming_old_price(self):
        from application.ledger import _order_row
        payment_sync.observe(self.stores.orders, self.o, self.detail, source='event')
        row = _order_row(self.stores.orders.get('o'))
        self.assertIn('已付款', row['state_label'])
        self.assertEqual(row['bucket'], 'attention')
        self.assertEqual(row['platform_paid_yuan'], 33)
        self.assertEqual(row['payment_state'], 'UNPAID')
        self.assertTrue(row['can_confirm_manual_price'])

    def test_event_mismatch_records_fact_without_refund_or_delivery(self):
        from unittest.mock import patch
        from application import plugin_runtime, ledger
        service = SimpleNamespace(sessions={})
        event = {'event':'order.paid','tenantId':'t','payload':{
            'orderId':'p','accountUnb':'s','buyerId':'b','chatId':'c'}, '_order_detail':self.detail}
        with patch.object(ledger,'stores',return_value=self.stores), patch.object(plugin_runtime,'get_local_service',return_value=service):
            commands = plugin_runtime._from_order_paid(event)
        self.assertTrue(all(c.type == 'SEND_MESSAGE' and not c.text for c in commands))
        saved = self.stores.orders.get('o')
        self.assertEqual(saved.payment_state, 'UNPAID')
        self.assertEqual(saved.payload['platform_payment_observation']['amount_fen'], 3300)

    def test_old_reprice_cannot_get_new_authorization(self):
        from unittest.mock import patch
        from application import plugin_authorize, ledger
        self.confirm()
        cmd = SimpleNamespace(type='CHANGE_ORDER_PRICE',event_id='p',shop_id='s',extra={'target_amount_fen':3400})
        with patch.object(plugin_authorize,'_load',return_value=(cmd,None)), patch.object(ledger,'stores',return_value=self.stores):
            result = plugin_authorize.authorize_execution('cmd',dict(self.detail,orderStatus=1,payTime=None))
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'],'MANUAL_SETTLEMENT_SUPERSEDES_REPRICE')

    def test_ledger_payment_replay_cannot_remove_manual_hold(self):
        from unittest.mock import patch
        from application import ledger
        self.confirm()
        with patch.object(ledger,'stores',return_value=self.stores):
            result = ledger.confirm_payment('o',online_order_id='p',detail=self.detail,tenant_id='t')
        self.assertEqual(result['error'],'MANUAL_RELEASE_REQUIRED')
        self.assertEqual(self.stores.orders.get('o').payment_state,'AMBIGUOUS')

    def test_receipt_failure_rolls_back_binding_and_orphan_is_not_an_offer(self):
        self.stores.orders.connection.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON order_payments BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            self.confirm()
        self.assertEqual(self.stores.orders.get('o').quote_id, 'q')
        self.assertEqual(self.stores.orders.get('o').payment_state, 'UNPAID')
        self.assertEqual([q.quote_id for q in self.stores.quotes.by_identity(self.ident)], ['q'])
        self.stores.orders.connection.execute('DROP TRIGGER reject_receipt')
        self.confirm()
        self.assertEqual(self.stores.orders.get('o').amount_fen, 3300)

    def test_staff_http_scope_json_and_platform_read_are_enforced(self):
        import json
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        import server
        with (patch.object(server.Handler,'_auth_ok',return_value=True),
              patch('application.ledger.stores',return_value=self.stores),
              patch('integrations.yumaiduo.shop_client.read_shops',return_value={'shops':[{'tenant_id':'t','shop_id':'s'}]}),
              patch('application.purchase_actions.platform_order',self.read)):
            httpd = ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread = threading.Thread(target=httpd.serve_forever,daemon=True)
            thread.start()
            body = dict(order_id='o',tenant_id='t',shop_id='s',amount_fen=3300,
                        expected_quote_id='q',expected_amount_fen=3400,
                        expected_updated_at=self.o.updated_at.isoformat(),approval_id='http-test')
            def request(data, content_type='application/json'):
                conn = http.client.HTTPConnection('127.0.0.1',httpd.server_port)
                try:
                    conn.request('POST','/api/order/manual-settlement',json.dumps(data),{'Content-Type':content_type})
                    response = conn.getresponse()
                    return response.status,json.loads(response.read())
                finally:
                    conn.close()
            try:
                self.assertEqual(request(body,'text/plain')[0],403)
                self.assertEqual(request(dict(body,shop_id='other'))[0],403)
                self.assertEqual(request(dict(body,expected_quote_id='old'))[0],409)
                self.assertFalse(self.read.called)
                status,response = request(body)
                self.assertEqual(status,200)
                self.assertTrue(response['fulfillment_paused'])
                self.assertEqual(self.read.call_count,1)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_pending_revision_or_supplier_order_cannot_be_settled(self):
        for key, value in (('quote_revision_pending', {'command_id':'cmd'}), ('upstream_created',True)):
            saved = self.stores.orders.get('o')
            saved.payload[key] = value
            self.stores.orders.put(saved)
            self.o = self.stores.orders.get('o')
            with self.assertRaises(ValueError):
                self.confirm()
            saved.payload.pop(key)
            self.stores.orders.put(saved)


if __name__ == '__main__':
    unittest.main()
