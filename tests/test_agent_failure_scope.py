"""Regression cases from continuous buyer tests and Wennier's live dialogue."""
import unittest
from datetime import datetime, timezone

from application.assistant_reply import failure_reply, unfinished_reply
from application.ticket_details import parse_seats
from domain.quote import QuoteRecord
from tests import test_buyer_assistant as fixtures


class FailureScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BuyerAssistantTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.conv.channel = 'wanda'

    def tools(self, text='改成四张'):
        t = self.f.tools(text)
        t.enable_semantic_routing()
        t.reply_metadata = {'intent': 'other', 'kind': 'explanation', 'claims': []}
        return t

    def test_paid_change_preserves_real_failure_reason(self):
        t = self.tools()
        t.semantic_intent = 'revise'
        t.tool_results = [('update_ticket_request', {'ok': False, 'status': 'PAID_ORDER_LOCKED',
                          'reason': '已有付款或处理中的订单，补充信息需人工核对，不自动替换'})]
        reply = t.review_reply('这笔订单已付款，不能自动改成四张，需要人工核对。').text
        self.assertIn('付款', reply)
        self.assertIn('人工核对', reply)
        self.assertNotIn('需要确认本次要几张', reply)
        self.assertTrue(t.review_reply('[转人工]这笔订单已付款，需要人工核对。').text.startswith('[转人工]'))

    def test_ambiguous_orders_has_specific_next_question(self):
        t = self.tools('帮我改价')
        t.semantic_intent = 'reprice'
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'ORDER_AMBIGUOUS'})]
        self.assertIn('哪一笔', failure_reply(t))

    def test_failed_platform_read_without_intent_has_concrete_explanation(self):
        t = self.tools('是')
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'QUERY_UNKNOWN'})]
        self.assertIn('平台订单', unfinished_reply(t))
        self.assertNotIn('没理解清楚', unfinished_reply(t))

    def test_unbound_order_is_not_reported_as_not_purchased(self):
        t = self.tools('链接改价？')
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'NO_BOUND_ORDER'})]
        self.assertIn('订单卡片', failure_reply(t))
        self.assertNotIn('尚未拍下', failure_reply(t))

    def test_retry_success_removes_previous_failure(self):
        t = self.tools()
        t.semantic_intent = 'reprice'
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'QUERY_UNKNOWN'}),
                          ('read_platform_order', {'ok': True, 'status': 'MATCHED'})]
        self.assertEqual(failure_reply(t), '')

    def test_cross_tool_recovery_only_clears_covered_fields(self):
        t = self.tools()
        t.semantic_intent = 'revise'
        failed = ('update_ticket_request', {'ok': False, 'status': 'INVALID_EVIDENCE', 'request_fields': ['ticket_count']})
        t.tool_results = [failed, ('supplement_ticket_details', {'ok': True, 'request_fields': ['date']})]
        self.assertTrue(failure_reply(t))
        t.tool_results.append(('supplement_ticket_details', {'ok': True, 'request_fields': ['date', 'ticket_count']}))
        self.assertEqual(failure_reply(t), '')

    def test_same_write_tool_success_cannot_hide_failure_of_another_field(self):
        t = self.tools()
        t.semantic_intent = 'revise'
        t.tool_results = [('supplement_ticket_details', {'ok': False, 'status': 'INVALID_FIELD_EVIDENCE', 'request_fields': ['seats']}),
                          ('supplement_ticket_details', {'ok': True, 'request_fields': ['date']})]
        self.assertTrue(failure_reply(t))
        t.tool_results.append(('supplement_ticket_details', {'ok': True, 'request_fields': ['seats']}))
        self.assertEqual(failure_reply(t), '')

    def test_faq_failure_does_not_mask_known_answer(self):
        t = self.tools('谢谢')
        t.tool_results = [('search_faq', {'ok': False, 'status': 'POLICY_NOT_FOUND'})]
        self.assertEqual(t.review_reply('不客气。').text, '不客气。')

    def test_redundant_history_fields_do_not_block_successful_current_date(self):
        self.f.session.image_context = dict(self.f.ident, awaiting_image_request={
            'demand_id': '', 'demand_rev': 0,
            'movie': '奥德赛', 'time': '22:30', 'cinema': '影院', 'ticket_count': 2})
        t = self.tools('今天')
        fields = {'movie': '奥德赛', 'time': '2230', 'date': '今天'}
        self.assertFalse(t.execute('supplement_ticket_details', fields)['ok'])
        t.execute('record_buyer_intent', {'intent': 'revise'})
        self.assertFalse(t.execute('supplement_ticket_details', fields)['ok'])
        self.assertTrue(t.execute('supplement_ticket_details', {'date': '今天'})['ok'])
        self.assertEqual(failure_reply(t), '')
        self.assertNotIn('核对通过', t.review_reply('日期记下了，请发对应场次截图。').text)

    def test_old_or_unversioned_fields_cannot_clear_rejected_input(self):
        from application.request_failure import already_saved_fields
        self.f.conv.demand_id, self.f.conv.demand_rev = 'new', 2
        for version in ({}, {'demand_id': 'new', 'demand_rev': 1}, {'demand_id': 'old', 'demand_rev': 2}):
            self.f.session.image_context = dict(self.f.ident, ticket_details={
                'movie': '奥德赛', 'position_preference': '8排中间', 'pending': True, **version})
            t = self.tools('今天')
            self.assertFalse(already_saved_fields(t, {'movie': '奥德赛'}))
            self.assertNotIn('8排中间', unfinished_reply(t))

    def test_ambiguous_time_cannot_be_used_as_saved_equivalence(self):
        from application.request_failure import already_saved_fields
        self.f.session.image_context = dict(self.f.ident, awaiting_image_request={
            'demand_id': '', 'demand_rev': 0, 'time': '09:15', 'ambiguous_time': True})
        self.assertFalse(already_saved_fields(self.tools(), {'time': '9:15'}))

    def test_pending_preference_fallback_is_specific_not_misunderstanding(self):
        self.seed_image_quote()
        t = self.tools('8排最中间')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        t.execute('supplement_ticket_details', {'position_preference': '8排最中间'})
        later = self.tools('多少钱')
        self.assertIn('位置和价格还未核实', unfinished_reply(later))
        self.assertNotIn('没理解清楚', unfinished_reply(later))

    def test_handoff_tool_sets_real_workflow_flag_without_transactions(self):
        t = self.tools('转人工')
        t.execute('request_human_handoff', {})
        self.assertTrue(t.flow_result.need_human)
        self.assertFalse(t.flow_result.pending_commands)
        self.assertIn('登记', t.flow_result.messages[0])
        self.assertFalse(self.f.orders.by_identity(self.f.ident))

    def test_handoff_text_without_action_requires_repair(self):
        t = self.tools('转人工')
        d = t.review_reply('已帮你转人工，店家稍后接手。')
        self.assertEqual(d.reason, 'NEEDS_REPAIR')
        self.assertIn('request_human_handoff', d.feedback)

    def test_automatic_identity_with_offer_is_not_completed_handoff(self):
        t = self.tools('是人工吗')
        reply = '当前是店铺自动回复，需要人工的话我可以帮你转人工。'
        self.assertEqual(t.review_reply(reply).text, reply)
        self.assertEqual(t.review_reply(reply).reason, 'ACCEPTED')

    def test_handoff_tool_survives_service_boundary(self):
        from application.conversation_ai import ConversationAI
        self.f.svc.ai = ConversationAI(fixtures.ScriptedModel([fixtures.tool('request_human_handoff')]))
        result = self.f.svc.on_message('c', '转人工', config=self.f.cfg)
        self.assertTrue(result.need_human)
        self.assertIn('已登记', ''.join(result.messages))

    def test_internal_error_is_not_sent_to_buyer(self):
        t = self.tools('我看还是原价')
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'QUERY_UNKNOWN',
                          'reason': 'TimeoutError https://private.example/token=secret'})]
        self.assertNotIn('secret', failure_reply(t))
        self.assertNotIn('TimeoutError', failure_reply(t))

    def test_old_order_never_projects_amount_into_new_demand(self):
        self.f.add_order(demand_id='old', demand_rev=1, amount_fen=6600)
        self.f.conv.demand_id, self.f.conv.demand_rev = 'new', 2
        r = self.tools().get_purchase_guidance()
        self.assertTrue(r['payment_blocked'])
        self.assertNotIn('66', r['guidance'])
        self.assertIn('本次', r['guidance'])

    def test_same_id_different_revision_is_not_current(self):
        self.f.add_order(demand_id='d', demand_rev=1, amount_fen=6600)
        self.f.conv.demand_id, self.f.conv.demand_rev = 'd', 2
        self.assertNotIn('66', self.tools().get_purchase_guidance()['guidance'])

    def test_current_paid_order_still_blocks_payment(self):
        self.f.add_order(demand_id='d', demand_rev=2, payment_state='CONFIRMED', state='PAID')
        self.f.conv.demand_id, self.f.conv.demand_rev = 'd', 2
        r = self.tools().get_purchase_guidance()
        self.assertTrue(r['payment_blocked'])
        self.assertIn('已确认付款', r['guidance'])

    def test_quote_identity_fallback_excludes_other_quote(self):
        self.f.add_order(quote_id='old-q', amount_fen=6600)
        self.f.conv.quote_id = 'new-q'
        self.assertNotIn('66', self.tools().get_purchase_guidance()['guidance'])

    def test_chinese_seats_and_adjacent_labels(self):
        for value in ('六排十座十一座', '6排10座11座', '六排十、十一座', '第六排10，11座'):
            with self.subTest(value=value):
                self.assertEqual([s['seatNo'] for s in parse_seats(value)], ['6排10座', '6排11座'])

    def test_invalid_or_ambiguous_seats_remain_rejected(self):
        for value in ('15 15', '六排十座十座', '零排十座', '6排10或者11座', '6排10座7排11座'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_seats(value)

    def seed_image_quote(self):
        ctx = self.f.session.image_context
        ctx.update(channel='wanda', verified=True, ticket_count=2,
                   recognition_facts={'match_level': 'EXACT', 'cinema_id': 10,
                     'cinema_name': '影院', 'movie_name': '影片', 'start_time': '2026-10-17T20:15:00+08:00',
                     'seats': [{'rowNo': 9, 'colNo': 10, 'seatNo': '9排10座'},
                               {'rowNo': 9, 'colNo': 11, 'seatNo': '9排11座'}]})
        self.f.conv.demand_id, self.f.conv.demand_rev = 'd', 1
        self.f.quotes.create(QuoteRecord(quote_id='q', demand_id='d', demand_rev=1,
             provider='WANDA', ticket_count=2, unit_sell_price_fen=2900,
             total_sell_price_fen=5800, created_at=datetime.now(timezone.utc), **self.f.ident))
        self.f.conv.quote_id = 'q'

    def test_seat_preference_keeps_count_and_invalidates_old_exact_quote(self):
        self.seed_image_quote()
        t = self.tools('8排最中间')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        r = t.execute('supplement_ticket_details', {'position_preference': '8排最中间'})
        self.assertTrue(r['ok'])
        d = self.f.session.image_context['ticket_details']
        self.assertEqual(d['position_preference'], '8排最中间')
        self.assertEqual(d['ticket_count'], 2)
        self.assertFalse(d.get('seats'))
        self.assertEqual(self.f.quotes.get('q').status, 'SUPERSEDED')
        self.assertFalse(t.read_quote()['quote']['valid'])
        self.assertEqual(r['known_fields']['ticket_count'],2)
        self.assertIn('seats',r['missing_fields'])
        self.assertNotIn('seats',r['known_fields'])
        self.f.svc.flow.on_screenshot.assert_not_called()
        self.assertNotIn('58', t.get_purchase_guidance()['guidance'])

    def test_without_image_paid_order_preference_is_still_blocked(self):
        self.f.session.image_context = dict(self.f.ident)
        self.f.add_order(payment_state='CONFIRMED', state='PAID')
        t = self.tools('8排最中间')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        r = t.execute('supplement_ticket_details', {'position_preference': '8排最中间'})
        self.assertFalse(r['ok'])
        self.assertNotIn('awaiting_image_request', self.f.session.image_context)

    def test_text_preference_then_image_cannot_be_reduced_to_quantity(self):
        from copy import deepcopy
        from application.seat_request import reconcile
        from application.image_context import preserve_image_result
        from application.ticket_flow import FlowResult
        from integrations.liangpiao.api import RecognitionResult
        self.f.session.image_context = dict(self.f.ident)
        t = self.tools('影院 影片 今天20:15 两张 8排最中间')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        t.execute('update_ticket_request', {'intent': 'revise', 'quantity': 2, 'quantity_text': '两张'})
        t.execute('supplement_ticket_details', {'position_preference': '8排最中间', 'movie': '影片',
                  'date': '今天', 'time': '20:15'})
        previous = deepcopy(self.f.session.image_context)
        request = previous['awaiting_image_request']
        r = RecognitionResult(match_level='EXACT', cinema_name='影院', movie_name='影片',
                              start_time=request['date']+'T20:15:00+08:00', cinema_truncated=False)
        out = reconcile(self.f.conv, r, request, self.f.quotes, FlowResult())
        self.assertIsNotNone(out)
        self.assertFalse(out.ok)
        self.f.session.image_context = {**self.f.ident, 'url': 'https://example.com/new.png'}
        preserve_image_result(self.f.session, previous, {}, 'https://example.com/new.png', out)
        saved = self.f.session.image_context['ticket_details']
        self.assertEqual(saved['position_preference'], '8排最中间')
        self.assertEqual(saved['ticket_count'], 2)
        self.assertTrue(saved['pending'])
        self.assertFalse(self.f.conv.quote_id)
        # Explicit original seat numbers consume any legacy duplicate pending request.
        self.f.session.image_context['awaiting_image_request'] = request
        t2 = self.tools('八排十座十一座')
        t2.execute('record_buyer_intent', {'intent': 'revise'})
        t2.execute('supplement_ticket_details', {'seats': '八排十座十一座'})
        self.assertNotIn('awaiting_image_request', self.f.session.image_context)
        self.assertNotIn('position_preference', self.f.session.image_context['ticket_details'])
        exact = self.f.session.image_context['ticket_details']
        r2 = RecognitionResult(match_level='EXACT', cinema_name='影院', movie_name='影片',
                               start_time=request['date']+'T20:15:00+08:00', cinema_truncated=False)
        self.assertIsNone(reconcile(self.f.conv, r2, exact, self.f.quotes, FlowResult()))
        self.assertEqual([s['seatNo'] for s in r2.seats], ['8排10座', '8排11座'])

    def test_preference_must_be_literal_and_paid_order_not_changed(self):
        self.seed_image_quote()
        t = self.tools('8排最中间')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        self.assertFalse(t.execute('supplement_ticket_details', {'position_preference': '7排最中间'})['ok'])
        self.assertEqual(self.f.quotes.get('q').status, 'READY')
        self.f.add_order(payment_state='CONFIRMED', state='PAID')
        self.assertFalse(t.execute('supplement_ticket_details', {'position_preference': '8排最中间'})['ok'])
        self.assertEqual(self.f.quotes.get('q').status, 'READY')

    def test_chinese_literal_passes_evidence_and_preserves_old_order(self):
        self.seed_image_quote()
        t = self.tools('六排十座十一座')
        t.execute('record_buyer_intent', {'intent': 'revise'})
        # No supplier result is asserted here; the explicit original seats must be saved.
        t.execute('supplement_ticket_details', {'seats': '六排十座十一座'})
        d = self.f.session.image_context['ticket_details']
        self.assertEqual([s['seatNo'] for s in d['seats']], ['6排10座', '6排11座'])
        t2 = self.tools('六排十座十一座')
        t2.execute('record_buyer_intent', {'intent': 'revise'})
        self.assertEqual(t2.execute('supplement_ticket_details', {'seats': '6排10、11座'})['status'], 'INVALID_FIELD_EVIDENCE')


if __name__ == '__main__':
    unittest.main()

