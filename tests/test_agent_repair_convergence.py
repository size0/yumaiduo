"""Failures must converge without inventing success or starving business tools."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from application import assistant_reply, conversation_ai, semantic_request
from integrations.model.client import ModelReply
from tests import test_buyer_assistant as fixtures


class RepairConvergenceTests(unittest.TestCase):
    def fixture(self, intent='reprice'):
        f = fixtures.BuyerAssistantTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        t = f.tools('改一下价吧')
        t.enable_semantic_routing()
        t.semantic_intent = intent
        t.reply_metadata = {'intent': intent, 'kind': 'clarification', 'claims': []}
        return t

    def test_failed_platform_query_is_not_reported_as_unattempted(self):
        t = self.fixture()
        t.tool_results = [('read_platform_order', {'ok': False, 'status': 'HISTORICAL_EVIDENCE_UNAVAILABLE'})]
        decision = t.review_reply('暂时未核实订单，请提供订单卡片核对。')
        self.assertNotIn('尚未查询', decision.feedback)
        self.assertNotIn('没理解清楚', decision.text)
        self.assertNotIn('HISTORICAL_', decision.text)
        self.assertTrue(decision.fallback or not decision.feedback)
        self.assertIsNone(t.flow_result)

    def test_unattempted_platform_query_still_requires_tool(self):
        t = self.fixture()
        self.assertIn('read_platform_order', t.finalization_feedback('我来改价。'))

    def test_failed_reprice_request_cannot_restart_query_or_claim_success(self):
        t = self.fixture()
        t.platform_facts = {'ok': True, 'action': 'REQUEST_REPRICE'}
        t.tool_results = [('read_platform_order', t.platform_facts),
                          ('request_latest_quote_reprice', {'ok': False, 'status': 'STALE_REFERENCE'})]
        decision = t.review_reply('已改好了，现在付款吧。')
        self.assertNotEqual(decision.reason, 'ACCEPTED')
        self.assertNotIn('请使用查询返回', decision.feedback)
        self.assertIn('未', decision.text)

    def test_failed_revision_and_inquiry_project_missing_information(self):
        for intent, name in [('revise', 'supplement_ticket_details'), ('inquire', 'update_ticket_request')]:
            t = self.fixture(intent)
            t.tool_results = [(name, {'ok': False, 'status': 'BLOCKED'})]
            decision = t.review_reply('请补充具体场次。')
            self.assertNotIn('尚未执行', decision.feedback)
            self.assertNotIn('没理解清楚', decision.text)
            self.assertIsNone(t.flow_result)

    def test_recovered_tool_failure_does_not_mask_later_success(self):
        t = self.fixture('inquire')
        t.tool_results = [('update_ticket_request', {'ok': False}), ('update_ticket_request', {'ok': True})]
        self.assertEqual(assistant_reply.failure_reply(t), '')

    def test_prompt_and_repair_feedback_include_complete_schema(self):
        self.assertIn('最终回复必须返回JSON对象', semantic_request.INSTRUCTIONS)
        t = self.fixture()
        t.semantic_intent = None
        t.reply_metadata = {}
        feedback = t.finalization_feedback('收到')
        for key in ('"answer"', '"intent"', '"kind"', '"claims"'):
            self.assertIn(key, feedback)
        t.reply_metadata = {'invalid_response_metadata': True}
        self.assertIn('"answer"', t.finalization_feedback('收到'))

    def test_two_repairs_leave_ten_tool_rounds_available(self):
        checks = iter([assistant_reply.ReplyAssessment('bad', 'use schema', reason='NEEDS_REPAIR'),
                       assistant_reply.ReplyAssessment('bad', 'use schema', reason='NEEDS_REPAIR'),
                       assistant_reply.ReplyAssessment('完成说明')])
        t = SimpleNamespace(semantic_mode=False, schemas=[], flow_result=None,
                            review_reply=lambda _: next(checks), request_failure_reply=lambda: '',
                            execute=Mock(return_value={'ok': True}), unfinished_reply=lambda: '未完成')
        model = fixtures.ScriptedModel([ModelReply(content='bad'), ModelReply(content='bad')]
                              + [fixtures.tool('read_buyer_context', step=i) for i in range(10)]
                              + [ModelReply(content='完成说明')])
        reply, rounds, used = conversation_ai.ConversationAI(model)._chat_with_tools([], '', t)
        self.assertEqual(reply.content, '完成说明')
        self.assertEqual(t.execute.call_count, 10)
        self.assertEqual(rounds, 13)
        self.assertEqual(len(used), 10)

    def test_repair_limit_is_independent_and_does_not_execute_mutations(self):
        t = SimpleNamespace(semantic_mode=False, schemas=[], flow_result=None,
                            review_reply=lambda _: assistant_reply.ReplyAssessment('未完成', 'repair', reason='NEEDS_REPAIR'),
                            request_failure_reply=lambda: '', execute=Mock(), unfinished_reply=lambda: '请补充场次。')
        model = fixtures.ScriptedModel([ModelReply(content='bad') for _ in range(3)])
        reply, rounds, _ = conversation_ai.ConversationAI(model)._chat_with_tools([], '', t)
        self.assertEqual(rounds, 3)
        self.assertEqual(reply.content, '请补充场次。')
        t.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
