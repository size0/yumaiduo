from tests.test_buyer_assistant import BuyerAssistantTests, ScriptedModel, tool
from integrations.model.client import ModelReply
from application import conversation_ai, service
from unittest.mock import patch

class MemberFaqTests(BuyerAssistantTests):
    def test_agent_can_choose_faq_before_reply(self):
        self.conv.channel="wanda"
        self.cfg.reply.intelligent_qa_enabled=True
        model=ScriptedModel([tool('record_buyer_intent', intent='other', evidence='会员座位可以买吗'),
            tool('search_faq',keyword='会员座可以买吗'), ModelReply(content="可以买的，请标记需要的位置并告知张数。")])
        self.svc.ai=conversation_ai.ConversationAI(model)
        out=self.svc._answer_with_ai(self.session,"会员座位可以买吗",self.cfg)
        self.assertFalse(out.need_human)
        self.assertIn("search_faq",self.session.last_ai_trace["tools"])
        self.assertIn("可以买",str(model.seen))
    def test_handoff_only_one_reply(self):
        self.svc.ai=conversation_ai.ConversationAI(ScriptedModel([ModelReply(content="[转人工]这个我帮你转人工确认下。")]))
        out=self.svc._answer_with_ai(self.session,"帮我找店家",self.cfg)
        self.assertTrue(out.need_human);self.assertEqual(len(out.messages),1)
