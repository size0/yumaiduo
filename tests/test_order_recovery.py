import unittest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from application.order_recovery import bind_wanda_card, recover_identity, payment_conversation
from domain.quote import QuoteStore, QuoteRecord
from domain.order import OrderStore
from domain.conversation import Conversation

class OrderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
        self.stores=SimpleNamespace(quotes=QuoteStore(),orders=OrderStore())
        self.addCleanup(self.stores.quotes.close);self.addCleanup(self.stores.orders.close)
        self.q=QuoteRecord(quote_id='q',provider='WANDA',ticket_count=1,selected_seats=['8排20座'],
            unit_sell_price_fen=4400,total_sell_price_fen=4400,cinema='影院',movie='电影',show='2099-01-01 18:00',
            demand_id='d',demand_rev=1,metadata={'quote_kind':'ORDER_TOTAL','inquiry_only':True},**self.ident)
        self.stores.quotes.create(self.q)
        c=Conversation(**self.ident);c.demand_id='d';c.demand_rev=1;c.demand_inquiry=True
        self.session=SimpleNamespace(conversation=c,image_context={'verified':True},pending_order_card={'payload':{'orderId':'p'}})
        self.service=SimpleNamespace(sessions={'c':self.session})
        self.detail=dict(orderId='p',accountUnb='s',orderStatus=2,payment='4400',payTime='2026-10-16T08:25:00Z',
            grouponPending=False,createTime=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())

    def test_paid_recovery_preserves_inquiry_and_is_idempotent(self):
        a=bind_wanda_card(self.session,self.stores,self.ident,self.detail,paid=True)
        b=bind_wanda_card(self.session,self.stores,self.ident,self.detail,paid=True)
        self.assertEqual(a.order_id,b.order_id)
        self.assertEqual(len(self.stores.orders.by_identity(self.ident)),1)
        self.assertTrue(self.stores.quotes.get('q').metadata['inquiry_only'])
        self.assertTrue(self.stores.quotes.get(a.quote_id).is_order_ready())
        self.assertEqual(a.payment_state,'UNPAID') # ledger is still required
        c=payment_conversation(a,self.stores.quotes)
        self.assertEqual(c.quote.total_price_fen,4400)

    def test_unpaid_purchase_accepts_full_quote(self):
        self.detail.update(orderStatus=1,payTime=None,payment='2000')
        self.assertIsNotNone(bind_wanda_card(self.session,self.stores,self.ident,self.detail))

    def test_wrong_amount_state_shop_missing_time_and_newer_quote_block(self):
        for field,value in [('payment','2000'),('orderStatus',1),('accountUnb','other'),('payTime',None),('createTime','2000-01-01T00:00:00Z'),('grouponPending',True)]:
            with self.subTest(field=field):
                d=dict(self.detail);d[field]=value
                self.assertIsNone(bind_wanda_card(self.session,self.stores,self.ident,d,paid=True))
        self.assertFalse(self.stores.orders.by_identity(self.ident))

    def test_new_demand_and_unit_only_block(self):
        self.session.conversation.demand_rev=2
        self.assertIsNone(bind_wanda_card(self.session,self.stores,self.ident,self.detail,paid=True))
        self.session.conversation.demand_rev=1
        self.session.image_context['unverified_image']='new'
        self.assertIsNone(bind_wanda_card(self.session,self.stores,self.ident,self.detail,paid=True))

    def test_identity_exact_order_and_shop_only(self):
        e={'payload':{'orderId':'p'}}
        ident=dict(self.ident,buyer_id='',chat_id='')
        self.assertEqual(recover_identity(e,ident,self.stores,self.service),self.ident)
        self.assertIsNone(recover_identity(e,dict(ident,shop_id='other'),self.stores,self.service))
        self.assertIsNone(recover_identity({'payload':{'orderId':'unknown'}},ident,self.stores,self.service))

    def test_full_paid_event_without_chat_builds_order_and_reply_once(self):
        from unittest.mock import patch
        from application import plugin_runtime, ledger
        from tests._check_channel_route import flow, MAP_NONE
        f,api,_=flow(MAP_NONE)
        self.service.flow=f
        self.service._save=lambda:None
        self.service.session=lambda _:self.session
        event={'event':'order.paid','tenantId':'t','payload':{'orderId':'p','accountUnb':'s'},'_order_detail':self.detail}
        with (patch.object(ledger,'stores',return_value=self.stores),
              patch.object(plugin_runtime,'get_local_service',return_value=self.service),
              patch.object(plugin_runtime,'_shop_allows_send',return_value=True),
              patch.object(plugin_runtime,'_sent_message_ids',return_value=[])):
            first=plugin_runtime.events_to_commands(event)
            second=plugin_runtime.events_to_commands(event)
        orders=self.stores.orders.by_identity(self.ident)
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0].payment_state,'CONFIRMED')
        self.assertEqual(orders[0].paid_fen,4400)
        self.assertTrue(any(c.text for c in first),first)
        self.assertTrue(all(not c.text for c in second))
        self.assertTrue(all(c.type != 'CHANGE_ORDER_PRICE' for c in first+second))
        self.assertFalse(api.create_calls)

    def test_bound_order_still_requires_strict_payment(self):
        from application.order_recovery import verified_payment
        d=dict(self.detail,orderStatus=1,payTime=None,payment='2000')
        order=bind_wanda_card(self.session,self.stores,self.ident,d)
        self.assertTrue(verified_payment(order,self.detail))
        for key,value in [('payment','2000'),('payTime',None),('orderStatus',1),('orderId','other'),('accountUnb','other')]:
            self.assertFalse(verified_payment(order,dict(self.detail,**{key:value})))

    def test_concurrent_different_cards_create_only_one_order(self):
        from concurrent.futures import ThreadPoolExecutor
        from copy import deepcopy
        sessions=[deepcopy(self.session),deepcopy(self.session)]
        sessions[1].pending_order_card['payload']['orderId']='p2'
        details=[self.detail,dict(self.detail,orderId='p2')]
        def bind(i):
            return bind_wanda_card(sessions[i],self.stores,self.ident,details[i],paid=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(bind,[0,1]))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(self.stores.orders.by_identity(self.ident)),1)
