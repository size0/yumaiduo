import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from application.assistant_scope import allowed, stage_for
from domain.conversation import ConvState
from settings import Assistant

class ScopeTests(unittest.TestCase):
    def conv(self,state):return SimpleNamespace(state=state,channel='wanda',quote_id='q',tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
    def test_off_means_all_stages_not_master_off(self):
        a=Assistant(stage_filter_enabled=False)
        for state in ConvState:self.assertTrue(allowed(a,self.conv(state)))
        a.agent_enabled=False
        self.assertFalse(allowed(a,self.conv(ConvState.IDLE)))
    def test_stage_boundaries(self):
        a=Assistant(stage_filter_enabled=True,allowed_stages=['intake'])
        self.assertTrue(allowed(a,self.conv(ConvState.QUOTED)))
        for state in [ConvState.AWAIT_REPRICE,ConvState.AWAIT_PAY,ConvState.AWAIT_TICKET,ConvState.DELIVERED,ConvState.REFUNDING]:
            self.assertFalse(allowed(a,self.conv(state)))
        a.allowed_stages=['intake','quote','order']
        self.assertTrue(allowed(a,self.conv(ConvState.AWAIT_TICKET)))
        self.assertFalse(allowed(a,self.conv(ConvState.REFUNDED)))
    def test_order_fact_identity_and_no_downgrade(self):
        c=self.conv(ConvState.IDLE)
        o=SimpleNamespace(quote_id='old',is_terminal=False,tenant_id='t',shop_id='s',buyer_id='b',chat_id='c',state='PAID',shipped=False,payment_state='CONFIRMED',platform_order_id='p',provider='WANDA',provider_order_id='p')
        store=Mock();store.by_identity.return_value=[o]
        self.assertEqual(stage_for(c,store),'order')
        c.quote_id=''
        self.assertEqual(stage_for(c,store),'order')
        c.state=ConvState.DELIVERED
        self.assertEqual(stage_for(c,store),'after_sale')
        c.state=ConvState.IDLE;o.buyer_id='other'
        self.assertEqual(stage_for(c,store),'intake')
    def test_invalid_config(self):
        self.assertTrue(Assistant(stage_filter_enabled='false').validate())
        self.assertTrue(Assistant(allowed_stages=['after_sale']).validate())
        self.assertFalse(Assistant().validate())
    def test_service_gate_does_not_call_model_or_handoff(self):
        from application.service import TicketService
        svc=TicketService.__new__(TicketService);svc.order_store=None;svc.ai=Mock();svc._finish=lambda session,result,**kwargs:result
        session=SimpleNamespace(conversation=self.conv(ConvState.DELIVERED))
        cfg=SimpleNamespace(assistant=Assistant(stage_filter_enabled=True,allowed_stages=['intake']))
        result=svc._answer_with_ai(session,'你好',cfg)
        self.assertEqual(result.messages,[]);self.assertFalse(result.need_human);svc.ai.answer.assert_not_called()
if __name__=='__main__':unittest.main()
