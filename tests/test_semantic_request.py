import unittest
from unittest.mock import patch
from types import SimpleNamespace
from application.buyer_assistant import BuyerAssistantTools
from application import conversation_ai
from integrations.model.client import ModelReply
from tests.test_buyer_assistant import BuyerAssistantTests, ScriptedModel, tool
from tests.test_wanda_inquiry import InquiryTests
from tests.test_agent_purchase import AgentPurchaseTests


class SemanticRequestTests(unittest.TestCase):
    def fixture(self):
        f = BuyerAssistantTests(); f.setUp(); self.addCleanup(f.tearDown)
        f.conv.channel = 'wanda'; f.session.image_context['channel'] = 'wanda'
        return f

    def test_multiple_quantity_phrasings_reach_agent_before_regex(self):
        for text in ('四张', '我们四个人', '不是三张，是四张', '还是改成四张吧'):
            with self.subTest(text=text):
                f = self.fixture()
                model = ScriptedModel([tool('update_ticket_request', intent='revise',
                                            quantity=4, quantity_text='四', evidence=text)])
                f.svc.ai = conversation_ai.ConversationAI(model)
                result = f.svc.on_message('c', text, config=f.cfg)
                self.assertEqual(len(model.seen), 1)
                self.assertEqual(f.svc.flow.on_screenshot.call_args.kwargs['ticket_count'], 4)
                self.assertEqual(result.pending_commands, [])

    def test_inquiry_variants_never_mutate_order_or_quote(self):
        for text in ('四张多少钱', '我们四个人，合计多少', '六排四张怎么卖', '四张和三张哪个划算'):
            with self.subTest(text=text):
                f = InquiryTests(); f.setUp(); self.addCleanup(f.doCleanups)
                t = BuyerAssistantTools(f.svc, f.session, text, f.tools.cfg, None)
                t.enable_semantic_routing()
                before = f.quotes.get('new').to_dict()
                result = t.execute('update_ticket_request', dict(intent='inquire', quantity=4,
                                     quantity_text='四', evidence=text))
                self.assertTrue(result['ok'])
                self.assertIn('187.60', result['guidance'])
                self.assertEqual(t.flow_result.pending_commands, [])
                self.assertEqual(f.quotes.get('new').to_dict(), before)
                f.svc.flow.on_screenshot.assert_not_called()

    def test_fabricated_quantity_evidence_is_rejected(self):
        f = self.fixture(); t = f.tools('三张'); t.enable_semantic_routing()
        r = t.execute('update_ticket_request', dict(intent='revise', quantity=4,
                                                   quantity_text='四', evidence='三张'))
        self.assertFalse(r['ok']); f.svc.flow.on_screenshot.assert_not_called()

    def test_ambiguous_alternatives_can_be_clarified_without_commands(self):
        f = self.fixture()
        model = ScriptedModel([tool('record_buyer_intent', intent='other', evidence='三张或者四张吧'), ModelReply(content='你是要三张还是四张？')])
        f.svc.ai = conversation_ai.ConversationAI(model)
        out = f.svc.on_message('c', '三张或者四张吧', config=f.cfg)
        self.assertEqual(out.pending_commands, [])
        f.svc.flow.on_screenshot.assert_not_called()
        self.assertEqual(out.messages, ['你是要三张还是四张？'])

    def test_old_order_new_image_quantity_recovers_from_premature_reply(self):
        f = self.fixture(); f.add_order(provider='WANDA', amount_fen=7380)
        f.session.history = [{'role':'assistant','content':'上海新场次39元每张，需要几张？'}]
        before = f.orders.get('o').to_dict()
        model = ScriptedModel([
            ModelReply(content='旧订单73.80元，请先核对。'),
            tool('record_buyer_intent', intent='revise', evidence='三张'),
            ModelReply(content='好的，三张。'),
            tool('update_ticket_request', intent='revise', quantity=3, quantity_text='三', evidence='三张')])
        f.svc.ai = conversation_ai.ConversationAI(model)
        out = f.svc.on_message('c', '三张', config=f.cfg)
        self.assertEqual(f.svc.flow.on_screenshot.call_args.kwargs['ticket_count'],3)
        self.assertEqual(f.orders.get('o').to_dict(),before)
        self.assertEqual(out.pending_commands,[])
        self.assertEqual(out.messages,['核验后的报价'])
        self.assertEqual(len(model.seen),4)

    def test_no_tool_loop_is_bounded_and_never_executes_transaction(self):
        f = self.fixture(); t = f.tools('三张'); t.enable_semantic_routing()
        model = ScriptedModel([ModelReply(content='已改价117元') for _ in range(conversation_ai.MAX_TOOL_ROUNDS)])
        reply, rounds, used = conversation_ai.ConversationAI(model)._chat_with_tools([], '', t)
        self.assertLessEqual(rounds,conversation_ai.MAX_TOOL_ROUNDS)
        self.assertEqual(used,[])
        self.assertNotIn('已改价',reply.content)
        f.svc.flow.on_screenshot.assert_not_called()

    def test_legacy_quantity_tools_not_executable_in_semantic_mode(self):
        f = self.fixture(); t = f.tools('四张'); t.enable_semantic_routing()
        self.assertFalse(t.execute('supplement_ticket_count', {'evidence': '四张'})['ok'])
        f.svc.flow.on_screenshot.assert_not_called()

    def test_direct_reprice_without_intent_is_denied(self):
        f = AgentPurchaseTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.tools.text = '四张多少钱'; f.tools.enable_semantic_routing()
        result = f.tools.request_latest_quote_reprice(order_no='123', quote_id='new', demand_id='', demand_rev=0)
        self.assertEqual(result['status'], 'INTENT_REQUIRED')
        self.assertIsNone(f.tools.flow_result)
        self.assertTrue(f.tools.record_buyer_intent('inquire', f.tools.text)['ok'])
        self.assertFalse(f.tools.record_buyer_intent('reprice', f.tools.text)['ok'])
        self.assertEqual(f.tools.request_latest_quote_reprice()['status'], 'INTENT_REQUIRED')

    def test_stale_demand_revision_and_changed_order_are_denied(self):
        from application import purchase_actions
        from application.quote_reprice import build_quote_reprice, authorize_quote_revision
        f = AgentPurchaseTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.q.demand_id = 'd'; f.q.demand_rev = 2; f.quotes.put(f.q)
        with patch.object(purchase_actions, 'platform_order', return_value=f.detail):
            result = f.tools.request_latest_quote_reprice(order_no='123', quote_id='new', demand_id='d', demand_rev=1)
        self.assertEqual(result['status'], 'STALE_REFERENCE'); self.assertIsNone(f.tools.flow_result)
        stores = SimpleNamespace(orders=f.orders, quotes=f.quotes)
        self.assertEqual(build_quote_reprice(stores, f.ident, 'new', allow_wanda_switch=True,
                                            expected_order_no='other'), [])
        command = build_quote_reprice(stores, f.ident, 'new', allow_wanda_switch=True,
                                      expected_order_no='123')[0]
        command.extra['demand_rev'] = 1
        with self.assertRaisesRegex(ValueError, 'DEMAND_VERSION'):
            authorize_quote_revision(stores, command)
        self.assertEqual(f.orders.get('o').amount_fen, 3290)

    def test_candidate_menu_is_preserved_with_agent_enabled(self):
        from domain.conversation import ConvState
        from application.ticket_flow import FlowResult
        from unittest.mock import Mock
        f = self.fixture(); f.conv.state = ConvState.PICK_SHOW; f.session.last_question = 'pick'
        f.svc.ai = conversation_ai.ConversationAI(ScriptedModel([]))
        f.svc.flow.on_candidate_pick = Mock(return_value=FlowResult(ok=True, messages=['已选择']))
        f.svc.on_message('c', '场次1', config=f.cfg)
        f.svc.flow.on_candidate_pick.assert_called_once()
        self.assertEqual(f.svc.ai.client.seen, [])

    def test_new_inquiry_preserves_bound_order_and_quote(self):
        f = AgentPurchaseTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.tools.text = '另外问一下明天的'; f.tools.enable_semantic_routing()
        before = f.orders.get('o').to_dict()
        f.tools.record_buyer_intent('inquire', f.tools.text)
        r = f.tools.begin_new_inquiry(f.tools.text)
        self.assertEqual(r['status'], 'NEW_INQUIRY')
        self.assertEqual(f.orders.get('o').to_dict(), before)
        self.assertEqual(f.quotes.get('new').status, 'READY')
        self.assertEqual(f.tools.request_latest_quote_reprice()['status'], 'INQUIRY_ONLY')

    def test_independent_inquiry_can_be_promoted_after_explicit_purchase(self):
        from tests.test_ticket_details import TicketDetailsTests
        from application.demand import bind_quote
        from domain.quote import QuoteRecord
        from application.ticket_flow import FlowResult
        f = TicketDetailsTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.session.image_context['ticket_details'] = dict(
            movie='神探之痕', date='2026-10-16', time='21:15', ticket_count=3)
        f.conv.demand_inquiry = True; f.conv.demand_id = 'inquiry'; f.conv.demand_rev = 1
        def quoted(conv, result, out):
            q = QuoteRecord(quote_id='purchase', provider='WANDA', ticket_count=3,
                            **f.identity)
            bind_quote(conv, q, f.quotes); f.quotes.create(q)
            conv.quote_id = q.quote_id
            return FlowResult(ok=True, messages=['已重新核价'])
        f.flow._after_recognition.side_effect = quoted
        t = BuyerAssistantTools(f.service, f.session, '就买这三张', f.cfg, None)
        t.enable_semantic_routing(); t.record_buyer_intent('revise', t.text)
        result = t.confirm_inquiry_purchase(t.text)
        self.assertTrue(result['ok'])
        self.assertFalse(f.conv.demand_inquiry)
        self.assertFalse(f.quotes.get('purchase').metadata['inquiry_only'])
        self.assertEqual(f.orders.by_identity(f.identity), [])
        self.assertEqual(t.flow_result.pending_commands, [])

    def test_inquiry_purchase_cannot_duplicate_existing_order(self):
        f = AgentPurchaseTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.tools.text = '就买这张'; f.tools.enable_semantic_routing()
        f.tools.conv.demand_inquiry = True
        f.tools.record_buyer_intent('revise', f.tools.text)
        before = f.orders.get('o').to_dict()
        self.assertFalse(f.tools.confirm_inquiry_purchase(f.tools.text)['ok'])
        self.assertTrue(f.tools.conv.demand_inquiry)
        self.assertEqual(f.orders.get('o').to_dict(), before)

    def test_failed_inquiry_promotion_keeps_inquiry_only(self):
        f = self.fixture(); t = f.tools('就买这场'); t.enable_semantic_routing()
        t.conv.demand_inquiry = True
        t.record_buyer_intent('revise', t.text)
        with patch.object(t, '_resume_quote', return_value={'ok': False}):
            self.assertFalse(t.confirm_inquiry_purchase(t.text)['ok'])
        self.assertTrue(t.conv.demand_inquiry)
