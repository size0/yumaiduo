import unittest
from types import SimpleNamespace
from unittest.mock import patch

from application import conversation_ai, ledger, plugin_runtime
from application.order_status_sync import is_delivery_notice, sync_delivery
from integrations.model.client import ModelReply
from tests import test_buyer_assistant as fixtures
from tests.test_buyer_assistant import ScriptedModel, tool


class PostDeliveryReplyTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.BuyerAssistantTests();f.setUp();self.addCleanup(f.tearDown)
        return f

    def test_other_intent_must_use_current_message_not_history(self):
        f=self.fixture();t=f.tools('ok');t.enable_semantic_routing()
        definition=next(s for s in t.schemas if s['function']['name']=='record_buyer_intent')
        self.assertEqual(definition['function']['parameters']['properties']['evidence']['enum'],['ok'])
        result=t.record_buyer_intent('other','你已发货')
        self.assertFalse(result['ok']);self.assertEqual(result['expected_evidence'],'ok')
        self.assertTrue(t.record_buyer_intent('other','ok')['ok'])

    def test_acknowledgement_does_not_need_order_query_or_write(self):
        f=self.fixture();f.add_order(payment_state='CONFIRMED',state='PAID',shipped=True)
        t=f.tools('ok');t.enable_semantic_routing()
        model=ScriptedModel([tool('record_buyer_intent',intent='other',evidence='ok'),ModelReply(content='好的～')])
        reply,_,used=conversation_ai.ConversationAI(model)._chat_with_tools([], '', t)
        self.assertEqual(t.guard_reply(reply.content),'好的～')
        self.assertEqual(used,['record_buyer_intent'])
        f.svc.flow.on_screenshot.assert_not_called()

    def test_exhausted_loop_does_not_inject_payment_task_or_internal_guidance(self):
        f=self.fixture();f.add_order(payment_state='CONFIRMED',state='PAID')
        t=f.tools('ok');t.enable_semantic_routing()
        model=ScriptedModel([tool('record_buyer_intent',intent='other',evidence='old') for _ in range(conversation_ai.MAX_TOOL_ROUNDS)])
        reply,_,_=conversation_ai.ConversationAI(model)._chat_with_tools([], '', t)
        final=t.guard_reply(reply.content)
        self.assertNotIn('付款',final);self.assertNotIn('买家',final);self.assertNotIn('人工',final)

    def test_paid_guidance_is_buyer_facing(self):
        f=self.fixture();f.add_order(payment_state='CONFIRMED',state='PAID')
        text=f.tools('ok').get_purchase_guidance()['guidance']
        self.assertIn('已确认付款',text)
        self.assertNotIn('请查询',text);self.assertNotIn('买家',text)


class ShipmentNoticeTests(unittest.TestCase):
    def setUp(self):
        from domain.order import Order,OrderStore
        self.orders=OrderStore();self.addCleanup(self.orders.close)
        self.orders.create(Order(order_id='o',quote_id='q',idempotency_key='i',tenant_id='t',shop_id='s',
            buyer_id='b',chat_id='c',provider='WANDA',provider_order_id='p',amount_fen=6600,paid_fen=6600,
            payment_state='CONFIRMED',state='PAID'))
        self.event={'id':'e','tenantId':'t','event':'im.message.received','payload':
            {'accountUnb':'s','peerUnb':'b','chatId':'c','messageType':14,'content':'你已发货'}}
        self.detail={'orderId':'p','accountUnb':'s','orderStatus':3,'payment':'6600'}

    def test_notice_syncs_without_sending_or_inventing_ticket_image(self):
        with patch.object(ledger,'stores',return_value=SimpleNamespace(orders=self.orders)),patch(
                'application.order_status_sync.platform_order',return_value=self.detail),patch.object(plugin_runtime,'_from_buyer_message') as ai:
            commands=plugin_runtime.events_to_commands(self.event)
        ai.assert_not_called()
        self.assertTrue(all(c.extra.get('internal_only') and not c.text for c in commands))
        order=self.orders.get('o');self.assertTrue(order.shipped)
        self.assertEqual(order.ticket_codes,[]);self.assertNotIn('wanda_ticket_image',order.payload)

    def test_unverified_state_or_wrong_identity_cannot_sync(self):
        for key,value in [('orderStatus',2),('orderStatus','3'),('orderId','another'),('accountUnb','another'),('payment','100')]:
            with patch('application.order_status_sync.platform_order',return_value=dict(self.detail,**{key:value})):
                sync_delivery(self.event,self.orders)
            self.assertFalse(self.orders.get('o').shipped)

    def test_same_words_from_buyer_are_not_system_notice(self):
        self.event['payload']['messageType']=1
        self.assertFalse(is_delivery_notice(self.event))

    def test_missing_order_id_and_multiple_orders_never_guess(self):
        from domain.order import Order
        self.orders.create(Order(order_id='o2',quote_id='q2',idempotency_key='i2',tenant_id='t',shop_id='s',
            buyer_id='b',chat_id='c',provider='WANDA',provider_order_id='p2',amount_fen=6600,state='PAID'))
        with patch('application.order_status_sync.platform_order') as read:
            sync_delivery(self.event,self.orders)
        read.assert_not_called()
        self.assertFalse(self.orders.get('o').shipped)
