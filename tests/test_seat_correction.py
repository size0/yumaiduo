import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from application.buyer_assistant import BuyerAssistantTools
from application.conversation_ai import context_from, ConversationAI
from application.ticket_details import parse_seats
from application.seat_request import reconcile
from application.ticket_flow import FlowResult
from domain.quote import QuoteRecord
from integrations.liangpiao.api import RecognitionResult
from integrations.model.client import ModelReply
from tests import test_ticket_details as fixtures
from tests import test_buyer_assistant as ai_fixtures


class SeatCorrectionTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.TicketDetailsTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.session.image_context.update(verified=True)
        f.session.image_context['recognition_facts'].update(
            match_level='EXACT', cinema_hit_nums=413, movie_name='神探之痕',
            start_time=f.show['startTime'], seats=[{'seatNo': '10排13座'}])
        return f

    def test_wanda_strings_and_liangpiao_dicts_both_render(self):
        for seats in (['10排13座'], [{'seatNo': '10排13座'}], [{'seatName': '10排13座'}]):
            self.assertIn('10排13座', context_from(SimpleNamespace(seats=seats)))

    def test_actual_string_seats_reach_model_not_exception_handoff(self):
        f = ai_fixtures.BuyerAssistantTests(); f.setUp(); self.addCleanup(f.tearDown)
        f.conv.channel = 'wanda'; f.conv.seats = ['10排13座']
        f.svc.ai = ConversationAI(ai_fixtures.ScriptedModel([
            ai_fixtures.tool('record_buyer_intent', intent='other', evidence='你好'),
            ModelReply(content='你好，在的。')]))
        out = f.svc._answer_with_ai(f.session, '你好', f.cfg)
        self.assertEqual(out.messages, ['你好，在的。'])
        self.assertEqual(f.session.last_ai_trace['source'], 'model')

    def test_correction_inherits_exact_show_and_rechecks_target(self):
        f = self.fixture()
        f.quotes.create(QuoteRecord(quote_id='old', **f.identity))
        t = BuyerAssistantTools(f.service, f.session, '8排13，我现在选不中', f.cfg, None)
        t.enable_semantic_routing(); t.record_buyer_intent('revise', t.text)
        r = t.execute('supplement_ticket_details', {'evidence': t.text, 'seats': '8排13'})
        self.assertTrue(r['ok'])
        self.assertEqual(f.result.seats[0]['seatNo'], '8排13座')
        self.assertEqual(f.result.start_time, f.show['startTime'])
        self.assertEqual(f.quotes.get('old').status, 'SUPERSEDED')
        self.assertTrue(f.result.raw_results['buyer_text_details'])
        self.assertEqual(t.flow_result.pending_commands, [])

    def test_failed_lookup_keeps_old_quote_unusable(self):
        f = self.fixture(); f.api.ch4_show_list.side_effect = TimeoutError()
        f.quotes.create(QuoteRecord(quote_id='old', **f.identity))
        t = BuyerAssistantTools(f.service, f.session, '改成八排12座、13座', f.cfg, None)
        t.enable_semantic_routing(); t.record_buyer_intent('revise', t.text)
        r = t.execute('supplement_ticket_details', {'evidence': t.text, 'seats': '八排12座、13座'})
        self.assertFalse(r['ok']); self.assertEqual(f.quotes.get('old').status, 'SUPERSEDED')
        self.assertEqual(len(f.session.image_context['ticket_details']['seats']), 2)
        self.assertIn('排片或座位暂未核实', t.guard_reply('还未核实，请先不要付款。'))
        f.flow._after_recognition.assert_not_called()

    def test_negative_correction_invalidates_without_guessing(self):
        f = self.fixture(); f.quotes.create(QuoteRecord(quote_id='old', **f.identity))
        t = BuyerAssistantTools(f.service, f.session, '不是10排', f.cfg, None)
        t.enable_semantic_routing(); t.record_buyer_intent('revise', t.text)
        t.execute('clarify_ticket_request', {'evidence': t.text,'invalidated_fields':['seats']})
        self.assertEqual(f.quotes.get('old').status, 'SUPERSEDED')
        f.flow._after_recognition.assert_not_called()
        t2 = BuyerAssistantTools(f.service, f.session, '8排13', f.cfg, None)
        t2.enable_semantic_routing(); t2.record_buyer_intent('revise', t2.text)
        self.assertTrue(t2.execute('supplement_ticket_details', {'evidence': t2.text, 'seats': '8排13'})['ok'])
        self.assertEqual(f.result.seats[0]['seatNo'], '8排13座')

    def test_unverified_image_does_not_inherit_old_show(self):
        f = self.fixture(); f.session.image_context['verified'] = False
        _, r = f.send('8排13', seats='8排13')
        self.assertIn('影片名', r['reason']); f.api.ch4_show_list.assert_not_called()

    def test_parse_literal_chinese_row_not_intent(self):
        self.assertEqual([s['seatNo'] for s in parse_seats('八排12座、13座')], ['8排12座', '8排13座'])
        for value in ('不要八排13', '八排12或13', '八排12、12', '0排13'):
            with self.assertRaises(ValueError): parse_seats(value)

    def test_preimage_text_is_saved_then_bound_before_quote(self):
        f = self.fixture(); f.session.image_context = {}
        f.conv.channel = None
        text = '神探之痕 2026-10-16 21:15 八排12座、13座'
        t = BuyerAssistantTools(f.service, f.session, text, f.cfg, None)
        t.enable_semantic_routing(); t.record_buyer_intent('revise', text)
        t.execute('supplement_ticket_details', dict(evidence=text, movie='神探之痕',
                  date='2026-10-16', time='21:15', seats='八排12座、13座'))
        request = f.session.image_context['awaiting_image_request']
        rec = RecognitionResult(match_level='EXACT', cinema_name='包头万达影城', movie_name='神探之痕',
                                start_time=f.show['startTime'], cinema_truncated=False,
                                seats=[{'seatNo': '10排13座'}])
        self.assertIsNone(reconcile(f.conv, rec, request, f.quotes, FlowResult()))
        self.assertEqual([s['seatNo'] for s in rec.seats], ['8排12座', '8排13座'])
        self.assertTrue(rec.raw_results['buyer_text_details'])
        wrong = deepcopy(rec); wrong.movie_name = '其他影片'
        out = reconcile(f.conv, wrong, request, f.quotes, FlowResult())
        self.assertFalse(out.ok); self.assertEqual(out.pending_commands, [])


if __name__ == '__main__':
    unittest.main()
