import json
from types import SimpleNamespace
from unittest.mock import Mock
from tests.test_ticket_details import TicketDetailsTests
from application.schedule_image import fallback
from application.image_context import preserve_image_result
from application.ticket_flow import FlowResult
from copy import deepcopy


class ScheduleImageTests(TicketDetailsTests):
    def setUp(self):
        super().setUp()
        self.model=Mock()
        self.model.chat.return_value=SimpleNamespace(content=json.dumps({'is_schedule':True,'cinema_text':'包头万达影城维多利摩尔城...','city':'包头'}))
        self.service._ai_for_config=lambda _:SimpleNamespace(client=self.model)
        self.api.ch4_city_list.return_value=[{'name':'包头','code':'150200'}]
        self.api.ch4_cinema_list.return_value={'total':1,'list':[{'cinemaId':1907,'name':'万达影城（包头维多利摩尔城IMAX店）','cityCode':'150200','cityName':'包头'}]}

    def test_real_shape_schedule_fallback_requires_confirmation(self):
        out=fallback(self.service,self.session,'https://example.com/schedule.png',FlowResult(ok=False),self.cfg)
        self.assertTrue(out.ok);self.assertIn('完整影院名',out.messages[0])
        self.assertEqual(self.session.image_context['recognition_facts']['cinema_id'],1907)
        self.assertTrue(self.session.image_context['recognition_facts']['cinema_truncated'])
        self.flow._after_recognition.assert_not_called()

    def test_ambiguous_catalog_does_not_select_first(self):
        self.api.ch4_cinema_list.return_value['total']=2
        out=fallback(self.service,self.session,'image',FlowResult(ok=False),self.cfg)
        self.assertFalse(out.ok)

    def test_hallucinated_city_not_in_title_is_rejected(self):
        self.model.chat.return_value.content=json.dumps({'is_schedule':True,'cinema_text':'万达影城...','city':'包头'})
        self.assertFalse(fallback(self.service,self.session,'image',FlowResult(ok=False),self.cfg).ok)
        self.api.ch4_city_list.assert_not_called()

    def test_old_quote_context_is_not_restored_after_fallback(self):
        previous={**self.session.image_context,'verified':True,'summary':'旧影院旧片','url':'old'}
        oldconv=deepcopy(self.conv.__dict__)
        self.session.image_context={**self.identity,'url':'new'}
        out=fallback(self.service,self.session,'new',FlowResult(ok=False),self.cfg)
        preserve_image_result(self.session,previous,oldconv,'new',out)
        self.assertNotIn('unverified_image',self.session.image_context)
        self.assertEqual(self.session.image_context['url'],'new')

    def test_failed_image_still_collects_new_text_without_old_cinema(self):
        self.session.image_context['unverified_image']='new'
        self.session.image_context['unverified_context']={**self.identity,'url':'new','recognition_facts':{}}
        t,r=self.send('9:15 神探之痕 7排10，11，12')
        self.assertTrue(r['ok']);self.assertIn('观影日期',t.flow_result.messages[0])
        self.assertEqual(self.session.image_context['url'],'new')
        self.assertEqual(self.session.image_context['recognition_facts'],{})

    def test_pending_pre_release_image_can_recover(self):
        from application.schedule_image import recover_pending
        self.session.image_context['unverified_image']='new'
        self.conv.supplement_recognition={'cinema_id':99}
        recover_pending(self.service,self.session,self.cfg)
        self.assertEqual(self.session.image_context['url'],'new')
        self.assertEqual(self.session.image_context['recognition_facts']['cinema_id'],1907)
        self.assertNotIn('unverified_image',self.session.image_context)
