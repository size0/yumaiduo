"""Partial buyer text supplements verified image facts; conflicts still block."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from application.seat_request import reconcile
from application.ticket_details import parse_time, CHINA
from application.ticket_flow import FlowResult
from domain.conversation import Conversation
from domain.order import OrderStore
from domain.quote import QuoteStore
from integrations.liangpiao.api import RecognitionResult

I=dict(tenant_id='107',shop_id='s',buyer_id='b',chat_id='c')


@pytest.mark.parametrize('literal,evidence,expected', [
    ('三点五十','明天下午 三点五十',('15:50',False)),
    ('3:50','明天下午3:50',('15:50',False)),
    ('0350','明天下午0350',('15:50',False)),
    ('三点五十','明天上午三点五十',('03:50',False)),
    ('三点五十','三点五十',('03:50',True)),
    ('三点五十','下午看电影，三点五十',('03:50',True)),
    ('三点五十','上午三点五十或下午三点五十',None),
])
def test_time_keeps_only_adjacent_literal_period(literal,evidence,expected):
    if expected is None:
        with pytest.raises(ValueError):parse_time(literal,evidence=evidence)
    else:
        assert parse_time(literal,evidence=evidence)==expected


def test_pre_image_save_preserves_period_even_if_model_submits_only_clock():
    orders,quotes=OrderStore(),QuoteStore()
    try:
        session=Session(chat_id='c',conversation=Conversation(**I))
        tools=BuyerAssistantTools(SimpleNamespace(order_store=orders,quote_store=quotes),session,
                                  '明天下午 三点五十',settings.Settings(),lambda _:[])
        tools.enable_semantic_routing();tools.semantic_intent='revise'
        with patch('application.ticket_details.now',return_value=datetime(2026,10,17,22,tzinfo=CHINA)):
            assert tools.supplement_ticket_details(evidence=tools.text,date='明天',time='三点五十')['ok']
        saved=session.image_context['awaiting_image_request']
        assert saved['time']=='15:50' and saved['ambiguous_time'] is False
        assert saved['evidence']==tools.text
    finally:
        orders.close();quotes.close()


def case():
    conv=Conversation(channel='wanda',**I)
    conv.demand_id,conv.demand_rev='current',1
    result=RecognitionResult(match_level='EXACT',cinema_id=2427,
        cinema_name='万达影城（长治万达IMAX广场店）',city_name='长治',movie_name='生化危机：爆发夜',
        start_time='2026-10-18T15:50:00+08:00',seats=[{'seatName':f'10排{col}座'} for col in (12,13,14)],
        seat_matched=True,price_all_fen=16260,seat_confirm_required=True,price_mismatch=True)
    request=dict(demand_id=conv.demand_id,demand_rev=conv.demand_rev)
    return conv,result,request


@pytest.mark.parametrize('fields', [dict(movie='生化危机爆发夜',date='2026-10-18',time='15:50',cinema='长治万达'),
    dict(time='15:50'),dict(date='2026-10-18'),dict(cinema='长治万达'),dict(city='长治')])
def test_matching_partial_text_preserves_image_seats_and_all_verification_flags(fields):
    conv,result,request=case();request.update(fields)
    before=deepcopy(result)
    assert reconcile(conv,result,request,None,FlowResult()) is None
    assert result==before  # No synthetic seats/count, no erased price or seat warnings.


@pytest.mark.parametrize('fields', [dict(time='03:50'),dict(time='15:50',ambiguous_time=True),
    dict(date='2026-10-19'),dict(movie='别的影片'),dict(cinema='包头万达'),dict(city='包头'),
    dict(time='15:50',ticket_count=2),dict(time='15:50',demand_rev=999),dict()])
def test_real_conflicts_and_empty_supplements_still_block(fields):
    conv,result,request=case();request.update(fields)
    assert reconcile(conv,result,request,None,FlowResult()) is not None


@pytest.mark.parametrize('fields', [dict(seats=[{'seatName':'1排1座'}]),
    dict(cinema='长治万达',seats=[{'seatName':'1排1座'}])])
def test_partial_scene_does_not_authorize_overwriting_image_seats(fields):
    conv,result,request=case();request.update(fields);before=deepcopy(result)
    assert reconcile(conv,result,request,None,FlowResult()) is not None
    assert result==before


def test_partial_same_seats_confirm_quantity_without_clearing_warnings():
    conv,result,request=case()
    request['seats']=deepcopy(result.seats)
    before=deepcopy(result)
    assert reconcile(conv,result,request,None,FlowResult()) is None
    assert result.seats==before.seats and result.price_mismatch==before.price_mismatch
    assert result.seat_confirm_required==before.seat_confirm_required
    assert result.expected_seat_count==len(before.seats)


@pytest.mark.parametrize('missing', ['demand_id','demand_rev'])
def test_partial_supplement_requires_explicit_current_version(missing):
    conv,result,request=case();request['time']='15:50';request.pop(missing)
    assert reconcile(conv,result,request,None,FlowResult()) is not None
