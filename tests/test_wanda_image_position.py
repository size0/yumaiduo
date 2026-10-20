"""Text seats after a mapped non-Wanda-branded image use fresh Wanda facts."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from application.ticket_flow import FlowResult
from domain.conversation import Conversation
from domain.order import OrderStore,Order
from domain.quote import QuoteStore
from integrations.liangpiao.api import RecognitionResult

I=dict(tenant_id='107',shop_id='s',buyer_id='b',chat_id='c')


@pytest.fixture
def case():
    conv=Conversation(**I);conv.demand_id,conv.demand_rev='d',1
    session=Session(chat_id='c',conversation=conv)
    session.image_context=dict(I,url='image',created_at=datetime.now(timezone.utc).isoformat(),demand_id='d',demand_rev=1,
        recognition_facts=dict(match_level='CANDIDATE',cinema_id=10025,cinema_name='寰映影城',
        raw_results=dict(city='厦门',cinema='厦门寰映影城海上世界店',film='复仇者联盟4',hall='IMAX厅银幕',showtime='2026-10-18 10:00:00',seat=[])))
    orders,quotes=OrderStore(),QuoteStore()
    flow=SimpleNamespace(wanda_bridge=Mock(),_after_recognition=Mock(return_value=FlowResult(ok=True,messages=['核价结果'])))
    service=SimpleNamespace(flow=flow,order_store=orders,quote_store=quotes)
    tool=BuyerAssistantTools(service,session,'7排18',settings.Settings(),lambda _:[])
    tool.enable_semantic_routing();tool.semantic_intent='revise'
    yield tool
    orders.close();quotes.close()


def test_nonbranded_candidate_saves_seat_and_revalidates_wanda_before_quote(case):
    verified=RecognitionResult(match_level='EXACT',cinema_name='厦门寰映影城海上世界店',movie_name='复仇者联盟4',start_time='2026-10-18T10:00:00+08:00',hall_name='IMAX厅')
    with patch('application.wanda_recovery.recover',return_value=verified) as recover:
        result=case.supplement_ticket_details(evidence=case.text,seats='7排18')
    assert result['ok'];recover.assert_called_once()
    saved=case.session.image_context['ticket_details']
    assert saved['seats'][0]['seatNo']=='7排18座' and saved['ticket_count']==1
    submitted=case.service.flow._after_recognition.call_args.args[1]
    assert submitted.seats[0]['seatNo']=='7排18座' and submitted.raw_results['buyer_text_details']


@pytest.mark.parametrize('bad', ['identity','version','expired','inquiry','price_warning','paid','ambiguous'])
def test_position_never_borrows_invalid_image_or_changes_paid_order(case,bad):
    ctx=case.session.image_context
    if bad=='identity':ctx['buyer_id']='other'
    if bad=='version':ctx['demand_rev']=0
    if bad=='expired':ctx['created_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    if bad=='inquiry':case.conv.demand_inquiry=True
    if bad=='price_warning':ctx['recognition_facts']['price_mismatch']=True
    if bad=='paid':case.service.order_store.create(Order(order_id='o',quote_id='q',idempotency_key='test-paid',provider='WANDA',state='PAID',payment_state='CONFIRMED',**I))
    if bad=='ambiguous':case.text='7.18'
    with patch('application.wanda_recovery.recover') as recover:
        result=case.supplement_ticket_details(evidence=case.text,seats='7排18')
    assert not result['ok'];recover.assert_not_called()
    case.service.flow._after_recognition.assert_not_called()


def test_failed_matching_keeps_explicit_seats_pending_instead_of_old_candidates(case):
    with patch('application.wanda_recovery.recover',side_effect=ValueError('private upstream error')):
        result=case.supplement_ticket_details(evidence=case.text,seats='7排18')
    assert not result['ok']
    assert case.session.image_context['ticket_details']['seats'][0]['seatNo']=='7排18座'
    assert 'private' not in result['reason']


@pytest.mark.parametrize('pending', ['time','movie','cinema','date','ticket_count','awaiting_image_request'])
def test_position_preserves_existing_pending_constraints(case,pending):
    ctx=case.session.image_context
    if pending=='awaiting_image_request':
        ctx[pending]={'movie':'新影片','ticket_count':2}
    else:
        ctx['ticket_details']={'source_image':ctx['url'],pending:2 if pending=='ticket_count' else '已补充内容'}
    before=deepcopy(ctx)
    with patch('application.wanda_recovery.recover') as recover:
        result=case.supplement_ticket_details(evidence=case.text,seats='7排18')
    assert result['status']=='PENDING_DETAILS_CONFLICT'
    assert ctx==before
    recover.assert_not_called()


@pytest.mark.parametrize('status,code', [(0,'SEAT_UNAVAILABLE'), (1,None)])
def test_explicit_seat_availability_has_truthful_reason(status,code):
    from application.wanda_cost import resolve_cost,WandaCostError
    from application.ticket_flow import _wanda_cost_hint
    from integrations.wanda.bridge import MemberArea,SeatMapResult
    bridge=Mock();bridge.wanda_lookup.return_value=SimpleNamespace(use_wanda=True,wanda_cinema_id='w')
    area=MemberArea(area_id='a',area_name='普通',member_price_fen=4300,seats=[{'name':'7排18座','status':status}])
    bridge.seat_map.return_value=SeatMapResult(showtime_id='w1',phone='p',areas=[area])
    args=dict(city_id='1',cinema_id='10',showtime_resolver=lambda _:'w1',phone='p',seat_names=['7排18座'],require_available=True)
    if code:
        with pytest.raises(WandaCostError) as caught:resolve_cost(bridge,**args)
        assert caught.value.code==code
        hint=_wanda_cost_hint(caught.value)
        assert '7排18座' in hint and '不可售' in hint and '没找到' not in hint
    else:
        assert resolve_cost(bridge,**args).unit_cost_fen==4300
