"""Regression coverage from the buyer-text audit; no production operations."""
import unittest
from copy import deepcopy

import settings
from application.buyer_assistant import BuyerAssistantTools
from application.conversation_ai import ConversationAI
from application.ticket_details import supplement
from application.semantic_request import number
from domain.conversation import ConvState
from tests import test_buyer_assistant as fixtures


class ConversationPolicyTests(unittest.TestCase):
    def fixture(self, text):
        f = fixtures.BuyerAssistantTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        f.conv.channel = 'wanda'
        f.session.image_context['channel'] = 'wanda'
        tools = f.tools(text)
        tools.enable_semantic_routing()
        return f, tools

    def test_legacy_style_does_not_restore_blanket_handoff(self):
        cfg = settings.Settings()
        cfg.assistant.reply_style = '短一点。\n不确定的事就说"我帮你转人工看下"，不要编。'
        prompt = cfg.assistant.build_prompt()
        self.assertIn('短一点', prompt)
        self.assertNotIn('上游只保证就近调座', prompt)
        self.assertNotIn('拿不准的事一律', prompt)
        self.assertNotIn('不确定的事就说"我帮你转人工看下"', prompt)
        self.assertTrue(cfg.assistant.reply_style.endswith('不要编。'))

    def test_absent_context_does_not_assert_no_order_or_image(self):
        prompt = ConversationAI(None)._system_prompt(settings.Settings(), '', ConvState.IDLE)
        self.assertIn('不代表买家没发图、没下单', prompt)
        self.assertNotIn('不知道的就说转人工', prompt)

    def test_ambiguous_seats_cannot_persist_fabricated_row(self):
        for text in ('好的，那就15 15', '就这两个', '中间那俩'):
            with self.subTest(text=text):
                f, tools = self.fixture(text)
                before = deepcopy(f.session.image_context)
                result = supplement(tools, evidence=text, seats='15排15座')
                self.assertFalse(result['ok'])
                self.assertEqual(f.session.image_context, before)
                f.svc.flow.on_screenshot.assert_not_called()

    def test_read_quote_does_not_complete_quantity_revision(self):
        f, tools = self.fixture('三张')
        tools.execute('record_buyer_intent', {'intent': 'revise', 'evidence': '三张'})
        tools.execute('read_quote', {})
        self.assertTrue(tools.finalization_feedback())
        f.svc.flow.on_screenshot.assert_not_called()

    def test_acknowledgement_variants_require_no_transactions(self):
        for text in ('好的', 'OK', '谢啦', '收到，辛苦了'):
            with self.subTest(text=text):
                f, tools = self.fixture(text)
                f.add_order(payment_state='CONFIRMED', state='PAID', shipped=True)
                before = f.orders.get('o').to_dict()
                tools.execute('record_buyer_intent', {'intent': 'other', 'evidence': text})
                self.assertFalse(tools.finalization_feedback())
                self.assertIsNone(tools.flow_result)
                self.assertEqual(f.orders.get('o').to_dict(), before)

    def test_quantity_unit_normalization_preserves_literal_evidence(self):
        for fragment, expected in [('三个人', 3), ('这三个人', 3), ('2个人', 2), ('两位', 2), ('四张', 4)]:
            with self.subTest(fragment=fragment):
                self.assertEqual(number(fragment), expected)
        for fragment in ('三个人或四个人', '15 15', '三排', '-3个人', '100个人'):
            self.assertIsNone(number(fragment))

    def test_three_people_correction_reaches_quote_once(self):
        text = '不是两张，我们三个人'
        f, tools = self.fixture(text)
        tools.execute('record_buyer_intent', {'intent': 'revise', 'evidence': text})
        result = tools.execute('update_ticket_request', {'intent': 'revise', 'quantity': 3,
                               'quantity_text': '三个人', 'evidence': text})
        self.assertTrue(result['ok'])
        self.assertEqual(f.svc.flow.on_screenshot.call_count, 1)
        self.assertEqual(f.svc.flow.on_screenshot.call_args.kwargs['ticket_count'], 3)
        self.assertFalse(tools.flow_result.pending_commands)


if __name__ == '__main__':
    unittest.main()
