import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, Mock

import settings
from application import model_config, service, conversation_ai
from application.buyer_assistant import BuyerAssistantTools
from application.ticket_flow import FlowResult
from domain.conversation import Conversation, ConvState
from domain.order import Order, OrderStore
from domain.quote import QuoteStore
from integrations.model.client import ModelReply


class ScriptedModel:
    ready = True

    def __init__(self, calls):
        self.calls, self.seen = list(calls), []
        # Scripted model fixtures declare their complete field plan up front.
        # This represents fixed model understanding; it is not inferred by app code.
        fields=set();proofs={}
        for reply in self.calls:
            for call in reply.raw.get('tool_calls') or []:
                fn=call.get('function') or {};name=fn.get('name');args=json.loads(fn.get('arguments') or '{}')
                if name=='update_ticket_request' and args.get('intent')=='revise':
                    fields.add('ticket_count');proofs['ticket_count']=args.get('quantity_text')
                if name=='supplement_city':fields.add('city');proofs['city']=args.get('city')
                if name=='supplement_ticket_details':
                    fields.update(k for k in ('movie','date','time','seats','cinema','position_preference') if args.get(k))
                    proofs.update({k:args[k] for k in fields if args.get(k)})
        for reply in self.calls:
            for call in reply.raw.get('tool_calls') or []:
                fn=call.get('function') or {};args=json.loads(fn.get('arguments') or '{}')
                if fields and fn.get('name') in ('update_ticket_request','supplement_city','supplement_ticket_details'):
                    args.setdefault('request_fields',sorted(fields));args.setdefault('field_evidence',dict(proofs));fn['arguments']=json.dumps(args)

    def chat(self, messages, **kwargs):
        self.seen.append((messages, kwargs))
        return self.calls.pop(0)


def tool(name, **args):
    return ModelReply(
        raw={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool1",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }
    )


class BuyerAssistantTests(unittest.TestCase):
    def setUp(self):
        self.ident = dict(tenant_id="t", shop_id="s", buyer_id="b", chat_id="c")
        self.orders, self.quotes = OrderStore(), QuoteStore()
        self.cfg = settings.Settings()
        self.cfg.assistant.agent_enabled = True
        self.conv = Conversation(**self.ident)
        self.session = service.Session(chat_id="c", conversation=self.conv)
        self.svc = service.TicketService.__new__(service.TicketService)
        self.svc.sessions = {"c": self.session}
        self.svc.persist = False
        self.svc.order_store, self.svc.quote_store = self.orders, self.quotes
        self.svc.flow = SimpleNamespace(
            on_screenshot=Mock(
                return_value=FlowResult(
                    ok=True, messages=["核验后的报价"], template_keys=["QUOTE_CARD"]
                )
            )
        )
        self.session.image_context = dict(
            self.ident,
            url="https://example.com/seat.png",
            created_at=datetime.now(timezone.utc).isoformat(),
            channel="liangpiao",
        )

    def tearDown(self):
        self.orders.close()
        self.quotes.close()

    def tools(self, text="深圳"):
        return BuyerAssistantTools(
            self.svc, self.session, text, self.cfg, lambda k: "FAQ参考"
        )

    def add_order(self, **changes):
        data = dict(
            order_id="o",
            quote_id="q",
            idempotency_key="k",
            provider="LIANGPIAO",
            provider_order_id="platform",
            amount_fen=1850,
            state="AWAITING_PAYMENT",
            **self.ident,
        )
        data.update(changes)
        self.orders.create(Order(**data))

    def test_agent_reads_real_order_after_memory_is_empty(self):
        self.add_order()
        result = self.tools().execute("get_purchase_guidance", {})
        self.assertIn("原订单", result["guidance"])
        self.assertNotIn("点击右上角立即购买", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("cost", json.dumps(result))

    def test_other_shop_order_is_invisible(self):
        self.add_order(shop_id="other")
        self.assertEqual(self.tools().read_order()["orders"], [])

    def test_paid_and_unconfirmed_never_instruct_new_order(self):
        self.add_order(payment_state="CONFIRMED", state="PAID")
        self.assertIn("无需再次下单", self.tools().get_purchase_guidance()["guidance"])
        o = self.orders.get("o")
        o.payment_state = "UNPAID"
        o.state = "REPRICE_PENDING"
        self.orders.put(o)
        self.assertIn("不要付款", self.tools().get_purchase_guidance()["guidance"])

    def test_completed_history_does_not_block_new_image_city(self):
        self.add_order(payment_state="CONFIRMED", state="DELIVERED")
        result = self.tools().execute("supplement_city", {"city": "深圳"})
        self.assertTrue(result["ok"])
        self.svc.flow.on_screenshot.assert_called_once()
        self.assertIn("截图", self.tools().get_purchase_guidance()["guidance"])

    def test_paid_failed_order_requires_review_not_new_purchase(self):
        self.add_order(payment_state="CONFIRMED", state="FAILED")
        result = self.tools().get_purchase_guidance()
        self.assertNotIn("template", result)
        self.assertIn("无需再次下单", result["guidance"])

    def test_city_cannot_be_applied_to_a_newer_image(self):
        tools = self.tools()
        self.session.image_context["url"] = "https://example.com/new.png"
        result = tools.execute("supplement_city", {"city": "深圳"})
        self.assertFalse(result["ok"])
        self.svc.flow.on_screenshot.assert_not_called()

    def test_quote_without_seats_cannot_invite_purchase(self):
        from domain.quote import QuoteRecord

        q = QuoteRecord(
            quote_id="q",
            ticket_count=1,
            total_sell_price_fen=1850,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            **self.ident,
        )
        self.quotes.create(q)
        self.assertTrue(self.tools().read_quote()["quote"]["valid"])
        self.assertNotIn("template", self.tools().get_purchase_guidance())
        q.selected_seats = ["4排7列"]
        self.quotes.put(q)
        self.assertIn("template", self.tools().get_purchase_guidance())

    def test_read_quote_tradable_matches_transaction_gate(self):
        from domain.quote import QuoteRecord

        cases = [
            ("ORDER_TOTAL", "READY", False, True),
            ("UNIT_PRICE", "READY", False, False),
            ("ORDER_TOTAL", "SUPERSEDED", False, False),
            ("ORDER_TOTAL", "READY", True, False),
        ]
        for index, (kind, status, inquiry, expected) in enumerate(cases):
            with self.subTest(kind=kind, status=status, inquiry=inquiry):
                self.quotes.create(QuoteRecord(
                    quote_id=f"tradable-{index}", provider="WANDA",
                    request_type="WPLUS_AREA", ticket_count=2,
                    unit_sell_price_fen=3800, total_sell_price_fen=7600,
                    status=status, metadata={"quote_kind": kind, "inquiry_only": inquiry},
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
                    **self.ident,
                ))
                quote = self.tools().read_quote()["quote"]
                self.assertEqual(quote["quote_kind"], kind)
                self.assertEqual(quote["tradable"], expected)
                self.assertEqual(quote["purchase_ready"], expected)

    def test_no_quote_asks_for_image_without_price(self):
        result = self.tools().get_purchase_guidance()
        self.assertIn("截图", result["guidance"])
        self.assertNotIn("元", result["guidance"])

    def test_city_evidence_and_expired_image_guard(self):
        self.assertFalse(
            self.tools("怎么买").execute("supplement_city", {"city": "深圳"})["ok"]
        )
        self.session.image_context["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        self.assertFalse(
            self.tools().execute("supplement_city", {"city": "深圳"})["ok"]
        )
        self.svc.flow.on_screenshot.assert_not_called()

    def test_city_continues_original_image_once_and_stops_model(self):
        model = ScriptedModel([tool('record_buyer_intent', intent='revise', evidence='是在深圳'),
                               tool("supplement_city", city="深圳")])
        self.svc.ai = conversation_ai.ConversationAI(model)
        with patch.object(self.svc, "_blacklist_gate", return_value=None):
            out = self.svc.on_message("c", "是在深圳", config=self.cfg)
        self.assertEqual(out.messages, ["核验后的报价"])
        self.svc.flow.on_screenshot.assert_called_once_with(
            self.conv,
            "https://example.com/seat.png",
            city_name="深圳",
            channel="liangpiao",
        )
        self.assertEqual(len(model.seen), 2)

    def test_natural_text_bypasses_keyword_and_does_not_confirm_payment(self):
        self.conv.state = ConvState.AWAIT_PAY
        self.add_order()
        model = ScriptedModel(
            [
                tool("get_purchase_guidance"),
                ModelReply(content="请查看原订单，勿重复下单。"),
            ]
        )
        self.svc.ai = conversation_ai.ConversationAI(model)
        with patch.object(service.qa_module, "reply", return_value="错误固定回复"):
            out = self.svc.on_message("c", "我拍好了怎么买", config=self.cfg)
        self.assertIn("请先不要付款或重复下单", out.messages[0])
        self.assertEqual(self.conv.state, ConvState.AWAIT_PAY)
        self.assertEqual(self.orders.get("o").payment_state, "UNPAID")

    def test_image_context_and_history_survive_reload(self):
        self.session.history = [{"role": "user", "content": "是在深圳"}]
        with patch.object(service.store, "save_sessions") as save:
            self.svc._save()
        with patch.object(
            service.store, "load_sessions", return_value=save.call_args.args[0]
        ):
            self.svc.sessions = {}
            self.svc._load()
        self.assertEqual(
            self.svc.sessions["c"].image_context, self.session.image_context
        )
        self.assertEqual(self.svc.sessions["c"].history, self.session.history)


class ModelConfigTests(unittest.TestCase):
    def test_missing_key_never_reports_connected(self):
        with patch.object(model_config.store, "load_credentials", return_value={}):
            self.assertFalse(model_config.status()["api_key_configured"])
            self.assertFalse(model_config.test_connection()["ok"])

    def test_connection_requires_real_chat_and_masks_errors(self):
        with (
            patch.object(
                model_config.store,
                "load_credentials",
                return_value={"model_api_key": "secret"},
            ),
            patch.object(model_config, "ModelClient") as client,
        ):
            client.return_value.chat.return_value = ModelReply(content="OK")
            self.assertTrue(model_config.test_connection()["ok"])
            client.return_value.chat.assert_called_once()
            client.return_value.chat.side_effect = RuntimeError("secret")
            self.assertNotIn("secret", str(model_config.test_connection()))

    def test_mask_cannot_replace_real_key(self):
        with self.assertRaises(ValueError):
            model_config.save_key("sk-••••ee50")


if __name__ == "__main__":
    unittest.main()
