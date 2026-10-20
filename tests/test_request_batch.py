"""Partial message tools must not terminate before all requested fields merge."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.conversation_ai import ConversationAI
from application.service import Session
from application.ticket_flow import FlowResult
from domain.conversation import Conversation,Event
from domain.order import OrderStore
from domain.quote import QuoteStore
from integrations.model.client import ModelReply
from tests import _check_channel_route as f
from tests.test_buyer_assistant import ScriptedModel,tool
from tests.test_quoted_supplements import setup


@pytest.mark.parametrize('text,fragment',[('还是两张','两张'),('要这两个','这两个')])
def test_unchanged_quantity_is_observation_then_model_ack_no_recognition(setup,text,fragment):
    make,session,quotes,service=setup
    t=make(text)
    model=ScriptedModel([tool('update_ticket_request',intent='revise',quantity=2,quantity_text=fragment,evidence=text),
        ModelReply(content=json.dumps(dict(answer='收到，仍是两张，刚才的报价已保留。',intent='revise',kind='acknowledgement',claims=[]),ensure_ascii=False))])
    reply,rounds,used=ConversationAI(model)._chat_with_tools([dict(role='user',content=text)],'',t)
    assert t.flow_result.messages==['收到，与刚才核实的购票信息一致，报价已保留。']
    assert len(model.seen)==1
    assert t.tool_results[0][1]['status']=='UNCHANGED'
    assert session.conversation.quote_id=='q'
    service.flow.on_screenshot.assert_not_called()


@pytest.fixture
def actual_flow():
    orders,quotes=OrderStore(),QuoteStore()
    shows=deepcopy(f.SHOWS)
    group=shows['data']['showtimeFilmInf'][0]['showtimeFilmDateInf'][0]
    group['date']=20301018
    start='2030-10-18T14:20:00+08:00'
    group['showtimesInf']['showtimeList'][0]['realtime']=int(datetime.fromisoformat(start).timestamp()*1000)
    seats=deepcopy(f.SEATS)
    seats['data']['realtimeSeats']['area'][0]['seat'][0]['name']='8排13座'
    seats['data']['realtimeSeats']['area'][0]['seat'][1]['name']='8排14座'
    with patch.dict(f.SHOWS,shows),patch.dict(f.SEATS,seats):
        flow,api,transport=f.flow(f.MAP_WANDA);flow.quote_store=quotes
        ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='batch')
        conv=Conversation(**ident,channel='wanda');conv.apply(Event.BUYER_IMAGE)
        image=f.result(start_time=start,expected_seat_count=2,price_all_fen=0,raw_results={})
        first=flow._after_recognition(conv,image,FlowResult())
        assert first.ok
        api.ch4_show_list=Mock(return_value=[dict(showId='12345',startTime=start,movieName=image.movie_name)])
        api.ch4_show_detail=Mock(return_value=dict(showId='12345',cinemaId=image.cinema_id,
            movieId=image.movie_id,movieName=image.movie_name,startTime=start,hallName=image.hall_name))
        session=Session(chat_id='batch',conversation=conv)
        session.image_context=dict(**ident,url='https://example.test/batch.jpg',verified=True,
            created_at=datetime.now(timezone.utc).isoformat(),demand_id=conv.demand_id,demand_rev=conv.demand_rev,
            recognition_facts=asdict(image))
        service=SimpleNamespace(flow=flow,order_store=orders,quote_store=quotes)
        def make(text):
            t=BuyerAssistantTools(service,session,text,settings.Settings(),lambda _:[])
            t.enable_semantic_routing();t.semantic_intent='revise';return t
        yield make,session,quotes,service,transport
    orders.close();quotes.close()


@pytest.mark.parametrize('quantity,text,fragment',[(2,'两张，改8排13座 8排14座','两张'),
    (2,'还是两张，改8排13座 8排14座','两张')])
def test_quantity_first_continues_to_seats_and_only_quotes_new_seats(actual_flow,quantity,text,fragment):
    make,session,quotes,service,transport=actual_flow
    t=make(text);old=session.conversation.quote_id;before=len(transport.calls)
    model=ScriptedModel([tool('update_ticket_request',intent='revise',quantity=quantity,quantity_text=fragment,evidence=text),
        tool('supplement_ticket_details',evidence=text,seats='8排13座 8排14座')])
    reply,rounds,used=ConversationAI(model)._chat_with_tools([dict(role='user',content=text)],'',t)
    assert used==['update_ticket_request','supplement_ticket_details'] and len(model.seen)==2
    q=quotes.get(session.conversation.quote_id)
    assert q.quote_id!=old and q.selected_seats==['8排13座','8排14座'] and q.ticket_count==2
    assert 'QUOTE_CARD' in t.flow_result.template_keys
    assert any('8排13座' in s for s in t.flow_result.messages)
    assert len(transport.calls)>before and not service.flow.api.create_calls


def test_changed_quantity_stages_then_merges_new_seat_without_old_ocr(actual_flow):
    make,session,quotes,service,transport=actual_flow
    text='一张，改8排13座';t=make(text);t.request_plan_enabled=True;old=session.conversation.quote_id
    before=len(transport.calls)
    result=t.execute('update_ticket_request',dict(intent='revise',quantity=1,quantity_text='一张',evidence=text,
        request_fields=['ticket_count','seats'],field_evidence={'ticket_count':'一张','seats':'8排13座'}))
    assert result['status']=='REQUEST_PLAN_STAGED' and t.flow_result is None
    assert result['outcome']=='NEEDS_INFORMATION' and not result['tradable']
    assert len(transport.calls)==before and quotes.get(old).status=='SUPERSEDED'
    assert session.conversation.quote_id==''
    result=t.execute('supplement_ticket_details',dict(evidence=text,seats='8排13座'))
    q=quotes.get(session.conversation.quote_id)
    assert result['ok'] and q.ticket_count==1 and q.selected_seats==['8排13座']
    assert 'QUOTE_CARD' in t.flow_result.template_keys


def test_changed_quantity_only_uses_current_plan_not_batch_completion(actual_flow):
    make,session,quotes,service,transport=actual_flow
    text='还是一张';t=make(text);t.request_plan_enabled=True
    t.execute('update_ticket_request',dict(intent='revise',quantity=1,quantity_text='一张',evidence=text,
        request_fields=['ticket_count']))
    assert not getattr(t,'request_batch_token',None)
    assert 'complete_ticket_request' not in {s['function']['name'] for s in t.schemas}
    before=len(transport.calls)
    assert not t.execute('complete_ticket_request',dict(evidence=text))['ok']
    assert len(transport.calls)==before


def test_batch_complete_is_retired_on_a_fresh_turn(actual_flow):
    make,session,quotes,service,transport=actual_flow
    text='还是一张';fresh=make(text);before=len(transport.calls)
    result=fresh.execute('complete_ticket_request',dict(evidence=text))
    assert not result['ok'] and result['status']=='FAILED'
    assert len(transport.calls)==before and fresh.flow_result is None


@pytest.mark.parametrize('text,quantity,fragment,extra', [
    ('一张，改8排13座',1,'一张',{'seats':'8排13座'}),
    ('三张，明天晚上8点',3,'三张',{'date':'明天','time':'晚上8点'}),
    ('一张，中间位置',1,'一张',{'position_preference':'中间位置'})])
def test_complete_cannot_skip_unmerged_seat_show_or_position(actual_flow,text,quantity,fragment,extra):
    make,session,quotes,service,transport=actual_flow
    t=make(text);t.request_plan_enabled=True;old=session.conversation.quote_id
    staged=t.execute('update_ticket_request',dict(intent='revise',quantity=quantity,quantity_text=fragment,evidence=text,
        request_fields=['ticket_count',*extra],field_evidence={'ticket_count':fragment,**extra}))
    assert staged['status']=='REQUEST_PLAN_STAGED'
    before=len(transport.calls)
    result=t.execute('complete_ticket_request',dict(evidence=text))
    assert not result['ok'] and result['status']=='FAILED'
    assert len(transport.calls)==before and t.flow_result is None
    assert quotes.get(old).status=='SUPERSEDED' and session.conversation.quote_id==''


@pytest.mark.parametrize('text',['不是一张','一张或者两张','一张8排13座','一张，换明天',
    '1张2026-10-18','还是一张但影院变了'])
def test_completion_quantity_grammar_does_not_extract_number_from_other_fields(text):
    from application.request_batch import positive_quantity
    assert positive_quantity(text) is None
