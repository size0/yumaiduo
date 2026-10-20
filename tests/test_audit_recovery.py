"""Cross-case recovery contracts, with no external business IO."""
import unittest
from tests import test_buyer_assistant as fixtures


class AuditRecoveryTests(unittest.TestCase):
    def test_natural_time_formats_remain_literal_and_ambiguous_when_needed(self):
        from application.ticket_details import parse_time
        for value, expected in [('下午一点', ('13:00', False)), ('四点十分', ('04:10', True)),
                                ('16点50', ('16:50', False)), ('晚上19:10', ('19:10', False)),
                                ('下午17.40', ('17:40', False)), ('晚上七点半', ('19:30', False))]:
            with self.subTest(value=value): self.assertEqual(parse_time(value), expected)
        for value in ('上午19:10', '25点', '下午四点六十分', '四点或五点', '晚上十二点', '晚上十二点半', '上午12:00'):
            with self.subTest(value=value), self.assertRaises(ValueError): parse_time(value)
    def setUp(self):
        self.f = fixtures.BuyerAssistantTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.conv.channel = 'wanda'
        self.f.session.image_context = {}

    def tool(self, text='今天奥德赛'):
        t = self.f.tools(text)
        t.enable_semantic_routing()
        return t

    def test_current_message_bound_without_model_retyping(self):
        t = self.tool('广州\u00a0万达')
        self.assertTrue(t.execute('record_buyer_intent', {'intent':'revise'})['ok'])
        self.assertTrue(t.execute('supplement_ticket_details', {'cinema':'广州\u00a0万达'})['ok'])
        self.assertEqual(self.f.session.image_context['awaiting_image_request']['cinema'], t.text)

    def test_wrong_history_evidence_is_still_rejected(self):
        t = self.tool('今天')
        self.assertFalse(t.execute('record_buyer_intent', {'intent':'revise','evidence':'昨天两张'})['ok'])

    def test_other_can_recover_to_details_but_not_to_money(self):
        t = self.tool()
        self.assertTrue(t.execute('record_buyer_intent', {'intent':'other'})['ok'])
        self.assertTrue(t.execute('record_buyer_intent', {'intent':'revise'})['ok'])
        self.assertFalse(t.execute('record_buyer_intent', {'intent':'reprice'})['ok'])

    def test_inquiry_cannot_upgrade_to_purchase(self):
        t = self.tool('四张多少钱')
        t.execute('record_buyer_intent', {'intent':'inquire'})
        self.assertFalse(t.execute('record_buyer_intent', {'intent':'revise'})['ok'])
        self.assertFalse(t.execute('record_buyer_intent', {'intent':'reprice'})['ok'])

    def test_rejected_update_is_not_successful_completion(self):
        t = self.tool()
        t.execute('record_buyer_intent', {'intent':'revise'})
        t.execute('update_ticket_request', {'intent':'revise','quantity':2,'quantity_text':'两张'})
        self.assertTrue(t.finalization_feedback())
        self.assertNotEqual(t.guard_reply('好的，现在数量是三张。'), '好的，现在数量是三张。')

    def test_failed_call_can_retry_after_intent_correction(self):
        from application.conversation_ai import ConversationAI
        from integrations.model.client import ModelReply
        model = fixtures.ScriptedModel([
            fixtures.tool('record_buyer_intent', intent='other'),
            fixtures.tool('supplement_ticket_details', movie='奥德赛'),
            fixtures.tool('record_buyer_intent', intent='revise'),
            fixtures.tool('supplement_ticket_details', movie='奥德赛'),
            ModelReply(content='请问哪天看？')])
        t = self.tool()
        ConversationAI(model)._chat_with_tools([], '', t)
        self.assertEqual(self.f.session.image_context['awaiting_image_request']['movie'], '奥德赛')

    def test_known_shop_experience_available_without_faq_match(self):
        t = self.tool('儿童票')
        t.cfg.assistant.experience = '一般1.4米以下无需买票，具体联系影院。'
        t._faq = lambda _: None
        result = t.execute('search_faq', {'keyword':'儿童票'})
        self.assertTrue(result['ok'])
        self.assertIn('1.4米', result['shop_experience'])
        self.assertNotIn('转人工', result.get('reason',''))

    def test_failed_arguments_do_not_force_handoff(self):
        t = self.tool()
        t.execute('record_buyer_intent', {'intent':'revise'})
        t.execute('supplement_ticket_details', {'movie':'其他电影'})
        self.assertTrue(t.reply_feedback('[转人工]工具参数不对，需要人工。'))

    def test_context_tool_does_not_expose_assistant_or_cost(self):
        t = self.tool('今天')
        self.f.session.history = [{'role':'assistant','content':'成本100'}, {'role':'user','content':'两人'}]
        result = t.execute('read_buyer_context', {})
        self.assertTrue(result['ok'])
        self.assertNotIn('成本', str(result))
        self.assertIn('两人', str(result))

    def test_no_faq_match_has_actionable_next_step(self):
        from application.tools import ReadOnlyTools
        result = ReadOnlyTools(faq_source=lambda _: None).search_faq('随便')
        self.assertNotIn('转人工', result['reason'])
        self.assertTrue(result['next_actions'])

    def test_historical_cinema_requires_verified_bounded_reference(self):
        from application.ticket_details import now
        self.f.session.history = [{'role':'user','content':'广州万达天河智慧城店', 'created_at':now().isoformat()}]
        t = self.tool('2230的奥德赛 两人')
        t.execute('record_buyer_intent', {'intent':'revise'})
        context = t.execute('read_buyer_context', {})
        ref = context['buyer_messages'][0]['message_ref']
        self.assertTrue(context['buyer_messages'][0]['can_reference'])
        result = t.execute('supplement_ticket_details', {'movie':'奥德赛','time':'2230','cinema':'广州万达天河智慧城店',
                                                       'source_refs':{'cinema':ref}})
        self.assertTrue(result['ok'])
        pending = self.f.session.image_context['awaiting_image_request']
        self.assertEqual(pending['cinema'],'广州万达天河智慧城店')
        self.assertEqual(pending['field_sources']['cinema']['message_ref'],ref)
        changed = self.tool('北京万达')
        changed.execute('record_buyer_intent', {'intent':'revise'})
        changed.execute('supplement_ticket_details', {'cinema':'北京万达'})
        self.assertNotIn('cinema', self.f.session.image_context['awaiting_image_request']['field_sources'])

    def test_old_future_unknown_and_cross_demand_messages_cannot_be_referenced(self):
        from application.ticket_details import now
        from datetime import timedelta
        for at in (None,(now()-timedelta(hours=1)).isoformat(),(now()+timedelta(minutes=1)).isoformat()):
            self.f.session.history=[{'role':'user','content':'广州万达','created_at':at}]
            t=self.tool();t.execute('record_buyer_intent',{'intent':'revise'})
            row=t.execute('read_buyer_context',{})['buyer_messages'][0]
            self.assertFalse(row['can_reference'])
            self.assertFalse(t.execute('supplement_ticket_details',{'cinema':'广州万达','source_refs':{'cinema':row['message_ref']}})['ok'])
        self.f.session.history=[{'role':'user','content':'广州万达','created_at':(now()-timedelta(seconds=1)).isoformat()}]
        t=self.tool();t.execute('record_buyer_intent',{'intent':'inquire'})
        t.execute('begin_new_inquiry',{})
        self.assertFalse(t.execute('read_buyer_context',{})['buyer_messages'][0]['can_reference'])
