from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from domain.conversation import Conversation,ConvState
from domain.order import OrderStore
from domain.quote import QuoteRecord,QuoteStore


@pytest.fixture
def setup():
    ident=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
    orders,quotes=OrderStore(),QuoteStore()
    conv=Conversation(**ident,state=ConvState.QUOTED)
    conv.demand_id,conv.demand_rev='d',1
    conv.quote_id='q'
    q=QuoteRecord(quote_id='q',**ident,demand_id='d',demand_rev=1)
    quotes.create(q)
    session=Session(chat_id='c',conversation=conv)
    session.image_context=dict(**ident,url='https://example.test/a.jpg',verified=True,
        demand_id='d',demand_rev=1,created_at=datetime.now(timezone.utc).isoformat(),
        recognition_facts=dict(match_level='EXACT',city_name='银川',final_results={'provinceName':'宁夏回族自治区'}))
    service=SimpleNamespace(order_store=orders,quote_store=quotes,flow=SimpleNamespace(on_screenshot=Mock()))
    def tools(text='宁夏银川'):
        tool=BuyerAssistantTools(service,session,text,settings.Settings(),lambda _:[])
        tool.enable_semantic_routing();tool.semantic_intent='revise'
        return tool
    yield tools,session,quotes,service
    orders.close();quotes.close()


@pytest.mark.parametrize('text,city',[('宁夏银川','银川'),('宁夏银川','宁夏银川'),('银川市','银川市')])
def test_same_city_preserves_quote_and_only_acknowledges(setup,text,city):
    make,session,quotes,service=setup
    before=quotes.get('q').to_dict()
    tools=make(text)
    result=tools.execute('supplement_city',{'city':city})
    assert result['ok'] and result['status']=='UNCHANGED'
    assert tools.flow_result.template_keys==['CITY_CONFIRMED']
    assert tools.flow_result.messages==['收到，是银川，刚才的报价已保留。']
    service.flow.on_screenshot.assert_not_called()
    assert quotes.get('q').to_dict()==before


@pytest.mark.parametrize('change', ['city','version','identity','unverified','pending','expired','mixed_text'])
def test_changed_or_unverified_facts_use_existing_reverification_path(setup,change):
    make,session,quotes,service=setup
    text='宁夏银川'
    city='银川'
    ctx=session.image_context
    if change=='city': text=city='太原'
    elif change=='version':ctx['demand_rev']=0
    elif change=='identity':ctx['chat_id']='other'
    elif change=='unverified':ctx['unverified_image']=True
    elif change=='pending':ctx['ticket_details']={'pending':True}
    elif change=='mixed_text':text='银川，换8排'
    elif change=='expired':
        q=quotes.get('q');q.status='SUPERSEDED';quotes.put(q)
    tools=make(text)
    tools._resume_quote=Mock(return_value={'ok':False,'status':'EXISTING_RECHECK'})
    assert tools.supplement_city(city)['status']=='EXISTING_RECHECK'
    tools._resume_quote.assert_called_once_with(city=city)
