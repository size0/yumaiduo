"""Contracts for one context, tool observation and reply policy."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from application import assistant_context, conversation_ai
from application.tool_observation import normalize
from domain.quote import QuoteRecord
from integrations.model.client import ModelReply
from tests import test_buyer_assistant as fixtures


class AssistantArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BuyerAssistantTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.conv.channel = "wanda"
        self.f.session.image_context = {}

    def tools(self, text="付款后怎么取票"):
        tools = self.f.tools(text)
        tools.enable_semantic_routing()
        return tools

    def test_snapshot_never_builds_payment_advice(self):
        self.f.add_order(payment_state="CONFIRMED", state="PAID")
        tools = self.tools()
        tools.get_purchase_guidance = Mock(side_effect=AssertionError("unexpected payment advice"))
        snapshot = assistant_context.build(tools)
        self.assertEqual(snapshot["orders"]["live_platform_check"], "NOT_QUERIED")
        self.assertEqual(snapshot["orders"]["orders"][0]["payment_state"], "CONFIRMED")
        tools.get_purchase_guidance.assert_not_called()

    def test_exact_current_image_seats_supply_quantity_without_another_question(self):
        self.f.session.image_context.update(**self.f.ident,demand_id='',demand_rev=0,
            recognition_facts=dict(match_level='EXACT',seat_matched=True,
                cinema_name='哈东万达',movie_name='生化危机：爆发夜',start_time='2026-10-18T15:50:00+08:00',
                seats=[{'seatNo':'7排19座'}]))
        snapshot=assistant_context.build(self.tools('7排19'))
        self.assertEqual(snapshot['demand']['fields']['ticket_count']['value'],1)
        self.assertNotIn('ticket_count',snapshot['demand']['missing_fields'])

    def test_old_version_or_unverified_image_does_not_supply_quantity(self):
        self.f.conv.demand_id='new';self.f.conv.demand_rev=2
        self.f.session.image_context.update(**self.f.ident,demand_id='old',demand_rev=1,
            recognition_facts=dict(match_level='EXACT',seat_matched=True,seats=[{'seatNo':'7排19座'}]))
        self.assertIn('ticket_count',assistant_context.build(self.tools())['demand']['missing_fields'])

    def test_old_text_seats_do_not_borrow_current_image_verification(self):
        self.f.conv.demand_id='new';self.f.conv.demand_rev=2
        self.f.session.image_context.update(**self.f.ident,demand_id='new',demand_rev=2,
            recognition_facts=dict(match_level='EXACT',seat_matched=True,seats=[{'seatNo':'7排18座'},{'seatNo':'7排19座'}]),
            ticket_details=dict(demand_id='old',demand_rev=1,seats=[{'seatNo':'1排1座'}]))
        fields=assistant_context.build(self.tools())['demand']['fields']
        self.assertEqual(fields['seats']['value'],['7排18座','7排19座'])
        self.assertEqual(fields['ticket_count']['value'],2)
        self.f.session.image_context.update(demand_id='new',demand_rev=2,unverified_image='new-image')
        self.assertIn('ticket_count',assistant_context.build(self.tools())['demand']['missing_fields'])

    def test_quote_from_another_demand_is_not_current(self):
        self.f.conv.demand_id, self.f.conv.demand_rev = "new-demand", 2
        self.f.quotes.create(QuoteRecord(quote_id="old-q", demand_id="old-demand", demand_rev=1,
                                      provider="WANDA", request_type="WPLUS_AREA", ticket_count=2,
                                      total_sell_price_fen=7600, metadata={"quote_kind": "ORDER_TOTAL"},
                                      **self.f.ident))
        snapshot = assistant_context.build(self.tools())
        self.assertIsNone(snapshot["current_quote"])
        self.assertEqual(snapshot["other_saved_quote"]["quote_id"], "old-q")

    def test_snapshot_does_not_leak_cost_or_unverified_old_image(self):
        self.f.session.image_context.update(
            recognition_facts={"cinema_name": "旧影院", "cost_fen": 3800, "token": "secret"},
            unverified_image="new-image")
        snapshot = assistant_context.build(self.tools())
        self.assertNotIn("cinema_name", snapshot["demand"]["fields"])
        self.assertEqual(snapshot["previous_image"]["cinema_name"], "旧影院")
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("cost_fen", json.dumps(snapshot))

    def test_normal_pickup_explanation_is_not_replaced_by_payment_advice(self):
        self.f.add_order(payment_state="CONFIRMED", state="PAID")
        text = "付款后收到二维码，到影院取票机扫码取票即可。"
        tools = self.tools()
        tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
        tools.get_purchase_guidance = Mock(side_effect=AssertionError("unexpected transaction projection"))
        self.assertEqual(tools.guard_reply(text), text)
        tools.get_purchase_guidance.assert_not_called()

    def test_normal_reply_needs_no_intent_registration(self):
        tools = self.tools("谢谢，知道了")
        tools.reply_metadata = {"intent": "other", "kind": "acknowledgement", "claims": []}
        self.assertFalse(tools.finalization_feedback("不客气～"))
        model = fixtures.ScriptedModel([ModelReply(content=json.dumps({"answer": "不客气～", "intent": "other", "kind": "acknowledgement", "claims": []}, ensure_ascii=False))])
        reply, rounds, used = conversation_ai.ConversationAI(model)._chat_with_tools([], "test", tools)
        self.assertEqual(reply.content, "不客气～")
        self.assertEqual(rounds, 1)
        self.assertEqual(used, [])

    def test_reply_metrics_count_repair_then_recovery_once(self):
        from application.reply_metrics import snapshot
        tools = self.tools("谢谢")
        model = fixtures.ScriptedModel([
            ModelReply(content='{"answer":"收到"}'),
            ModelReply(content=json.dumps({"answer": "不客气", "intent": "other",
                                          "kind": "acknowledgement", "claims": []}))])
        reply, _, _ = conversation_ai.ConversationAI(model)._chat_with_tools([], "test", tools)
        tools.review_reply(reply.content)  # Final send boundary must not count twice.
        metrics = snapshot(tools)
        self.assertEqual(metrics["candidate_count"], 2)
        self.assertEqual(metrics["needs_repair_count"], 1)
        self.assertEqual(metrics["invalid_metadata_count"], 1)
        self.assertEqual(metrics["missing_metadata_count"], 0)
        self.assertEqual(metrics["final_fallback_reason"], "")

    def test_reply_metrics_plain_text_failure_and_exhaustion(self):
        from application.reply_metrics import snapshot
        tools = self.tools("谢谢")
        model = fixtures.ScriptedModel([ModelReply(content="收到") for _ in range(conversation_ai.MAX_REPAIR_ROUNDS + 1)])
        conversation_ai.ConversationAI(model)._chat_with_tools([], "test", tools)
        metrics = snapshot(tools)
        self.assertEqual(metrics["candidate_count"], conversation_ai.MAX_REPAIR_ROUNDS + 1)
        self.assertEqual(metrics["missing_metadata_count"], conversation_ai.MAX_REPAIR_ROUNDS + 1)
        self.assertEqual(metrics["invalid_metadata_count"], 0)
        self.assertEqual(metrics["final_fallback_reason"], "REPAIR_EXHAUSTED")

    def test_metrics_aggregation_distinguishes_missing_observation_and_zero(self):
        from application.reply_metrics import snapshot, aggregate
        tools = self.tools()
        report = aggregate([{}, {"reply_metrics": snapshot(tools)}])
        self.assertEqual(report["unobserved_turns"], 1)
        self.assertEqual(report["observed_turns"], 1)
        self.assertIsNone(report["rates"]["needs_repair_candidate_rate"])
        self.assertEqual(report["rates"]["needs_repair_turn_rate"], 0)
        report = aggregate([{"reply_metrics": {**snapshot(tools), "candidate_count": 2,
                             "needs_repair_count": 1, "invalid_metadata_count": 1,
                             "final_fallback_reason": "REPAIR_EXHAUSTED"}}])
        self.assertEqual(report["rates"]["needs_repair_candidate_rate"], 0.5)
        self.assertEqual(report["rates"]["invalid_metadata_turn_rate"], 1)
        self.assertEqual(report["rates"]["final_fallback_turn_rate"], 1)

    def test_metric_write_failure_does_not_break_reply(self):
        from unittest.mock import patch
        self.f.session.last_ai_trace = {"reply_metrics": {"candidate_count": 1}}
        self.f.svc.persist = True
        try:
            for error in (OSError("disk unavailable"), TypeError("not JSON serializable"), ValueError("invalid data")):
                with self.subTest(error=type(error).__name__):
                    with patch("application.store.append_reply_metrics", side_effect=error):
                        self.f.svc._record_reply_metrics(self.f.session, "wanda")
                    self.assertEqual(self.f.session.last_ai_trace["reply_metrics_write_error"], type(error).__name__)
        finally:
            self.f.svc.persist = False

    def test_metrics_daily_log_appends_without_buyer_content(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from application import store
        with tempfile.TemporaryDirectory() as temp, patch.object(store, "DATA", Path(temp)):
            row = {"channel": "wanda", "reply_metrics": {"version": "reply-metrics-v1", "candidate_count": 1}}
            store.append_reply_metrics(row)
            store.append_reply_metrics(row)
            files = list((Path(temp) / "reply-metrics").glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(set(item) == {"observed_at", "channel", "reply_metrics"} for item in rows))

    def test_service_does_not_query_purchase_guidance_for_normal_question(self):
        model = fixtures.ScriptedModel([ModelReply(content=json.dumps({"answer": "收到二维码后，在影院取票机扫码即可。", "intent": "other", "kind": "explanation", "claims": []}, ensure_ascii=False))])
        self.f.svc.ai = conversation_ai.ConversationAI(model)
        from application.buyer_assistant import BuyerAssistantTools
        from unittest.mock import patch
        with patch.object(BuyerAssistantTools, "get_purchase_guidance", side_effect=AssertionError("payment path")):
            result = self.f.svc.on_message("c", "付款后怎么领取", config=self.f.cfg)
        self.assertIn("影院取票机", "".join(result.messages))
        self.assertNotIn("请先不要付款", "".join(result.messages))
        self.assertIn("buyer-context-v1", model.seen[0][1]["system"])
        self.assertNotIn("当前订单核验", model.seen[0][1]["system"])
        self.assertEqual(self.f.session.last_ai_trace["reply_metrics"]["candidate_count"], 1)

    def test_tool_results_include_stable_observation_fields(self):
        result = self.tools().execute("read_order", {})
        for key in ("outcome", "status", "summary", "facts", "missing_fields", "next_actions",
                    "quote_kind", "tradable", "operation_id"):
            self.assertIn(key, result)
        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertIsNone(result["operation_id"])

    def test_unknown_is_not_completed_or_automatically_retryable(self):
        result = normalize("read_platform_order", {"ok": False, "status": "QUERY_UNKNOWN"}, self.tools())
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertIsNone(result["operation_id"])
        self.assertTrue(result["next_actions"])

    def test_requested_without_a_task_is_not_real_progress(self):
        result = normalize("request_latest_quote_reprice", {"ok": True, "status": "REQUESTED"}, self.tools())
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertIsNone(result["operation_id"])

    def test_normalized_unit_price_is_not_tradable(self):
        quote = {"quote_kind": "UNIT_PRICE", "tradable": False}
        result = normalize("read_quote", {"ok": True, "quote": quote}, self.tools())
        self.assertEqual(result["quote_kind"], "UNIT_PRICE")
        self.assertFalse(result["tradable"])

    def test_reprice_without_guarded_intent_is_rejected(self):
        tools = self.tools("三张")
        result = tools.execute("request_latest_quote_reprice", {"order_no": "o", "quote_id": "q", "demand_id": "d", "demand_rev": 1})
        self.assertFalse(result["ok"])
        self.assertIsNone(tools.flow_result)
        self.f.svc.flow.on_screenshot.assert_not_called()

    def test_empty_claims_cannot_hide_transaction_statements(self):
        for text in ("本单价格￥999", "改好了，拍下吧", "已下单成功", "付钱吧", "价格已更新但不要付款"):
            with self.subTest(text=text):
                tools = self.tools()
                tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
                tools.get_purchase_guidance = Mock(return_value={"ok": True, "payment_blocked": True,
                                                               "guidance": "状态未核实，请先不要付款。"})
                decision = tools.review_reply(text)
                self.assertNotEqual(decision.reason, "ACCEPTED")
                self.assertNotEqual(decision.text, text)
                self.assertIsNone(tools.flow_result)

    def test_negative_transaction_explanation_is_preserved(self):
        tools = self.tools()
        tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
        tools.get_purchase_guidance = Mock(side_effect=AssertionError("unexpected projection"))
        text = "万达不支持自动出票。尚未改价成功前不要付款。"
        self.assertEqual(tools.guard_reply(text), text)
        tools.get_purchase_guidance.assert_not_called()

    def test_unrelated_pending_command_does_not_change_failed_observation(self):
        from types import SimpleNamespace
        tools = self.tools()
        tools.flow_result = SimpleNamespace(pending_commands=[SimpleNamespace(command_id="old")])
        result = normalize("read_order", {"ok": False, "status": "BLOCKED"}, tools)
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertIsNone(result["operation_id"])

    def test_new_request_is_not_completed(self):
        result = normalize("request_latest_quote_reprice", {"ok": True, "status": "REQUESTED"},
                           self.tools(), operation_ids=("new-command",))
        self.assertEqual(result["outcome"], "REQUESTED")
        self.assertEqual(result["operation_id"], "new-command")
        self.assertEqual(result["operation_state"], "CREATED_NOT_EXECUTED")

    def test_failed_request_with_actual_command_is_unknown_and_keeps_recovery_reference(self):
        result = normalize("request_latest_quote_reprice", {"ok": False}, self.tools(),
                           operation_ids=("new-command",))
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["operation_id"], "new-command")

    def test_new_inquiry_does_not_inherit_old_image_fields(self):
        self.f.session.image_context.update(recognition_facts={"movie_name": "旧影片"},
            created_at="2026-10-16T10:00:00+08:00", inquiry_started_at="2026-10-16T11:00:00+08:00")
        snapshot = assistant_context.build(self.tools())
        self.assertNotIn("movie_name", snapshot["demand"]["fields"])
        self.assertIn("movie", snapshot["demand"]["missing_fields"])

    def test_context_drops_cross_identity_fields_and_nested_credentials(self):
        self.f.session.image_context.update(shop_id="another-shop", ticket_details={"movie": "别店影片"},
            recognition_facts={"seats": [{"seatNo": "1排1座", "token": "secret"}]})
        snapshot = assistant_context.build(self.tools())
        self.assertFalse(snapshot["demand"]["saved_context_identity_matches"])
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("别店影片", json.dumps(snapshot))

    def test_structured_claim_must_reference_current_quote(self):
        tools = self.tools()
        tools.reply_metadata = {"intent": "inquire", "kind": "result",
                                "claims": [{"kind": "quote", "quote_id": "fake", "demand_id": "fake", "demand_rev": 1}]}
        self.assertTrue(tools.finalization_feedback())

    def test_fenced_response_is_unpacked_and_never_grants_write_permission(self):
        from application.assistant_reply import unpack
        answer, metadata = unpack('```json\n' + json.dumps({"answer": "收到", "intent": "reprice",
                                                    "kind": "result", "claims": []}) + '\n```')
        self.assertEqual(answer, "收到")
        tools = self.tools()
        tools.reply_metadata = metadata
        self.assertIsNone(getattr(tools, "semantic_intent", None))
        self.assertFalse(tools.request_latest_quote_reprice(order_no="o", quote_id="q", demand_id="d", demand_rev=1)["ok"])

    def test_invalid_structured_reply_requires_repair(self):
        from application.assistant_reply import unpack
        answer, metadata = unpack('{"answer":"收到"}')
        self.assertEqual(answer, "收到")
        tools = self.tools()
        tools.reply_metadata = metadata
        self.assertTrue(tools.finalization_feedback())

    def test_historical_success_cannot_prove_new_demand_success(self):
        self.f.add_order(demand_id="old-demand", demand_rev=1, state="TICKETED",
                         payment_state="CONFIRMED", payload={"wanda_ticket_image": {"imageUrl": "qr"}})
        self.f.conv.demand_id, self.f.conv.demand_rev = "new-demand", 1
        for text in ("已下单成功", "已经付款", "出票成功"):
            with self.subTest(text=text):
                tools = self.tools()
                assistant_context.build(tools)
                tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
                tools.get_purchase_guidance = Mock(return_value={"ok": True, "payment_blocked": True,
                                                               "guidance": "新需求尚未完成核验。"})
                self.assertNotEqual(tools.review_reply(text).reason, "ACCEPTED")

    def test_success_with_same_demand_wrong_revision_is_not_current(self):
        self.f.add_order(demand_id="d", demand_rev=1, payment_state="CONFIRMED")
        self.f.conv.demand_id, self.f.conv.demand_rev = "d", 2
        tools = self.tools()
        assistant_context.build(tools)
        tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
        self.assertNotEqual(tools.guard_reply("已经付款"), "已经付款")

    def test_current_bound_payment_fact_can_be_explained(self):
        self.f.add_order(demand_id="d", demand_rev=2, payment_state="CONFIRMED")
        self.f.conv.demand_id, self.f.conv.demand_rev = "d", 2
        tools = self.tools()
        assistant_context.build(tools)
        tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
        self.assertEqual(tools.guard_reply("这笔订单已经付款，无需再次支付。"), "这笔订单已经付款，无需再次支付。")

    def test_historical_order_claim_requires_order_number_in_answer(self):
        self.f.add_order(demand_id="old", demand_rev=1, payment_state="CONFIRMED")
        self.f.conv.demand_id = "new"
        tools = self.tools()
        assistant_context.build(tools)
        tools.reply_metadata = {"intent": "other", "kind": "result",
                                "claims": [{"kind": "order", "order_no": "platform"}]}
        self.assertTrue(tools.finalization_feedback("已经付款"))
        self.assertFalse(tools.finalization_feedback("订单 platform 已经付款。"))

    def test_saved_correction_overrides_image_field_in_one_canonical_value(self):
        self.f.session.image_context.update(recognition_facts={"movie_name": "旧影片", "start_time": "2099-10-16T20:15:00+08:00"},
            ticket_details={"movie": "新影片", "date": "2099-10-17", "pending": True})
        snapshot = assistant_context.build(self.tools())
        fields = snapshot["demand"]["fields"]
        self.assertEqual(fields["movie"]["value"], "新影片")
        self.assertEqual(fields["date"]["value"], "2099-10-17")
        self.assertEqual(fields["time"]["value"], "20:15")
        self.assertNotIn("movie_name", fields)
        self.assertTrue(snapshot["demand"]["pending_requote"])

    def test_latest_text_quantity_overrides_saved_image_quantity(self):
        self.f.session.image_context.update(ticket_count=2, ticket_details={"ticket_count": 4})
        self.assertEqual(assistant_context.build(self.tools())["demand"]["fields"]["ticket_count"]["value"], 4)

    def test_unverified_image_keeps_only_new_grounded_quantity(self):
        self.f.conv.demand_id, self.f.conv.demand_rev = "d", 1
        self.f.session.image_context.update(self.f.ident, url="old-image", unverified_image="new-image", ticket_count=3)
        tools = self.tools("两张")
        tools.execute("record_buyer_intent", {"intent": "revise"})
        tools.execute("update_ticket_request", {"intent": "revise", "quantity": 2, "quantity_text": "两"})
        snapshot = assistant_context.build(tools)
        self.assertEqual(snapshot["demand"]["fields"]["ticket_count"]["value"], 2)
        self.f.session.image_context["unverified_image"] = "yet-another-image"
        self.assertNotIn("ticket_count", assistant_context.build(tools)["demand"]["fields"])

    def test_current_message_and_sources_are_separate(self):
        self.f.session.history = [{"role": "user", "content": "明天两张", "created_at": datetime.now(timezone.utc).isoformat()}]
        snapshot = assistant_context.build(self.tools("今天"))
        self.assertEqual(snapshot["current_message"], "今天")
        self.assertEqual(snapshot["buyer_message_sources"][0]["text"], "明天两张")

    def test_amount_validation_reads_quote_updated_after_snapshot(self):
        from application.assistant_reply import _unsupported_amount
        tools = self.tools()
        tools.conv.demand_id, tools.conv.demand_rev = "new", 2
        tools.context_snapshot = {"current_quote": None}
        tools.read_quote = Mock(return_value={"ok": True, "quote": {
            "valid": True, "demand_id": "new", "demand_rev": 2,
            "unit_yuan": "38.00", "total_yuan": "76.00"}})
        self.assertFalse(_unsupported_amount(tools, "两张合计76元。"))
        tools.conv.demand_rev = 3
        self.assertTrue(_unsupported_amount(tools, "两张合计76元。"))

    def test_conditional_process_explanation_is_not_success_claim(self):
        for text in ("付款成功后，店家会安排出票。", "如果出票成功，会发送取票二维码。",
                     "已付款后不能直接改价。"):
            with self.subTest(text=text):
                tools = self.tools()
                tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
                tools.get_purchase_guidance = Mock(side_effect=AssertionError("unrelated payment advice"))
                self.assertEqual(tools.review_reply(text).text, text)
                tools.get_purchase_guidance.assert_not_called()

    def test_conditional_clause_does_not_hide_later_false_success(self):
        tools = self.tools()
        tools.reply_metadata = {"intent": "other", "kind": "explanation", "claims": []}
        tools.get_purchase_guidance = Mock(return_value={"ok": False, "guidance": "尚未核实。"})
        self.assertNotEqual(tools.review_reply("如果出票成功会发二维码。现在已经付款。").reason, "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
