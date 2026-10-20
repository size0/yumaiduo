from datetime import datetime,timedelta,timezone
from tests.test_buyer_assistant import BuyerAssistantTests,ScriptedModel
from application import conversation_ai
from domain.quote import QuoteRecord
from integrations.model.client import ModelReply

class AITransactionTruthTests(BuyerAssistantTests):
    def setup_mismatch(self):
        self.add_order(amount_fen=3290)
        self.conv.channel='wanda'
        self.quotes.create(QuoteRecord(quote_id='new',provider='WANDA',request_type='WPLUS_AREA',ticket_count=3,total_sell_price_fen=13560,unit_sell_price_fen=4520,expires_at=datetime.now(timezone.utc)+timedelta(minutes=10),metadata={'quote_kind':'ORDER_TOTAL'},**self.ident))

    def test_model_skips_tools_cannot_claim_reprice_or_automatic_ticketing(self):
        self.setup_mismatch()
        model=ScriptedModel([ModelReply(content='这单135.60元，已经改好了哈，直接付款，系统自动出票。')])
        self.svc.ai=conversation_ai.ConversationAI(model)
        out=self.svc._answer_with_ai(self.session,'改价',self.cfg)
        self.assertIn('32.90',out.messages[0])
        self.assertIn('135.60',out.messages[0])
        self.assertIn('请先不要付款',out.messages[0])
        self.assertNotIn('已经改好了',out.messages[0])
        self.assertNotIn('自动出票',out.messages[0])
        self.assertIn('32.90',model.seen[0][1]['system'])

    def test_prompt_not_required_for_guard(self):
        self.setup_mismatch()
        self.assertIn('请先不要付款',self.tools('一致').guard_reply('直接付款就行'))

    def test_store_failure_cannot_pass_claim(self):
        self.svc.order_store=None
        self.assertIn('请先不要付款',self.tools('改价').guard_reply('已经改好了'))

    def test_wanda_automatic_ticket_promise_without_payment_words(self):
        self.conv.channel='wanda'
        self.assertNotIn('自动出票',self.tools('多久出票').guard_reply('系统会自动出票'))

    def test_paid_order_blocks_payment_invitation(self):
        self.add_order(payment_state='PAID')
        self.assertNotEqual(self.tools('怎么买').guard_reply('直接拍下付款就可以'),'直接拍下付款就可以')

    def test_missing_records_blocks_payment_invitation(self):
        from domain.conversation import ConvState
        self.conv.state=ConvState.AWAIT_PAY
        self.assertIn('记录不完整',self.tools('怎么买').guard_reply('直接拍下付款就可以'))

    def test_multiple_orders_blocks_payment_invitation(self):
        self.add_order()
        self.add_order(order_id='second',quote_id='q2',idempotency_key='second',provider_order_id='second')
        self.assertIn('多笔订单',self.tools('怎么买').guard_reply('直接拍下付款就可以'))

    def test_alternate_transaction_claims_are_blocked(self):
        self.setup_mismatch()
        for content in ('价格已更新为135.60元','价格调整好了','现在是135.60元，可以付了','已经给你调好了'):
            with self.subTest(content=content):
                reply=self.tools('一致').guard_reply(content)
                self.assertIn('请先不要付款',reply)
                self.assertIn('32.90',reply)
