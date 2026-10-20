from copy import deepcopy
from application.ticket_details import parse_date,extract,cinema_confirmation
from application.service import TicketService
from tests.test_ticket_details import TicketDetailsTests


class DateConfirmationTests(TicketDetailsTests):
    def test_short_dates(self):
        for text in ('10-16','10/16','10月16日','10月16号'):
            self.assertEqual(parse_date(text),'2026-10-16')
            self.assertEqual(extract(text,True),{'date':text})

    def test_invalid_date_is_chinese(self):
        for text in ('02-30','十三月','2026-19-40'):
            with self.assertRaisesRegex(ValueError,'日期没有识别清楚'):
                parse_date(text)

    def test_real_message_sequence_accepts_yes(self):
        svc=TicketService.__new__(TicketService)
        svc.sessions={'c':self.session};svc.persist=False
        svc.flow=self.flow;svc.quote_store=self.quotes;svc.order_store=self.orders
        self.session.image_context['recognition_facts']['cinema_truncated']=True
        for message in ('9:15 神探之痕 7排10，11，12','10-16','晚上'):
            out=svc.on_message('c',message,config=self.cfg)
        self.assertIn('回复“是”',out.messages[0])
        out=svc.on_message('c','是',config=self.cfg)
        self.assertEqual(out.messages,['真实接口核价结果'])
        self.assertEqual(self.session.image_context['ticket_details']['cinema_confirmed'],'包头万达影城')

    def test_yes_cannot_confirm_without_matching_question(self):
        self.session.image_context['ticket_details']={'pending':True,'awaiting':'cinema_confirmation'}
        self.session.history=[{'role':'assistant','content':'是否付款？'}]
        self.assertEqual(cinema_confirmation(self.session,'是'),'')

    def test_old_handoff_can_recover_only_same_cinema(self):
        self.session.image_context['ticket_details']={'pending':True}
        self.session.history=[{'role':'assistant','content':'截图影院名称被截断，请确认是否为：包头万达影城。若是，请回复完整影院名'},
                              {'role':'user','content':'是'},
                              {'role':'assistant','content':'影院这边我帮你转人工核对下，稍等哈~'}]
        self.assertEqual(cinema_confirmation(self.session,'是'),'包头万达影城')
        self.session.image_context['recognition_facts']['cinema_name']='深圳万达'
        self.assertEqual(cinema_confirmation(self.session,'是'),'')

    def test_new_question_clears_yes_scope(self):
        self.session.image_context['ticket_details']={'pending':True,'awaiting':'time_period'}
        self.session.history=[{'role':'assistant','content':'截图影院名称被截断，请确认是否为：包头万达影城。'}]
        self.assertEqual(cinema_confirmation(self.session,'是'),'')
