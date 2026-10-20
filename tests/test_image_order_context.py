import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from application import service, plugin_runtime, ledger, store
from application.image_context import preserve_image_result, handle_pending_image
from application.ticket_flow import FlowResult
from domain.conversation import Conversation
from domain.order import Order, OrderStore
from domain.quote import QuoteRecord, QuoteStore
import settings
import templates

class ImageOrderTests(unittest.TestCase):
    def setUp(self):
        self.ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
        self.session=service.Session(chat_id='c',conversation=Conversation(**self.ident,channel='wanda'))
        self.session.image_context=dict(self.ident,url='first',verified=True,summary='影院｜影片｜场次',created_at=datetime.now(timezone.utc).isoformat())
        self.svc=SimpleNamespace(session=lambda _:self.session,_save=Mock(),flow=SimpleNamespace(on_buyer_confirm=Mock()))
        self.orders,self.quotes=OrderStore(),QuoteStore()
        self.addCleanup(self.orders.close);self.addCleanup(self.quotes.close)
    def test_failed_second_image_preserves_verified_context(self):
        old=deepcopy(self.session.image_context);conv=deepcopy(self.session.conversation.__dict__)
        self.session.image_context=dict(self.ident,url='second')
        out=preserve_image_result(self.session,old,conv,'second',FlowResult(ok=False,messages=['不是选座图']))
        self.assertEqual(self.session.image_context['url'],'first')
        self.assertEqual(self.session.image_context['unverified_image'],'second')
        self.assertIn('上一张',out.messages[0])
        with patch('application.buyer_assistant.BuyerAssistantTools') as cls:
            reply=handle_pending_image(self.svc,self.session,'两张',settings.Settings())
            cls.assert_not_called()
            self.assertEqual(self.session.image_context['ticket_count'],2)
            self.assertIn('确认',reply.messages[0])
            tool=cls.return_value;tool.flow_result=FlowResult(ok=True,template_keys=['QUOTE_CARD'])
            handle_pending_image(self.svc,self.session,'按上一张',settings.Settings())
            tool._resume_quote.assert_called_once_with(ticket_count=2)
            self.assertNotIn('unverified_image',self.session.image_context)
    def test_questions_and_negations_do_not_confirm(self):
        for text in ('要按上一张吗？', '按上一张多少钱', '如果同一场呢', '不要按上一张', '不是同一场'):
            self.session.image_context['unverified_image'] = 'second'
            with patch('application.buyer_assistant.BuyerAssistantTools') as tool:
                handle_pending_image(self.svc,self.session,text,settings.Settings())
                tool.assert_not_called()
                self.assertEqual(self.session.image_context['unverified_image'],'second')

    def test_new_order_with_unit_quote_never_rebinds_old(self):
        old=Order(order_id='old',quote_id='oldq',idempotency_key='old',provider='WANDA',provider_order_id='old-platform',state='AWAITING_PAYMENT',**self.ident)
        self.orders.create(old)
        self.quotes.create(QuoteRecord(quote_id='unit',ticket_count=0,metadata={'quote_kind':'UNIT_PRICE'},**self.ident))
        event=dict(self.ident,payload={'orderId':'new-platform'})
        with patch.object(ledger,'stores',return_value=SimpleNamespace(orders=self.orders,quotes=self.quotes)),patch.object(plugin_runtime,'get_local_service',return_value=self.svc):
            commands=plugin_runtime._from_order_card(event)
        self.assertEqual(self.orders.get('old').provider_order_id,'old-platform')
        self.assertEqual(self.session.pending_order_card,event)
        self.assertFalse(any(c.type=='CHANGE_ORDER_PRICE' for c in commands))
        self.svc.flow.on_buyer_confirm.assert_not_called()
    def test_closed_notification_not_buyer_question(self):
        event={'event':'im.message.received','payload':{'messageType':14,'content':'未付款，你关闭了订单'}}
        with patch.object(plugin_runtime,'_from_buyer_message') as f:
            self.assertEqual(plugin_runtime.events_to_commands(event),[]);f.assert_not_called()
    def test_bargain_system_tip_is_ignored_but_buyer_text_is_preserved(self):
        text='不想宝贝被砍价? <a href="fleamarket://message_no_bargain?bizId=bargain">设置不砍价回复</a>'
        event={'event':'im.message.received','payload':{'messageType':14,'content':text}}
        with patch.object(plugin_runtime,'_from_buyer_message',return_value=[]) as f:
            self.assertEqual(plugin_runtime.events_to_commands(event),[])
            f.assert_not_called()
            event['payload']['messageType']=1
            plugin_runtime.events_to_commands(event)
            f.assert_called_once()
    def test_cost_failure_keeps_new_image_instead_of_restoring_old_quote(self):
        old=deepcopy(self.session.image_context);conv=deepcopy(self.session.conversation.__dict__)
        self.session.image_context=dict(self.ident,url='second')
        self.session.conversation.quote_id=''
        out=FlowResult(ok=False,messages=['会员价缺失'],template_keys=['WANDA_QUOTE_FAILED'])
        preserve_image_result(self.session,old,conv,'second',out)
        self.assertEqual(self.session.image_context['url'],'second')
        self.assertFalse(self.session.image_context['verified'])
        self.assertNotIn('unverified_image',self.session.image_context)
        self.assertEqual(out.messages,['会员价缺失'])
    def test_wanda_notice_default_loaded_with_old_empty_overrides(self):
        row=templates.find('PURCHASE_NOTICE');old=deepcopy(row)
        try:
            with patch.object(store,'_read_json',return_value={'PURCHASE_NOTICE':{'channel_text':{}}}):store.load_template_overrides()
            text=templates.text_for('PURCHASE_NOTICE','wanda')
            self.assertNotIn('40分钟',text);self.assertNotIn('系统自动',text)
        finally:row.clear();row.update(old)

if __name__=='__main__':unittest.main()

class FullContinuationTest(unittest.TestCase):
    def test_two_images_quantity_confirmation_create_new_order(self):
        from tests.test_buyer_assistant import BuyerAssistantTests, ScriptedModel
        from tests._check_channel_route import flow, result, MAP_WANDA
        from application.conversation_ai import ConversationAI
        from integrations.liangpiao.client import LiangPiaoError
        fixture=BuyerAssistantTests();fixture.setUp()
        try:
            f,api,_=flow(MAP_WANDA);f.quote_store=fixture.quotes;f.order_store=fixture.orders
            def recognize(url,city):
                if url=='second':raise LiangPiaoError(400001,'not seat')
                return result(expected_seat_count=None)
            api.ch7_recognize=Mock(side_effect=recognize)
            fixture.svc.flow=f;fixture.svc.ai=ConversationAI(ScriptedModel([]))
            fixture.cfg.assistant.agent_enabled=False  # Explicitly exercise the no-model fallback.
            event=dict(fixture.ident,payload={'orderId':'new-platform'})
            with patch('domain.quote.models._now',return_value=datetime(2026,10,13,tzinfo=timezone.utc)),patch('application.ticket_flow._second_quote_wait_seconds',return_value=0),patch.object(plugin_runtime,'get_local_service',return_value=fixture.svc),patch.object(ledger,'stores',return_value=SimpleNamespace(orders=fixture.orders,quotes=fixture.quotes)),patch.object(plugin_runtime,'build_purchase_notice_commands',return_value=[]):
                fixture.svc.on_message('c','',image_url='first',config=fixture.cfg)
                failed=fixture.svc.on_message('c','',image_url='second',config=fixture.cfg)
                self.assertIn('IMAGE_CONTEXT_CONFIRM',failed.template_keys)
                plugin_runtime._from_order_card(event)
                count=api.ch7_recognize.call_count
                fixture.svc.on_message('c','两张',config=fixture.cfg)
                self.assertEqual(api.ch7_recognize.call_count,count)
                final=fixture.svc.on_message('c','按上一张',config=fixture.cfg)
                self.assertIn('QUOTE_CARD',final.template_keys)
                self.assertEqual(api.ch7_recognize.call_args.args[0],'first')
                self.assertTrue(fixture.session.image_context['verified'])
                commands=plugin_runtime._from_order_card(fixture.session.pending_order_card)
                self.assertTrue(any(c.type=='CHANGE_ORDER_PRICE' for c in commands))
                order=fixture.orders.find_by_provider_order('new-platform')
                self.assertEqual(order.ticket_count,2)
                self.assertEqual(order.quote_id,fixture.conv.quote_id)
                self.assertFalse(fixture.session.pending_order_card)
        finally:fixture.tearDown()
