"""Unchanged supplements must preserve quotes; changed demands must reverify."""
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock,patch
from copy import deepcopy
from dataclasses import asdict

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from domain.conversation import Conversation,ConvState,Event
from domain.order import OrderStore
from domain.quote import QuoteRecord,QuoteStore


@pytest.fixture
def setup():
    identity=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
    orders,quotes=OrderStore(),QuoteStore()
    conv=Conversation(**identity,state=ConvState.QUOTED,channel='wanda')
    conv.demand_id,conv.demand_rev,conv.quote_id,conv.quote_kind='d',1,'q','ORDER_TOTAL'
    quotes.create(QuoteRecord(quote_id='q',**identity,demand_id='d',demand_rev=1,provider='WANDA',
        cinema='影城',movie='电影',show='2030-10-18T19:40:00+08:00',
        selected_seats=['6排11座','6排12座'],ticket_count=2,total_sell_price_fen=8000,
        metadata={'quote_kind':'ORDER_TOTAL'}))
    session=Session(chat_id='c',conversation=conv)
    session.image_context=dict(**identity,verified=True,url='https://example.test/img.jpg',
        demand_id='d',demand_rev=1,created_at=datetime.now(timezone.utc).isoformat(),
        recognition_facts=dict(match_level='EXACT',cinema_id='1',city_name='银川',cinema_name='影城'))
    service=SimpleNamespace(order_store=orders,quote_store=quotes,
        flow=SimpleNamespace(on_screenshot=Mock(),api=SimpleNamespace(ch4_show_list=Mock(return_value=[]))))
    def make(text):
        t=BuyerAssistantTools(service,session,text,settings.Settings(),lambda _:[])
        t.enable_semantic_routing();t.semantic_intent='revise'
        return t
    yield make,session,quotes,service
    orders.close();quotes.close()


@pytest.mark.parametrize('text,field', [('两张','count'),('两张','modern_count'),('两人','modern_count'),
    ('这两个','modern_count'),('两张','details_count'),
    ('6排11座 6排12座','seats'),('六排十二座 六排十一座','seats'),
    ('电影','movie'),('2030-10-18','date'),('晚上7:40','time'),('影城','cinema')])
def test_equivalent_supplements_across_turns_keep_same_quote(setup,text,field):
    make,session,quotes,service=setup
    before=quotes.get('q').to_dict()
    for _ in range(2):
        t=make(text)
        if field=='count':r=t.supplement_ticket_count(text)
        elif field=='modern_count':r=t.execute('update_ticket_request',
            {'intent':'revise','quantity':2,'quantity_text':text,'evidence':text})
        elif field=='details_count':
            from application.ticket_details import supplement
            r=supplement(t,text,ticket_count=2)
        else:r=t.execute('supplement_ticket_details',{'evidence':text,field:text})
        assert r['ok'] and r['status']=='UNCHANGED'
        assert t.flow_result.template_keys==['QUOTE_DETAILS_CONFIRMED']
        assert len(t.flow_result.messages)==1
        assert session.conversation.quote_id=='q'
    assert quotes.get('q').to_dict()==before
    service.flow.on_screenshot.assert_not_called()
    service.flow.api.ch4_show_list.assert_not_called()


@pytest.mark.parametrize('kind',['UNIT_PRICE','changed','stale','new_image','pending','other_chat'])
def test_quantity_changes_unknown_quantity_and_stale_context_do_not_short_circuit(setup,kind):
    make,session,quotes,service=setup
    if kind=='UNIT_PRICE':
        q=quotes.get('q');q.metadata['quote_kind']='UNIT_PRICE';q.ticket_count=0;quotes.put(q)
    elif kind=='stale':session.image_context['demand_rev']=0
    elif kind=='new_image':session.image_context['unverified_image']='https://example.test/new.jpg'
    elif kind=='pending':session.image_context['ticket_details']={'pending':True}
    elif kind=='other_chat':session.image_context['chat_id']='other'
    text='三张' if kind=='changed' else '两张'
    t=make(text);t._resume_quote=Mock(return_value={'ok':False,'status':'REVERIFY'})
    assert t.supplement_ticket_count(text)['status']=='REVERIFY'
    t._resume_quote.assert_called_once_with(ticket_count=3 if kind=='changed' else 2)


@pytest.mark.parametrize('field,text',[('seats','6排13座 6排14座'),('position_preference','中间位置')])
def test_changed_seats_and_preferences_invalidate_old_quote(setup,field,text):
    make,session,quotes,service=setup
    t=make(text)
    r=t.execute('supplement_ticket_details',{'evidence':text,field:text})
    assert r.get('status')!='UNCHANGED'
    assert session.conversation.quote_id==''
    assert quotes.get('q').status=='SUPERSEDED'
    assert not t.flow_result or not any(k in ('QUOTE_CARD','QUOTE_CARD_MULTI') for k in t.flow_result.template_keys)


def test_completed_same_demand_supplement_can_be_acknowledged(setup):
    make,session,quotes,service=setup
    session.image_context['ticket_details']=dict(pending=False,source_image=session.image_context['url'],
        demand_id='d',demand_rev=1,seats=[{'seatNo':'6排11座'},{'seatNo':'6排12座'}])
    t=make('6排11座 6排12座')
    r=t.execute('supplement_ticket_details',{'evidence':t.text,'seats':t.text})
    assert r['status']=='UNCHANGED'


def test_matching_count_in_mixed_message_does_not_hide_seat_change(setup):
    make,session,quotes,service=setup
    t=make('两张，换8排')
    t._resume_quote=Mock(return_value={'ok':False,'status':'REVERIFY'})
    r=t.execute('update_ticket_request',dict(intent='revise',quantity=2,quantity_text='两张',evidence=t.text))
    assert r['status']=='UNCHANGED_FIELDS'
    assert t.flow_result is None
    t._resume_quote.assert_not_called()


def test_unit_price_then_first_quantity_quotes_once_then_repeat_only_acknowledges():
    from tests import _check_channel_route as f
    from application.ticket_flow import FlowResult
    orders,quotes=OrderStore(),QuoteStore()
    try:
        flow,api,transport=f.flow(f.MAP_WANDA)
        flow.quote_store=quotes
        ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='continuous')
        conv=Conversation(**ident,channel='wanda')
        image=f.result(start_time='2030-10-18T14:20:00+08:00',expected_seat_count=None,
                       price_all_fen=0,raw_results={})
        shows=deepcopy(f.SHOWS)
        shows['data']['showtimeFilmInf'][0]['showtimeFilmDateInf'][0]['date']=20301018
        shows['data']['showtimeFilmInf'][0]['showtimeFilmDateInf'][0]['showtimesInf']['showtimeList'][0]['realtime']=int(
            datetime.fromisoformat(image.start_time).timestamp()*1000)
        with patch.dict(f.SHOWS,shows):
            conv.apply(Event.BUYER_IMAGE)
            out=flow._after_recognition(conv,image,FlowResult())
            assert out.ok and out.template_keys==['WANDA_UNIT_PRICE']
            session=Session(chat_id=ident['chat_id'],conversation=conv)
            session.image_context=dict(**ident,url='https://example.test/continuous.jpg',verified=True,
                demand_id=conv.demand_id,demand_rev=conv.demand_rev,
                created_at=datetime.now(timezone.utc).isoformat(),recognition_facts=asdict(image))
            def screenshot(c,url,**params):
                c.apply(Event.BUYER_IMAGE)
                fresh=deepcopy(image);fresh.expected_seat_count=params['ticket_count']
                return flow._after_recognition(c,fresh,FlowResult())
            with patch.object(flow,'on_screenshot',side_effect=screenshot) as recognize:
                service=SimpleNamespace(flow=flow,order_store=orders,quote_store=quotes)
                def tools():
                    t=BuyerAssistantTools(service,session,'两张',settings.Settings(),lambda _:[])
                    t.enable_semantic_routing();return t
                first=tools()
                args=dict(intent='revise',quantity=2,quantity_text='两张',evidence='两张')
                assert first.execute('update_ticket_request',args)['ok']
                assert 'QUOTE_CARD' in first.flow_result.template_keys
                first_quote=conv.quote_id;before_calls=len(transport.calls)
                second=tools()
                from application.quote_confirmation import current_quote
                assert current_quote(second) is not None, (session.image_context,quotes.get(conv.quote_id).to_dict(),conv.state)
                assert second.execute('update_ticket_request',args)['status']=='UNCHANGED'
                assert second.flow_result.template_keys==['QUOTE_DETAILS_CONFIRMED']
                assert conv.quote_id==first_quote and len(transport.calls)==before_calls
                recognize.assert_called_once()
        assert not api.create_calls and not api.preflight_calls
    finally:
        orders.close();quotes.close()
