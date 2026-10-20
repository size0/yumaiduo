"""Comparison facts survive followups/restarts without becoming transaction quotes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from application.buyer_assistant import BuyerAssistantTools
from application.service import Session,TicketService
from application.assistant_reply import _unsupported_amount, assess, unfinished_reply
from application.quote_comparison import lookup
from domain.conversation import Conversation,ConvState
import settings


def tools():
    ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
    conv=Conversation(**ident,state=ConvState.SELF_BUY,channel='wanda')
    conv.quote_comparison=dict(**ident,demand_id='',demand_rev=0,status='SELF_BUY_RECOMMENDED',tradable=False,
        quote_kind='COMPARISON_ONLY',provider='WANDA',seat_count=2,sell_unit_fen=3500,sell_total_fen=7000,
        buyer_total_fen=6600,cost_total_fen=6800,reason='本次规则售价不低于截图自购总价')
    session=Session(chat_id='c',conversation=conv,image_context={**ident,'verified':True,
        'recognition_facts':{'match_level':'EXACT','cinema_name':'万达影城','seats':[{},{}]}})
    service=SimpleNamespace(quote_store=SimpleNamespace(by_identity=lambda _ : []))
    return BuyerAssistantTools(service,session,'多少钱',settings.Settings(),lambda _:[])


def test_followup_does_not_ask_for_same_image_and_is_not_tradable():
    t=tools()
    result=t.read_quote()
    assert result['status']=='SELF_BUY_RECOMMENDED' and result['quote'] is None
    assert result['comparison']['sell_total_fen']==7000 and not result['tradable']
    assert '需要选座截图' not in result['reason']


def test_known_price_explanation_survives_but_payment_invitation_is_blocked():
    t=tools()
    t.tool_results=[('read_quote',t.read_quote())]
    assert '35.00元/张' in assess(t,'上次规则35元/张，两张70元，截图自购66元，所以建议自购。').text
    assert _unsupported_amount(t,'上次只要28.33元')
    t.get_purchase_guidance=Mock(return_value={'ok':True,'payment_blocked':True,'guidance':'比价记录不能用于付款'})
    assert assess(t,'可以付款70元').text=='比价记录不能用于付款'


def test_swapped_unit_total_or_sources_are_replaced_with_typed_price_paragraph():
    t=tools();t.tool_results=[('read_quote',t.read_quote())]
    for wrong in ('上次报价70元一张，两张合计35元。','截图自购35元，规则售价66元。',
                  '上次70一张，两张35，截图自购35。','每张70，总价35，自购价35。'):
        decision=assess(t,wrong)
        assert decision.reason=='COMPARISON_PRICE_PROJECTION'
        assert '35.00元/张，合计70.00元' in decision.text
        assert '截图自购合计66.00元' in decision.text


def test_supplier_market_is_not_described_as_screenshot_payment():
    from application.quote_comparison import render
    t=tools()
    t.conv.quote_comparison.update(comparison_source='SUPPLIER_MARKET',comparison_total_fen=6600,buyer_total_fen=None)
    paragraph=render(lookup(t))
    assert '接口市场参考合计66.00元' in paragraph and '截图自购' not in paragraph


def test_repair_fallback_explains_real_observation_instead_of_generic_reply():
    t=tools();t.tool_results=[('read_quote',t.read_quote())]
    answer=unfinished_reply(t)
    assert '35.00元/张' in answer and '66.00元' in answer and '重新核价' in answer
    assert '没理解清楚' not in answer and '发截图' not in answer


def test_changed_request_and_unverified_image_cannot_reuse_comparison():
    t=tools();t.conv.demand_rev=1
    assert lookup(t) is None
    t.conv.demand_rev=0;t.session.image_context['unverified_image']='new'
    assert lookup(t) is None
    t.session.image_context.pop('unverified_image')
    t.session.image_context['ticket_details']={'pending':True}
    assert lookup(t) is None


def test_legacy_self_buy_has_no_invented_amount():
    t=tools();t.conv.quote_comparison={}
    t.tool_results=[('read_quote',t.read_quote())]
    assert t.read_quote()['comparison']['historical_amounts_unavailable']
    answer=unfinished_reply(t)
    assert '未保存' in answer and '不用重复发送' in answer
    assert '35' not in answer


def test_comparison_persists_through_service_restart():
    t=tools();svc=TicketService.__new__(TicketService);svc.sessions={'c':t.session}
    with patch('application.service.store.save_sessions') as save:
        svc._save()
    saved=deepcopy(save.call_args.args[0]);restored=TicketService.__new__(TicketService);restored.sessions={}
    with patch('application.service.store.load_sessions',return_value=saved):
        restored._load()
    assert restored.sessions['c'].conversation.quote_comparison==t.conv.quote_comparison
