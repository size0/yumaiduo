import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import settings
from application.buyer_assistant import BuyerAssistantTools, inquiry_quantity
from application import conversation_ai
from domain.conversation import Conversation
from domain.quote import QuoteRecord, QuoteStore
from domain.order import OrderStore
from tests.test_buyer_assistant import ScriptedModel, tool

class InquiryTests(unittest.TestCase):
    def setUp(self):
        self.ident=dict(tenant_id="t",shop_id="s",buyer_id="b",chat_id="c")
        self.quotes=QuoteStore();self.orders=OrderStore()
        self.addCleanup(self.quotes.close);self.addCleanup(self.orders.close)
        self.q=QuoteRecord(quote_id="new",provider="WANDA",cinema="最新影院",movie="最新影片",show="2099-10-16 20:15",unit_sell_price_fen=4690,metadata={"price_mode":"FIXED"},**self.ident)
        self.quotes.create(self.q)
        self.session=SimpleNamespace(conversation=Conversation(channel="wanda",**self.ident),image_context=dict(self.ident,created_at=(datetime.now(timezone.utc)-timedelta(seconds=3)).isoformat(),url="test"))
        self.svc=SimpleNamespace(quote_store=self.quotes,order_store=self.orders,flow=Mock())
        self.tools=BuyerAssistantTools(self.svc,self.session,"六排四张多少钱",settings.Settings(),None)
    def check_read_only(self):
        out=self.tools.flow_result
        self.assertIn("187.60",out.messages[0]);self.assertIn("六排",out.messages[0])
        self.assertIn("尚未确认座位",out.messages[0]);self.assertEqual(out.pending_commands,[])
        self.assertNotIn("QUOTE_CARD",out.template_keys)
        self.assertEqual(len(self.quotes.by_identity(self.ident)),1)
        self.svc.flow.on_screenshot.assert_not_called()
    def test_inquiry_full_sentence(self):
        self.assertTrue(self.tools.quote_ticket_inquiry(self.tools.text)["ok"]);self.check_read_only()
    def test_wrong_supplement_tool_still_read_only(self):
        self.tools.supplement_ticket_count(self.tools.text);self.check_read_only()
    def test_wrong_reprice_tool_still_read_only(self):
        self.tools.request_latest_quote_reprice();self.check_read_only()
    def test_old_order_guard_does_not_replace_inquiry(self):
        self.assertIn("187.60",self.tools.guard_reply("已记录旧订单135.60元"));self.check_read_only()
    def test_ambiguous_negated_quantity(self):
        for text in ["不要四张多少钱","四张还是三张多少钱","十四张多少钱"]:
            self.assertIsNone(inquiry_quantity(text))
        self.assertEqual(inquiry_quantity("六排四张多少钱"),4)
    def test_newer_image_blocks_previous_quote(self):
        self.session.image_context["created_at"]=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        r=self.tools.quote_ticket_inquiry(self.tools.text)
        self.assertFalse(r["ok"]);self.assertNotIn("187.60",r["guidance"])
    def test_superseded_quote_not_used(self):
        self.q.status="SUPERSEDED";self.quotes.put(self.q)
        self.assertFalse(self.tools.quote_ticket_inquiry(self.tools.text)["ok"])
    def test_agent_calls_inquiry_and_finishes(self):
        ai=conversation_ai.ConversationAI(ScriptedModel([tool("quote_ticket_inquiry",evidence=self.tools.text)]))
        result=ai.answer(self.tools.text,cfg=self.tools.cfg,tools=self.tools,conv=self.tools.conv)
        self.assertEqual(result["source"],"workflow");self.check_read_only()

    def test_changed_show_requires_clarification(self):
        self.tools.text="明天同一场四张多少钱"
        r=self.tools.quote_ticket_inquiry(self.tools.text)
        self.assertFalse(r["ok"]);self.assertNotIn("187.60",r["guidance"])
    def test_liangpiao_guard_not_hijacked(self):
        self.tools.conv.channel="liangpiao"
        self.tools.quote_ticket_inquiry=Mock(side_effect=AssertionError("Wanda tool called"))
        self.tools.guard_reply("请核对良票报价")
        self.tools.quote_ticket_inquiry.assert_not_called()

    def test_liangpiao_direct_tool_does_not_end_loop(self):
        self.tools.conv.channel="liangpiao"
        self.assertFalse(self.tools.quote_ticket_inquiry(self.tools.text)["ok"])
        self.assertIsNone(self.tools.flow_result)
