import unittest
from unittest.mock import patch
from application.order_status_sync import sync_closed,is_close_notice
from domain.order import Order,OrderStore

class CloseSyncTests(unittest.TestCase):
    def setUp(self):
        self.orders=OrderStore();self.addCleanup(self.orders.close)
        self.orders.create(Order(order_id='o',quote_id='q',idempotency_key='i',tenant_id='t',shop_id='s',buyer_id='b',chat_id='c',provider='WANDA',provider_order_id='p',amount_fen=7380,state='AWAITING_PAYMENT'))
        self.event={'event':'im.message.received','id':'event','tenantId':'t','payload':{'chatId':'__notify__','peerUnb':'unrelated-system-peer','accountUnb':'s','orderId':'p','messageType':28,'content':'未付款，买家关闭订单'}}
        self.detail={'orderId':'p','accountUnb':'s','orderStatus':6,'payTime':None}
    def test_closes_only_bound_order_and_duplicate_is_noop(self):
        with patch('application.order_status_sync.platform_order',return_value=self.detail) as read:
            sync_closed(self.event,self.orders)
            before=self.orders.get('o').to_dict()
            sync_closed(self.event,self.orders)
        self.assertEqual(before,self.orders.get('o').to_dict())
        self.assertEqual(before['state'],'CANCELLED')
        self.assertEqual(read.call_args.args[1]['buyer_id'],'b')
    def test_wrong_shop_and_uncertain_or_paid_details_never_close(self):
        for key,value in [('orderStatus',1),('orderStatus',2),('orderStatus','6'),('payTime','2026-10-16'),('orderId','other'),('accountUnb','other')]:
            with patch('application.order_status_sync.platform_order',return_value=dict(self.detail,**{key:value})):
                sync_closed(self.event,self.orders)
            self.assertEqual(self.orders.get('o').state,'AWAITING_PAYMENT')
        self.event['payload']['accountUnb']='other'
        with patch('application.order_status_sync.platform_order') as read:
            sync_closed(self.event,self.orders)
            read.assert_not_called()
    def test_local_paid_state_never_downgraded(self):
        o=self.orders.get('o');o.payment_state='CONFIRMED';o.paid_fen=7380;o.state='PAID';self.orders.put(o)
        with patch('application.order_status_sync.platform_order',return_value=self.detail):
            sync_closed(self.event,self.orders)
        self.assertEqual(self.orders.get('o').state,'PAID')
    def test_system_notice_is_processed_before_chat_filter_without_buyer_reply(self):
        from application import plugin_runtime,ledger
        from types import SimpleNamespace
        with patch.object(ledger,'stores',return_value=SimpleNamespace(orders=self.orders)),patch('application.order_status_sync.platform_order',return_value=self.detail):
            commands=plugin_runtime.events_to_commands(self.event)
        self.assertEqual(self.orders.get('o').state,'CANCELLED')
        self.assertTrue(all(not c.text and c.extra.get('internal_only') for c in commands))
    def test_notice_without_order_id_cannot_guess_order(self):
        self.event['payload'].pop('orderId')
        self.assertFalse(is_close_notice(self.event))

    def test_late_reprice_receipt_cannot_reopen_closed_order(self):
        from tests.test_quote_reprice import QuoteRepriceTests
        from application.quote_reprice import authorize_quote_revision,settle_quote_revision
        t=QuoteRepriceTests();t.setUp();self.addCleanup(t.tearDown)
        cmd=t.command();authorize_quote_revision(t.stores,cmd)
        e={'tenantId':'t','payload':{'accountUnb':'s','orderId':'platform'}}
        with patch('application.order_status_sync.platform_order',return_value=dict(self.detail,orderId='platform')):
            sync_closed(e,t.stores.orders)
        cmd.status='SUCCEEDED';cmd.extra['price_after_fen']=cmd.extra['target_amount_fen']
        with self.assertRaisesRegex(ValueError,'TERMINAL'):
            settle_quote_revision(t.stores,cmd)
        self.assertEqual(t.stores.orders.get('o').state,'CANCELLED')

    def test_quantity_fragments_keep_literal_evidence_boundary(self):
        from application.semantic_request import number
        for text in ('两','两个','这两个','俩','2张'):
            self.assertEqual(number(text),2)
        for text in ('两部电影','两场','两三个','不要两个'):
            self.assertIsNone(number(text))
