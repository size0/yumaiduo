import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime,timedelta,timezone
import settings
from application.buyer_assistant import BuyerAssistantTools
from application import purchase_actions
from application.quote_reprice import authorize_quote_revision,settle_quote_revision,build_quote_reprice
from domain.order import Order,OrderStore
from domain.quote import QuoteRecord,QuoteStore
from domain.conversation import Conversation

class AgentPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
        self.orders=OrderStore();self.quotes=QuoteStore()
        self.addCleanup(self.orders.close);self.addCleanup(self.quotes.close)
        self.orders.create(Order(order_id='o',quote_id='old',provider='LIANGPIAO',provider_order_id='123',idempotency_key='i',amount_fen=3290,state='AWAITING_PAYMENT',payload={'upstream_created':False},**self.ident))
        self.q=QuoteRecord(quote_id='new',provider='WANDA',request_type='WPLUS_AREA',ticket_count=3,total_sell_price_fen=13560,expires_at=datetime.now(timezone.utc)+timedelta(minutes=10),metadata={'price_mode':'FIXED','quote_kind':'ORDER_TOTAL'},**self.ident)
        self.quotes.create(self.q)
        cfg=settings.Settings();cfg.fish_reprice.auto_reprice=True
        self.tools=BuyerAssistantTools(SimpleNamespace(order_store=self.orders,quote_store=self.quotes),SimpleNamespace(conversation=Conversation(channel='wanda',**self.ident),image_context={}), '按新截图改价',cfg,None)
        self.detail=dict(orderId='123',accountUnb='s',orderStatus=1,payment='3290',payTime=None,grouponPending=False)
    def request(self):
        with patch.object(purchase_actions,'platform_order',return_value=self.detail):
            return self.tools.request_latest_quote_reprice()
    def test_cross_route_only_commits_after_verified_receipt(self):
        self.assertEqual(self.request()['status'],'REQUESTED')
        cmd=self.tools.flow_result.pending_commands[0]
        self.assertEqual(cmd.extra['target_amount_fen'],13560)
        stores=SimpleNamespace(orders=self.orders,quotes=self.quotes)
        authorize_quote_revision(stores,cmd)
        self.assertEqual(self.orders.get('o').provider,'LIANGPIAO')
        cmd.extra['price_after_fen']=13560;cmd.status='SUCCEEDED'
        settle_quote_revision(stores,cmd)
        o=self.orders.get('o')
        self.assertEqual((o.provider,o.amount_fen,o.ticket_count),('WANDA',13560,3))
        self.assertTrue(o.payload['manual_ticketing'])
    def test_paid_query_blocks_request_but_returns_explanation(self):
        self.detail.update(orderStatus=2,payTime='now')
        self.assertFalse(self.request()['unpaid'])
        self.assertIsNone(self.tools.flow_result)
    def test_unknown_query_does_not_queue(self):
        with patch.object(purchase_actions,'platform_order',side_effect=TimeoutError):
            self.assertEqual(self.tools.request_latest_quote_reprice()['status'],'QUERY_UNKNOWN')
        self.assertIsNone(self.tools.flow_result)
    def test_repeated_tool_call_only_one_proposal(self):
        self.request();first=self.tools.flow_result.pending_commands[0].command_id
        self.request();self.assertEqual(self.tools.flow_result.pending_commands[0].command_id,first)
    def test_legacy_wanda_deadline_does_not_block_request(self):
        self.q.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);self.quotes.put(self.q)
        self.assertEqual(self.request()["status"],"REQUESTED")
    def test_background_quote_cannot_switch_route(self):
        self.assertEqual(build_quote_reprice(SimpleNamespace(orders=self.orders,quotes=self.quotes),self.ident,'new'),[])
    def test_bound_quote_wrong_platform_amount_can_request_repair(self):
        o=self.orders.get('o');o.quote_id='new';o.provider='WANDA';o.amount_fen=13560;self.orders.put(o)
        self.assertEqual(self.request()['status'],'REQUESTED')
    def test_real_client_projection_rejects_wrong_shop(self):
        import json
        with patch.object(purchase_actions.PluginShopsClient,'_post',return_value=(200,json.dumps(dict(self.detail,accountUnb='other')))),patch.object(purchase_actions,'resolve_plugin_secret',return_value='test'):
            self.assertEqual(self.tools.read_platform_order()['status'],'QUERY_UNKNOWN')


    def test_agent_tool_loop_proposes_without_claiming_success(self):
        from tests.test_buyer_assistant import ScriptedModel, tool
        from integrations.model.client import ModelReply
        from application import service,conversation_ai
        svc=service.TicketService.__new__(service.TicketService)
        svc.persist=False;svc.order_store=self.orders;svc.quote_store=self.quotes
        conv=self.tools.conv
        session=service.Session(chat_id='c',conversation=conv);svc.sessions={'c':session}
        svc.ai=conversation_ai.ConversationAI(ScriptedModel([
            tool('record_buyer_intent', intent='reprice', evidence='按新截图改价'),
            tool('read_platform_order'),
            tool('request_latest_quote_reprice', order_no='123', quote_id='new', demand_id='', demand_rev=0),
            ModelReply(content='已经改好了')]))
        with patch.object(purchase_actions,'platform_order',return_value=self.detail):
            out=svc._answer_with_ai(session,'按新截图改价',self.tools.cfg)
        self.assertEqual(len(out.pending_commands),1)
        self.assertNotIn('已经改好了',''.join(out.messages))
        self.assertEqual(self.orders.get('o').amount_fen,3290)

    def test_local_paid_platform_stale_unpaid_never_invites_payment(self):
        o=self.orders.get('o');o.quote_id='new';o.provider='WANDA';o.amount_fen=13560;o.payment_state='CONFIRMED';o.paid_fen=13560;self.orders.put(o)
        self.detail['payment']='13560'
        with patch.object(purchase_actions,'platform_order',return_value=self.detail):
            facts=self.tools.read_platform_order()
        self.assertTrue(facts['payment_blocked'])
        self.assertFalse(facts['matches_latest_quote'])
        self.assertNotEqual(self.tools.guard_reply('直接付款就行'),'直接付款就行')

    def test_second_active_order_added_before_authorization_blocks(self):
        self.request();cmd=self.tools.flow_result.pending_commands[0]
        self.orders.create(Order(order_id='o2',quote_id='q2',idempotency_key='i2',provider='WANDA',provider_order_id='456',**self.ident))
        with self.assertRaisesRegex(ValueError,'AMBIGUOUS'):
            authorize_quote_revision(SimpleNamespace(orders=self.orders,quotes=self.quotes),cmd)

    def test_agent_continues_after_three_read_rounds(self):
        from tests.test_buyer_assistant import ScriptedModel,tool
        from application import conversation_ai
        model=ScriptedModel([tool('get_purchase_guidance'),tool('read_order'),tool('read_platform_order'),tool('request_latest_quote_reprice')])
        ai=conversation_ai.ConversationAI(model)
        with patch.object(purchase_actions,'platform_order',return_value=self.detail):
            result=ai.answer('我看到还没有改价',cfg=self.tools.cfg,tools=self.tools,conv=self.tools.conv)
        self.assertEqual(result['source'],'workflow')
        self.assertFalse(result['handoff'])
        self.assertEqual(len(self.tools.flow_result.pending_commands),1)
        self.assertEqual(len(model.seen),4)
