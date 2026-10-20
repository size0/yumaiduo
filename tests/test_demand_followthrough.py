"""Buyer follow-through: compact time, text-first facts, and actionable replies."""
import unittest

from application.buyer_assistant import BuyerAssistantTools
from application.conversation_ai import ConversationAI
from application.ticket_details import parse_time, parse_seats
from integrations.model.client import ModelReply
from tests import test_buyer_assistant as fixtures
from tests.test_buyer_assistant import ScriptedModel


class DemandFollowthroughTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BuyerAssistantTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.conv.channel = 'wanda'
        self.f.session.image_context = {}

    def tools(self, text):
        tools = self.f.tools(text)
        tools.enable_semantic_routing()
        tools.execute('record_buyer_intent', {'intent': 'revise', 'evidence': text})
        return tools

    def test_compact_time_is_normalized_without_guessing_period(self):
        self.assertEqual([s['seatName'] for s in parse_seats('第一排6、7座')], ['1排6座', '1排7座'])
        self.assertEqual(parse_time('2230'), ('22:30', False))
        self.assertEqual(parse_time('0930'), ('09:30', True))
        for value in ('2460', '2400', '2360', '223', '22300'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time(value)

    def test_three_messages_keep_cinema_movie_time_and_quantity(self):
        first = self.tools('广州万达天河智慧城店')
        first.execute('supplement_ticket_details', {'evidence': first.text, 'cinema': first.text})
        second = self.tools('2230的奥德赛 两人')
        second.execute('supplement_ticket_details', {'evidence': second.text, 'movie': '奥德赛', 'time': '2230'})
        result = second.execute('update_ticket_request', {'evidence': second.text, 'intent': 'revise',
                                                        'quantity': 2, 'quantity_text': '两人'})
        self.assertTrue(result['ok'])
        pending = self.f.session.image_context['awaiting_image_request']
        self.assertEqual((pending['cinema'], pending['movie'], pending['time'], pending['ticket_count']),
                         ('广州万达天河智慧城店', '奥德赛', '22:30', 2))
        self.assertIn('date', result['missing_fields'])
        self.assertNotIn('movie', result['missing_fields'])
        self.assertIn('22:30', second.demand_context())
        third = self.tools('今天')
        result = third.execute('supplement_ticket_details', {'evidence': third.text, 'date': '今天'})
        self.assertEqual(self.f.session.image_context['awaiting_image_request']['ticket_count'], 2)
        self.assertNotIn('date', result['missing_fields'])
        self.f.svc.flow.on_screenshot.assert_not_called()
        self.assertEqual(self.f.orders.by_identity(self.f.ident), [])

    def test_quantity_first_is_not_lost_without_image(self):
        tools = self.tools('两人')
        result = tools.execute('update_ticket_request', {'evidence': tools.text, 'intent': 'revise',
                                                       'quantity': 2, 'quantity_text': '两人'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.f.session.image_context['awaiting_image_request']['ticket_count'], 2)

    def test_other_identity_pending_facts_are_not_inherited(self):
        self.f.session.image_context = dict(self.f.ident, shop_id='other', awaiting_image_request={'cinema': '别家影院'})
        tools = self.tools('两人')
        tools.execute('update_ticket_request', {'evidence': tools.text, 'intent': 'revise',
                                               'quantity': 2, 'quantity_text': '两人'})
        self.assertNotIn('cinema', self.f.session.image_context['awaiting_image_request'])

    def test_new_inquiry_does_not_inherit_old_text_demand(self):
        old = self.tools('广州万达天河智慧城店')
        old.execute('supplement_ticket_details', {'evidence': old.text, 'cinema': old.text})
        new = self.f.tools('另问奥德赛')
        new.enable_semantic_routing()
        new.execute('record_buyer_intent', {'evidence': new.text, 'intent': 'inquire'})
        new.execute('begin_new_inquiry', {'evidence': new.text})
        new.execute('supplement_ticket_details', {'evidence': new.text, 'movie': '奥德赛'})
        ctx = self.f.session.image_context
        self.assertNotIn('cinema', ctx['awaiting_image_request'])
        self.assertTrue(ctx['inquiry_only'])
        self.assertEqual(ctx['awaiting_image_request']['demand_id'], self.f.conv.demand_id)
        self.assertEqual(ctx['demand_archive'][-1]['awaiting_image_request']['cinema'], old.text)

    def test_waiting_without_a_task_is_replaced_by_specific_missing_information(self):
        tools = self.tools('2230的奥德赛')
        tools.execute('supplement_ticket_details', {'evidence': tools.text, 'movie': '奥德赛', 'time': '2230'})
        reply = '收到哈，两张~ 我这边按你发的截图核一下场次和价格，稍等。'
        self.assertTrue(tools.reply_feedback(reply))
        final = tools.guard_reply(reply)
        self.assertIn('观影日期', final)
        self.assertNotIn('稍等', final)
        self.assertNotIn('两张', final)

    def test_model_gets_feedback_instead_of_returning_an_idle_promise(self):
        tools = self.tools('2230的奥德赛')
        tools.execute('supplement_ticket_details', {'evidence': tools.text, 'movie': '奥德赛', 'time': '2230'})
        model = ScriptedModel([ModelReply(content='我帮你核一下价格，稍等。'),
                               ModelReply(content='请问哪天看？再发一下对应场次截图。')])
        reply, rounds, _ = ConversationAI(model)._chat_with_tools([], 'test', tools)
        self.assertEqual(rounds, 2)
        self.assertIn('哪天', reply.content)

    def test_final_round_keeps_specific_question(self):
        tools = self.tools('2230的奥德赛')
        tools.execute('supplement_ticket_details', {'evidence': tools.text, 'movie': '奥德赛', 'time': '2230'})
        model = ScriptedModel([ModelReply(content='稍等，我帮你查一下。') for _ in range(10)])
        reply, _, _ = ConversationAI(model)._chat_with_tools([], 'test', tools)
        self.assertIn('观影日期', reply.content)
        self.assertNotIn('稍等', reply.content)

    def test_verified_pending_ticket_order_can_explain_waiting(self):
        tools = self.tools('票什么时候出')
        self.f.add_order(provider='WANDA', state='PAID', payment_state='CONFIRMED')
        tools.execute('read_order', {})
        self.assertFalse(tools.reply_feedback('这笔订单已付款，正在等待出票，请稍等。'))
        self.assertFalse(tools.reply_feedback('这笔订单已付款，店家正在出票，请稍等。'))
        self.assertTrue(tools.reply_feedback('我帮你核一下新场次价格，稍等。'))

    def test_quantity_and_seats_conflict_requires_clarification(self):
        count = self.tools('两人')
        count.execute('update_ticket_request', {'evidence': count.text, 'intent': 'revise',
                                               'quantity': 2, 'quantity_text': '两人'})
        seats = self.tools('7排10、11、12座')
        result = seats.execute('supplement_ticket_details', {'evidence': seats.text, 'seats': seats.text})
        self.assertFalse(result['ok'])
        self.assertIn('不一致', result['reason'])
        self.assertNotIn('seats', self.f.session.image_context['awaiting_image_request'])

    def test_count_without_seats_binds_matching_image_only(self):
        from application.seat_request import reconcile
        from application.ticket_flow import FlowResult
        from integrations.liangpiao.api import RecognitionResult
        request = {'cinema': '广州万达', 'movie': '奥德赛', 'date': '2026-10-17',
                   'time': '22:30', 'ticket_count': 2}
        rec = RecognitionResult(match_level='EXACT', cinema_name='广州万达天河智慧城店',
                                movie_name='奥德赛', start_time='2026-10-17T22:30:00+08:00',
                                seats=[{'seatName': '1排1座'}])
        self.assertFalse(reconcile(self.f.conv, rec, request, self.f.quotes, FlowResult()).ok)
        rec.seats = [{'seatName': '1排1座'}, {'seatName': '1排2座'}]
        self.assertIsNone(reconcile(self.f.conv, rec, request, self.f.quotes, FlowResult()))
        self.assertEqual(rec.expected_seat_count, 2)
        self.assertEqual(rec.seats, [{'seatName': '1排1座'}, {'seatName': '1排2座'}])
        rec.start_time = '2026-10-17T20:30:00+08:00'
        blocked = reconcile(self.f.conv, rec, request, self.f.quotes, FlowResult())
        self.assertFalse(blocked.ok)


if __name__ == '__main__':
    unittest.main()
