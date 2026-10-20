import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock,patch
import settings
from application.service import Session
from application.buyer_assistant import BuyerAssistantTools
from application.ticket_details import extract,CHINA
from application.ticket_flow import FlowResult
from domain.conversation import Conversation,ConvState
from domain.order import OrderStore
from domain.quote import QuoteStore,QuoteRecord
from integrations.liangpiao.api import SeatMap


class TicketDetailsTests(unittest.TestCase):
    def setUp(self):
        self.identity=dict(tenant_id='107',shop_id='s',buyer_id='b',chat_id='c')
        self.conv=Conversation(**self.identity);self.conv.channel='wanda'
        self.session=Session(chat_id='c',conversation=self.conv)
        self.session.image_context={**self.identity,'url':'https://example.com/image.png',
            'recognition_facts':dict(cinema_id=10,cinema_name='包头万达影城',city_name='包头',cinema_hit_nums=1)}
        self.orders=OrderStore();self.quotes=QuoteStore()
        self.api=Mock()
        self.show=dict(showId='show1',cinemaId=10,movieId=20,movieName='神探之痕',hallName='7号厅',startTime='2026-10-16T21:15:00+08:00')
        self.api.ch4_show_list.return_value=[self.show]
        self.api.ch4_show_detail.return_value=self.show
        self.api.ch4_seat_list.return_value=SeatMap(seats=[dict(rowNo=7,colNo=n,status='AVAILABLE',seatId=str(n)) for n in (10,11,12)])
        def quoted(conv,result,out):
            self.result=result;conv.quote_id='new';return FlowResult(ok=True,messages=['真实接口核价结果'])
        self.flow=SimpleNamespace(api=self.api,_after_recognition=Mock(side_effect=quoted))
        self.service=SimpleNamespace(flow=self.flow,order_store=self.orders,quote_store=self.quotes)
        self.cfg=settings.Settings()
        self.clock=patch('application.ticket_details.now',return_value=datetime(2026,10,16,11,tzinfo=CHINA));self.clock.start();self.addCleanup(self.clock.stop)
        self.addCleanup(self.orders.close);self.addCleanup(self.quotes.close)

    def send(self,text,**fields):
        t=BuyerAssistantTools(self.service,self.session,text,self.cfg,lambda _:[])
        result=t.supplement_ticket_details(evidence=text,**(fields or extract(text,pending=bool(self.session.image_context.get('ticket_details')))))
        return t,result

    def complete(self):
        self.send('9:15 神探之痕 7排10，11，12')
        self.send('今天')
        return self.send('晚上')

    def test_three_messages_merged_and_ambiguous_time_not_guessed(self):
        t,_=self.send('9:15 神探之痕 7排10，11，12')
        self.assertIn('观影日期',t.flow_result.messages[0])
        t,_=self.send('今天')
        self.assertIn('上午还是晚上',t.flow_result.messages[0]);self.api.ch4_show_list.assert_not_called()
        t,r=self.send('晚上')
        self.assertTrue(r['ok']);self.assertEqual(self.result.expected_seat_count,3)
        self.assertEqual(self.result.start_time,'2026-10-16T21:15:00+08:00')
        self.assertFalse(self.session.image_context['ticket_details']['pending'])

    def test_no_show_never_uses_end_time(self):
        self.api.ch4_show_list.return_value=[{**self.show,'startTime':'2026-10-16T18:55:00+08:00','endTime':'2026-10-16T21:15:00+08:00'}]
        t,_=self.complete();self.assertIn('没有找到',t.flow_result.messages[0]);self.flow._after_recognition.assert_not_called()

    def test_wanda_seats_are_checked_by_wanda_route_not_liangpiao(self):
        self.complete()
        self.assertTrue(self.result.raw_results['buyer_text_details'])
        self.api.ch4_seat_list.assert_not_called()

    def test_identity_mismatch_blocks(self):
        self.session.image_context['shop_id']='other'
        _,r=self.send('今天',date='今天');self.assertFalse(r['ok']);self.api.ch4_show_list.assert_not_called()

    def test_invented_movie_rejected(self):
        _,r=self.send('今天',movie='神探之痕',date='今天');self.assertFalse(r['ok']);self.assertNotIn('ticket_details',self.session.image_context)

    def test_changed_request_supersedes_old_quote(self):
        self.quotes.create(QuoteRecord(quote_id='old',**self.identity))
        self.conv.quote_id='old'
        self.send('9:15 神探之痕 7排10，11，12')
        self.assertEqual(self.quotes.get('old').status,'SUPERSEDED');self.assertEqual(self.conv.quote_id,'')

    def test_paid_order_prevents_replacement(self):
        self.service.order_store=SimpleNamespace(by_identity=lambda _: [SimpleNamespace(state='PAID',payment_state='PAID',payload={})])
        _,r=self.send('今天',date='今天');self.assertFalse(r['ok']);self.assertNotIn('ticket_details',self.session.image_context)

    def test_detail_conflict_blocks(self):
        self.api.ch4_show_detail.return_value={**self.show,'cinemaId':999}
        _,r=self.complete();self.assertFalse(r['ok']);self.flow._after_recognition.assert_not_called()

    def test_api_failure_preserves_demand(self):
        self.api.ch4_show_list.side_effect=TimeoutError()
        t,r=self.complete();self.assertFalse(r['ok']);self.assertEqual(self.session.image_context['ticket_details']['movie'],'神探之痕')

    def test_plain_quantity_cannot_resume_old_image(self):
        self.send('9:15 神探之痕 7排10，11，12')
        t=BuyerAssistantTools(self.service,self.session,'三张',self.cfg,lambda _:[])
        self.assertFalse(t.supplement_ticket_count('三张')['ok'])

    def test_alternatives_not_extracted(self):
        self.assertEqual(extract('不要7排10，11，12'),{})

    def test_movie_title_is_not_a_question_or_negation(self):
        for movie in ('什么意思夫妇','不说话的爱'):
            message=f'21:15 {movie} 7排10座'
            self.assertEqual(extract(message)['movie'],movie)
            t,r=self.send(message)
            self.assertTrue(r['ok']);self.assertEqual(self.session.image_context['ticket_details']['movie'],movie)

    def test_inquiry_keeps_previous_quote(self):
        self.quotes.create(QuoteRecord(quote_id='old',**self.identity))
        self.assertEqual(extract('7排10座多少钱？'),{})
        self.send('7排10座多少钱？',seats='7排10座')
        self.assertEqual(self.quotes.get('old').status,'READY')
        for text in ('7排10座有票吗','7排10座是否可售','7排10座能否购买'):
            self.assertEqual(extract(text),{})
            _,reply=self.send(text,seats='7排10座');self.assertFalse(reply['ok'])
        self.assertEqual(self.quotes.get('old').status,'READY')

    def test_service_merges_separate_messages(self):
        from application.service import TicketService
        svc=TicketService.__new__(TicketService)
        svc.sessions={'c':self.session};svc.persist=False
        svc.flow=self.flow;svc.quote_store=self.quotes;svc.order_store=self.orders
        first=svc.on_message('c','9:15 神探之痕 7排10，11，12',config=self.cfg)
        second=svc.on_message('c','今天',config=self.cfg)
        third=svc.on_message('c','晚上',config=self.cfg)
        self.assertIn('观影日期',first.messages[0]);self.assertIn('上午还是晚上',second.messages[0])
        self.assertEqual(third.messages,['真实接口核价结果'])

    def test_truncated_cinema_requires_confirmation(self):
        self.session.image_context['recognition_facts']['cinema_truncated']=True
        t,_=self.complete();self.assertIn('影院名称被截断',t.flow_result.messages[0]);self.flow._after_recognition.assert_not_called()
        t,r=self.send('包头万达影城',cinema='包头万达影城');self.assertTrue(r['ok']);self.assertTrue(self.result.cinema_truncated)

    def test_quantity_after_success_cannot_go_back_to_old_image(self):
        self.complete()
        t=BuyerAssistantTools(self.service,self.session,'三张',self.cfg,lambda _:[])
        self.assertFalse(t.supplement_ticket_count('三张')['ok'])

    def test_wanda_cost_rejects_sold_or_duplicate_seat(self):
        from application.wanda_cost import resolve_cost,WandaCostError
        from integrations.wanda.bridge import MemberArea,SeatMapResult
        bridge=Mock();bridge.wanda_lookup.return_value=SimpleNamespace(use_wanda=True,wanda_cinema_id='w')
        area=MemberArea(area_id='a',area_name='普通',member_price_fen=3000,seats=[{'name':'7排10座','status':0}])
        bridge.seat_map.return_value=SeatMapResult(showtime_id='w1',phone='p',areas=[area])
        with self.assertRaises(WandaCostError):resolve_cost(bridge,city_id='1',cinema_id='10',showtime_resolver=lambda _:'w1',phone='p',seat_names=['7排10座'],require_available=True)
        area.seats[0]['status']=1
        cost=resolve_cost(bridge,city_id='1',cinema_id='10',showtime_resolver=lambda _:'w1',phone='p',seat_names=['7排10座'],require_available=True)
        self.assertIsNotNone(cost)

    def test_hadong_alias_uses_directory_then_queries_exact_show(self):
        facts=self.session.image_context['recognition_facts']
        facts.update(cinema_id=4019,cinema_name='万达影城（哈东IMAXGT双激光店地下停车首赠1H）',
                     city_name='哈尔滨',final_results={'cityCode':'230100'})
        self.api.ch4_cinema_list.return_value={'list':[dict(cinemaId=4019,name=facts['cinema_name'],cityName='哈尔滨')]}
        self.show.update(cinemaId=4019,movieName='生化危机：爆发夜',startTime='2026-10-18T15:20:00+08:00')
        text='《生化危机:爆发夜》儒意影城哈东万达广场店 10月18日 15:20-17:24 7排21'
        _,reply=self.send(text,movie='生化危机:爆发夜',cinema='儒意影城哈东万达广场店',date='10月18日',time='15:20',seats='7排21')
        self.assertTrue(reply['ok'])
        self.assertEqual(self.result.cinema_id,4019)
        self.assertEqual(self.result.movie_name,'生化危机：爆发夜')
        self.assertEqual(self.result.expected_seat_count,1)
        self.assertEqual(self.result.start_time,'2026-10-18T15:20:00+08:00')
        self.api.ch4_show_list.assert_called_once_with(4019,'2026-10-18')
        self.assertEqual(self.result.seats[0]['seatNo'],'7排21座')

    def test_ambiguous_cinema_keeps_text_and_never_quotes_old_branch(self):
        self.session.image_context['recognition_facts'].update(city_name='哈尔滨',final_results={'cityCode':'230100'})
        self.api.ch4_cinema_list.return_value={'list':[dict(cinemaId=i,name='万达影城（哈东店）',cityName='哈尔滨') for i in (4019,4020)]}
        _,reply=self.send('哈东万达店 今天 21:15 神探之痕 7排10座',cinema='哈东万达店',date='今天',time='21:15',movie='神探之痕',seats='7排10座')
        self.assertTrue(reply['ok'])
        self.assertEqual(self.session.image_context['ticket_details']['awaiting'],'cinema_identity')
        self.assertEqual(self.session.image_context['ticket_details']['seats'][0]['seatNo'],'7排10座')
        self.api.ch4_show_list.assert_not_called()
        self.flow._after_recognition.assert_not_called()

    def test_alias_confirmation_persists_across_supplements_without_rematching(self):
        facts=self.session.image_context['recognition_facts']
        facts.update(cinema_name='万达影城（包头九原店）',final_results={'cityCode':'150200'})
        self.api.ch4_cinema_list.return_value={'list':[dict(cinemaId=10,name=facts['cinema_name'],cityName='包头')]}
        self.send('儒意影城包头九原万达广场店',cinema='儒意影城包头九原万达广场店')
        self.complete()
        self.api.ch4_cinema_list.assert_called_once()

    def test_paid_order_blocks_alias_change_before_directory_lookup(self):
        self.service.order_store=SimpleNamespace(by_identity=lambda _:[SimpleNamespace(state='PAID',payment_state='PAID',payload={})])
        _,reply=self.send('哈东万达店',cinema='哈东万达店')
        self.assertFalse(reply['ok'])
        self.api.ch4_cinema_list.assert_not_called()

if __name__=='__main__':unittest.main()
