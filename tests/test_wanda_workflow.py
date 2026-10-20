"""Offline regressions for Wanda area quoting and image continuation."""
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from tests._check_channel_route import flow, result, MAP_WANDA, MAP_NONE
from tests.test_buyer_assistant import BuyerAssistantTests
from application.ticket_flow import FlowResult
from application.wanda_cost import WandaCostError
from application.buyer_assistant import explicit_ticket_count
from application.quote_reprice import build_quote_reprice
from domain.conversation import Conversation, ConvState
from domain.quote import QuoteStore


class WandaWorkflowTests(unittest.TestCase):
    def setUp(self):
        # Supplier fixtures describe 2026-10-13 shows; freeze availability time.
        clock = patch("domain.quote.models._now", return_value=datetime(2026, 10, 13, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)

    def quote(self, count=None, seats=None):
        f, api, transport = flow(MAP_WANDA)
        f.quote_store = QuoteStore()
        self.addCleanup(f.quote_store.close)
        conv = Conversation(chat_id="c", tenant_id="t", shop_id="s", buyer_id="b", state=ConvState.QUOTED, channel="liangpiao")
        with patch("application.ticket_flow._second_quote_wait_seconds", return_value=0):
            out = f._build_quote(conv, result(expected_seat_count=count, seats=seats or []), FlowResult())
        return f, api, conv, out, f.quote_store.get(conv.quote_id)

    def test_unselected_unknown_quantity_is_unit_only(self):
        f, api, conv, out, q = self.quote()
        self.assertIn("WANDA_UNIT_PRICE", out.template_keys)
        self.assertTrue(any("元/张" in m for m in out.messages))
        self.assertEqual((q.ticket_count, q.total_sell_price_fen), (0, 0))
        self.assertFalse(q.is_order_ready())
        self.assertIsNone(conv.quote)
        self.assertFalse(f.on_buyer_confirm(conv).ok)
        self.assertFalse(api.preflight_calls)
        self.assertEqual((conv.channel, out.channel), ("wanda", "wanda"))

    def test_unselected_confirmed_quantity_is_formal(self):
        f, api, conv, out, q = self.quote(2)
        self.assertEqual(q.ticket_count, 2)
        self.assertEqual(q.total_sell_price_fen, conv.quote.total_price_fen)
        self.assertEqual(q.total_sell_price_fen, q.unit_sell_price_fen * 2)
        self.assertEqual(q.selected_seats, [])
        self.assertTrue(q.is_order_ready())
        self.assertIn("QUOTE_CARD", out.template_keys)
        self.assertTrue(f.on_buyer_confirm(conv).ok)
        self.assertFalse(api.create_calls)
        self.assertFalse(api.preflight_calls)

    def test_wanda_mapping_missing_never_falls_back(self):
        for bridge in (True, False):
            f, api, _ = flow(MAP_NONE, bridge=bridge)
            out = f._build_quote(Conversation(chat_id="c"), result(), FlowResult())
            self.assertFalse(out.ok)
            self.assertFalse(api.preflight_calls)
            self.assertTrue(any("无需重新选座" in m for m in out.messages))

    def test_missing_member_price_is_not_missing_selection(self):
        f, api, _ = flow(MAP_WANDA)
        conv = Conversation(chat_id="c", state=ConvState.QUOTED)
        with patch("application.ticket_flow.resolve_cost", side_effect=WandaCostError("NO_MEMBER_PRICE", "no price")):
            out = f._build_quote(conv, result(), FlowResult())
        self.assertFalse(out.ok)
        self.assertTrue(any("会员价" in m for m in out.messages))
        self.assertFalse(any("未识别到您选择" in m for m in out.messages))
        self.assertIn("万达成本查询失败：NO_MEMBER_PRICE", out.notes)
        self.assertEqual(conv.state, ConvState.IDLE)
        self.assertEqual(out.state, ConvState.IDLE)
        self.assertEqual(conv.quote_id, "")
        self.assertIn('WANDA_QUOTE_FAILED', out.template_keys)
        self.assertFalse(any('换猫眼' in m for m in out.messages))

    def test_cost_failure_invalidates_previous_demand_quote_without_changing_order(self):
        from tests.test_agent_purchase import AgentPurchaseTests
        from application import purchase_actions
        t = AgentPurchaseTests(); t.setUp(); self.addCleanup(t.doCleanups)
        f, _, _ = flow(MAP_WANDA)
        f.quote_store = t.quotes
        before = t.orders.get('o').to_dict()
        t.tools.conv.state = ConvState.QUOTED
        with patch('application.ticket_flow.resolve_cost', side_effect=WandaCostError('NO_MEMBER_PRICE', 'no price')):
            f._build_quote(t.tools.conv, result(), FlowResult())
        self.assertFalse(t.quotes.get('new').is_order_ready())
        self.assertEqual(t.orders.get('o').to_dict(), before)
        with patch.object(purchase_actions, 'platform_order', return_value=t.detail):
            facts = t.tools.read_platform_order()
            self.assertFalse(facts['quote_valid'])
            self.assertTrue(facts['payment_blocked'])
            t.tools.request_latest_quote_reprice()
        self.assertIsNone(t.tools.flow_result)

    def test_unit_quote_cannot_reprice(self):
        f, api, conv, out, q = self.quote()
        stores = SimpleNamespace(quotes=f.quote_store, orders=object())
        self.assertEqual(build_quote_reprice(stores, {k:getattr(conv,k) for k in ("tenant_id","shop_id","buyer_id","chat_id")}, q.quote_id), [])

    def test_wanda_formal_quote_has_no_time_expiry(self):
        _, _, _, _, q = self.quote(2)
        q.expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.assertTrue(q.is_order_ready())

    def test_quantity_conflict_releases_recognition_state(self):
        f, api, _ = flow(MAP_WANDA)
        conv = Conversation(chat_id="c", state=ConvState.RECOGNIZING)
        conv.buyer_ticket_count = 3
        r = result(seats=[{"seatNo":"6排9座"},{"seatNo":"6排10座"}])
        out = f._after_recognition(conv, r, FlowResult())
        self.assertFalse(out.ok)
        self.assertEqual(conv.state, ConvState.IDLE)

    def test_account_pool_uses_app_signature(self):
        from integrations.wanda.bridge import WandaBridgeClient, BridgeConfig, PATH_ACCOUNTS
        bridge = WandaBridgeClient(BridgeConfig(base="http://example", key="", app_key="ak-test", app_secret="test-secret"))
        headers, mode = bridge._auth_headers(PATH_ACCOUNTS, "{}")
        self.assertEqual(mode, "app")
        self.assertNotIn("X-Plugin-Bridge-Key", headers)

    def test_quantity_parser_does_not_guess(self):
        for phrase in ("不要两张", "不是2张", "两张还是三张", "订单123", "0张"):
            self.assertIsNone(explicit_ticket_count(phrase))
        self.assertEqual(explicit_ticket_count("要两张"), 2)


class QuantityContinuationTests(BuyerAssistantTests):
    def test_image_then_quantity_runs_workflow_without_model(self):
        from application import conversation_ai
        from tests.test_buyer_assistant import ScriptedModel
        f, api, _ = flow(MAP_WANDA)
        f.quote_store = self.quotes
        api.ch7_recognize = lambda image, city: result(expected_seat_count=None)
        self.svc.flow = f
        model = ScriptedModel([])
        self.svc.ai = conversation_ai.ConversationAI(model)
        self.cfg.assistant.agent_enabled = False  # Exercise the no-model fallback.
        with patch("application.ticket_flow._second_quote_wait_seconds", return_value=0):
            first = self.svc.on_message("c", "", image_url="https://example.com/seat.png", config=self.cfg)
            second = self.svc.on_message("c", "要两张", config=self.cfg)
        self.assertIn("WANDA_UNIT_PRICE", first.template_keys)
        self.assertIn("QUOTE_CARD", second.template_keys)
        self.assertEqual(self.quotes.get(self.conv.quote_id).ticket_count, 2)
        self.assertEqual(model.seen, [])

    def test_order_card_rejects_unit_price(self):
        from application import plugin_runtime, ledger
        from domain.quote import QuoteRecord
        self.add_order()
        self.quotes.create(QuoteRecord(quote_id="q", ticket_count=0, total_sell_price_fen=0,
            metadata={"quote_kind":"UNIT_PRICE"}, **self.ident))
        event = {"tenant_id":"t", "shop_id":"s", "buyer_id":"b", "chat_id":"c", "payload":{"orderId":"platform"}}
        with patch.object(ledger, "stores", return_value=SimpleNamespace(orders=self.orders, quotes=self.quotes)), patch.object(plugin_runtime, "build_purchase_notice_commands", return_value=[]):
            commands = plugin_runtime._from_order_card(event)
        self.assertFalse(any(c.type == "CHANGE_ORDER_PRICE" for c in commands))

    def test_count_and_city_share_image_context(self):
        self.session.image_context["city"] = "深圳"
        answer = self.tools("要两张").supplement_ticket_count("要两张")
        self.assertTrue(answer["handled"])
        self.svc.flow.on_screenshot.assert_called_once_with(self.conv, "https://example.com/seat.png", city_name="深圳", channel="liangpiao", ticket_count=2)
        self.svc.flow.on_screenshot.reset_mock()
        self.tools("广州").supplement_city("广州")
        self.assertEqual(self.svc.flow.on_screenshot.call_args.kwargs["ticket_count"], 2)

    def test_negated_quantity_tool_cannot_extract_substring(self):
        answer = self.tools("不要两张").supplement_ticket_count("两张")
        self.assertFalse(answer["ok"])
        self.svc.flow.on_screenshot.assert_not_called()

    def test_paid_order_blocks_quantity_requote(self):
        self.add_order(payment_state="PAID", state="AWAITING_PAYMENT")
        answer = self.tools("两张").supplement_ticket_count("两张")
        self.assertFalse(answer["ok"])
        self.svc.flow.on_screenshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
