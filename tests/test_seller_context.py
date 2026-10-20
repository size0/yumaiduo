from datetime import datetime, timezone, timedelta
import json
import sqlite3
import pytest
from application.assistant_reply import assess, _unsupported_amount
from domain.quote import QuoteRecord


@pytest.fixture
def tool():
    from tests.test_buyer_assistant import BuyerAssistantTests
    f=BuyerAssistantTests();f.setUp()
    f.conv.demand_id,f.conv.demand_rev,f.conv.quote_id='d',1,'q'
    created=datetime.now(timezone.utc)-timedelta(minutes=1)
    f.quotes.create(QuoteRecord(quote_id='q',demand_id='d',demand_rev=1,**f.ident,
        provider='WANDA',unit_cost_fen=12700,unit_sell_price_fen=12700,ticket_count=2,
        total_cost_fen=25400,total_sell_price_fen=25400,created_at=created,
        metadata={'quote_kind':'ORDER_TOTAL','price_mode':'FIXED'}))
    t=f.tools('W+是50一张');t.enable_semantic_routing()
    t.official_dialogue=dict(status='AVAILABLE',identity=f.ident,messages=[dict(
        message_id='manual88',speaker='seller',origin='EXTERNAL_SELLER',text='88一张',
        at=(created+timedelta(seconds=10)).isoformat())])
    yield t
    f.tearDown()


def test_external_reply_pauses_old_auto_quote_without_creating_trade(tool):
    from application.seller_context import collect
    ctx=collect(tool)
    assert ctx['active'] and ctx['messages'][0]['message_id']=='manual88'
    q=tool.service.quote_store.get('q')
    assert q.status=='SUPERSEDED' and q.total_sell_price_fen==25400
    assert not tool.read_quote()['quote']['tradable']
    assert not tool.service.order_store.by_identity(tool.identity)


def test_attributed_manual_amount_allowed_but_not_automatic_price(tool):
    from application.seller_context import collect
    collect(tool)
    tool.reply_metadata={'intent':'other','kind':'explanation','claims':[dict(
        kind='seller_message',message_id='manual88',demand_id='d',demand_rev=1)]}
    text='店家刚才人工回复88元一张，请以店家核对为准。'
    assert not _unsupported_amount(tool,text)
    assert assess(tool,text).text==text
    assert assess(tool,'现在售价127元一张。').text!='现在售价127元一张。'
    assert assess(tool,'店家已经改价88元，请现在付款。').text!='店家已经改价88元，请现在付款。'


@pytest.mark.parametrize('bad',['auto','old','identity','version'])
def test_other_sources_do_not_invalidate_current_quote(tool,bad):
    from application.seller_context import collect
    if bad=='auto':tool.official_dialogue['messages'][0]['origin']='PLUGIN_AUTOMATED'
    if bad=='old':tool.official_dialogue['messages'][0]['at']='2020-01-01T00:00:00+00:00'
    if bad=='identity':tool.official_dialogue['identity']={**tool.identity,'buyer_id':'other'}
    if bad=='version':tool.conv.demand_rev=2
    assert not collect(tool)['active']
    assert tool.service.quote_store.get('q').status=='READY'


def test_persisted_pause_survives_history_failure_but_not_new_quote(tool):
    from application.seller_context import collect
    assert collect(tool)['active']
    tool.official_dialogue={'status':'UNAVAILABLE','messages':[]}
    assert collect(tool)['active']
    tool.conv.demand_rev=2
    assert not collect(tool)['active']


def test_origin_checks_receipt_identity_and_never_creates_db(tmp_path):
    from application.seller_context import annotate
    identity=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c')
    messages=[dict(message_id='bot',speaker='seller'),dict(message_id='manual',speaker='seller')]
    missing=tmp_path/'absent.sqlite3'
    assert annotate(messages,identity,missing)[0]['origin']=='UNKNOWN'
    assert not missing.exists()
    db=tmp_path/'commands.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE yumaiduo_commands(session_id,type,status,payload)')
        c.execute('INSERT INTO yumaiduo_commands VALUES (?,?,?,?)',('c','SEND_MESSAGE','SENT',json.dumps(dict(identity,sent_message_id='bot'))))
    result=annotate(messages,identity,db)
    assert [r['origin'] for r in result]==['PLUGIN_AUTOMATED','EXTERNAL_SELLER']


@pytest.mark.parametrize('source,answer',[('7排18座','店家说18元一张。'),
    ('16:10开场','店家说10元一张。'),('订单号123456','店家说12345元一张。'),
    ('88一张','店家说总价88元。'),('88一张','店家说88元一张，总价88元。'),
    ('7排18座','店家说18一张。')])
def test_seller_reference_cannot_turn_non_money_or_unit_price_into_new_price(tool,source,answer):
    from application.seller_context import collect
    tool.official_dialogue['messages'][0]['text']=source
    collect(tool)
    tool.reply_metadata={'intent':'other','kind':'explanation','claims':[dict(
        kind='seller_message',message_id='manual88',demand_id='d',demand_rev=1)]}
    assert assess(tool,answer).text!=answer


def test_recalled_or_filtered_manual_price_does_not_remain_referencable(tool):
    from application.seller_context import collect,referenced_messages
    collect(tool)
    tool.official_dialogue['messages']=[]
    result=collect(tool)
    assert result['active'] and result['messages']==[]
    tool.reply_metadata={'claims':[dict(kind='seller_message',message_id='manual88',demand_id='d',demand_rev=1)]}
    assert not referenced_messages(tool)


def test_unavailable_history_never_extends_amount_evidence_past_memory_window(tool):
    from application.seller_context import collect,referenced_messages
    collect(tool)
    tool.seller_context['messages'][0]['at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    tool.reply_metadata={'claims':[dict(kind='seller_message',message_id='manual88',demand_id='d',demand_rev=1)]}
    assert not referenced_messages(tool)
