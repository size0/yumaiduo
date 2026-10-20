from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from application.ticket_details import respond
from application.assistant_reply import assess


@pytest.fixture
def tool():
    from tests.test_buyer_assistant import BuyerAssistantTests
    f=BuyerAssistantTests();f.setUp()
    t=f.tools('今天');t.enable_semantic_routing()
    yield t
    f.tearDown()


def test_semantic_details_return_missing_fields_not_a_fixed_reply(tool):
    tool.session.image_context=dict(tool.identity,awaiting_image_request=dict(
        demand_id='',demand_rev=0,cinema='长治万达',movie='生化危机',date='2026-10-19',time='15:50',ticket_count=3))
    result=respond(tool,'硬编码的完整买家回复')
    assert tool.flow_result is None
    assert result['missing_fields']==['image']
    assert result['known_fields']['ticket_count']==3
    assert '硬编码' not in str(result)


def test_failure_explanation_does_not_require_an_extra_failure_label(tool):
    tool.semantic_intent='revise'
    tool.tool_results=[('update_ticket_request',dict(ok=False,status='PAID_ORDER_LOCKED'))]
    tool.reply_metadata=dict(intent='revise',kind='explanation',claims=[])
    text='原订单正在处理，不能自动修改，需店家核对。'
    assert assess(tool,text).text==text
    assert assess(tool,'张数已改成四张。').text!='张数已改成四张。'


def test_missing_field_contract_does_not_repeat_saved_movie_and_quantity(tool):
    tool.session.image_context=dict(tool.identity,awaiting_image_request=dict(
        demand_id='',demand_rev=0,cinema='长治万达',movie='生化危机',ticket_count=2))
    result=respond(tool,'旧模板要求重复所有字段')
    assert set(result['missing_fields'])=={'date','time','image'}
    assert 'movie' not in result['missing_fields'] and 'ticket_count' not in result['missing_fields']


def test_non_agent_fallback_remains_available(tool):
    tool.semantic_mode=False
    result=respond(tool,'请提供观影日期。')
    assert tool.flow_result.messages==['请提供观影日期。']
    assert result['ok']


def test_typed_clarification_is_not_reinterpreted_as_a_background_job(tool):
    tool.tool_results=[('search_faq',dict(ok=True,status='SHOP_POLICY_AVAILABLE'))]
    tool.reply_metadata=dict(intent='other',kind='clarification',claims=[])
    text='您想看哪家影院、哪部片、几号几点的？发我截图我帮您核一下有没有4D场次。'
    decision=assess(tool,text)
    assert decision.text==text and not decision.feedback


@pytest.mark.parametrize('kind',['explanation','acknowledgement'])
def test_unstarted_waiting_promise_is_still_rejected(tool,kind):
    tool.reply_metadata=dict(intent='other',kind=kind,claims=[])
    decision=assess(tool,'稍等，我查一下，有结果告诉您。')
    assert decision.feedback


def test_clarification_kind_does_not_authorize_transaction_success(tool):
    tool.reply_metadata=dict(intent='other',kind='clarification',claims=[])
    text='已经改价成功，现在可以付款了，您还有什么问题？'
    decision=assess(tool,text)
    assert decision.text!=text and decision.reason=='UNVERIFIED_TRANSACTION_CLAIM'


def test_invalid_clarification_metadata_does_not_bypass_waiting_check(tool):
    tool.reply_metadata=dict(intent='other',kind='clarification',claims=[],invalid_response_metadata=True)
    assert assess(tool,'稍等，我查一下。').feedback


@pytest.mark.parametrize('text',[
    '票价38元一张，请发截图我帮您核一下。',
    '已经出票成功了，请发截图我帮您核一下。',
    '已经下单成功了，请发截图我帮您核一下。',
    '已为你转人工，请发截图我帮您核一下。',
])
def test_clarification_still_checks_unsupported_business_claims(tool,text):
    tool.reply_metadata=dict(intent='other',kind='clarification',claims=[])
    assert assess(tool,text).text!=text


def test_clarification_with_claims_still_checks_waiting_promise(tool):
    from application.assistant_reply import response_feedback
    tool.reply_metadata=dict(intent='other',kind='clarification',claims=[dict(type='tool_failure')])
    assert response_feedback(tool,'稍等，我查一下。')


@pytest.mark.parametrize('count',[0,2])
def test_show_match_observations_preserve_actual_result(tool,count):
    tool.session.image_context=dict(tool.identity,url='archive-image',ticket_details=dict(
        demand_id='',demand_rev=0,awaiting='show_verification',movie='奥德赛'))
    observed=dict(code='SHOW_NOT_UNIQUE',match_count=count,
                  candidates=[dict(showId=str(i),hallName='IMAX厅') for i in range(count)])
    result=respond(tool,'不应代替Agent生成完整回复',observation=observed)
    assert result['business_observation']==observed
    assert 'show_verification' in result['missing_fields']
    assert '不应代替Agent' not in str(result)


def test_cinema_confirmation_preserves_the_actual_candidate(tool):
    tool.session.image_context=dict(tool.identity,url='image',ticket_details=dict(
        demand_id='',demand_rev=0,awaiting='cinema_confirmation'))
    observed=dict(code='TRUNCATED_CINEMA_CONFIRMATION',cinema_name='寰映海上世界店',city_name='厦门')
    result=respond(tool,'旧模板',observation=observed)
    assert result['business_observation']['cinema_name']=='寰映海上世界店'
    assert 'cinema_confirmation' in result['missing_fields']


@pytest.mark.parametrize('status,kept',[('PAID_ORDER_LOCKED',True),('QUERY_UNKNOWN',True),('INVALID_FIELD_EVIDENCE',False)])
def test_local_failure_handoff_uses_status_not_chinese_wording(tool,status,kept):
    tool.semantic_intent='revise'
    result=respond(tool,'这是本地真实业务原因，与通用模板不同。',False,status=status)
    tool.tool_results=[('supplement_ticket_details',result)]
    tool.reply_metadata={}
    reply=assess(tool,'[转人工]我来处理。').text
    assert reply.startswith('[转人工]') is kept
    assert '真实业务原因' in reply



def test_new_purchase_returns_facts_instead_of_a_terminal_template(tool):
    tool.text='另外买两张'
    tool.semantic_intent='revise'
    result=tool.execute('begin_new_purchase',dict(evidence=tool.text,quantity=2,quantity_text='两张'))
    assert tool.flow_result is None
    assert result['known_fields']['ticket_count']==2
    assert 'image' in result['missing_fields']
    assert result['business_observation']['code']=='NEW_PURCHASE'


@pytest.mark.parametrize('keys,human',[([],False),(['TICKET_DETAILS'],True),(['QUOTE_CARD'],False)])
def test_semantic_clarification_conversion_preserves_other_results(tool,keys,human):
    from application.ticket_flow import FlowResult
    original=FlowResult(ok=True,state=tool.conv.state,template_keys=keys,need_human=human)
    def handler(**args):
        tool.flow_result=original
        return {'ok':True}
    tool.begin_new_purchase=handler
    tool.execute('begin_new_purchase',{})
    assert tool.flow_result is original


@pytest.mark.parametrize('name,subject',[('read_quote','报价'),('read_order','订单'),('supplement_ticket_details','购票信息')])
def test_missing_history_fallback_names_the_actual_subject(name,subject):
    from application.request_failure import explain
    text=explain(name,dict(ok=False,status='HISTORICAL_EVIDENCE_UNAVAILABLE'))
    assert subject in text
    if name!='read_order':assert '订单卡片' not in text and '订单证据' not in text


@pytest.mark.parametrize('new_inquiry',[False,True])
def test_missing_fields_do_not_ask_for_verified_current_cinema_or_borrow_old_one(tool,new_inquiry):
    tool.conv.demand_inquiry=new_inquiry
    tool.session.image_context=dict(tool.identity,url='current',demand_id='',demand_rev=0,recognition_facts=dict(
        match_level='EXACT',cinema_id='1',cinema_name='已核实影院'),ticket_details=dict(
        demand_id='',demand_rev=0,movie='奥德赛',date='2026-10-19',time='15:50',ticket_count=2))
    result=respond(tool,'旧模板')
    assert ('cinema' in result['missing_fields']) is new_inquiry
    if not new_inquiry:assert result['known_fields']['cinema']=='已核实影院'


@pytest.mark.parametrize('version',[{},dict(demand_id='old',demand_rev=1)])
def test_old_or_unversioned_image_cannot_supply_current_cinema(tool,version):
    tool.conv.demand_id='current';tool.conv.demand_rev=2
    tool.session.image_context=dict(tool.identity,url='old',recognition_facts=dict(
        match_level='EXACT',cinema_id='1',cinema_name='旧影院'),**version)
    assert 'cinema' in respond(tool,'旧模板')['missing_fields']


@pytest.mark.parametrize('ok,keys',[(True,['QUOTE_CARD']),(False,['RECOG_FAIL'])])
def test_verified_quote_image_stamps_actual_demand_but_failure_does_not(tool,ok,keys):
    from application.image_context import preserve_image_result
    from application.ticket_flow import FlowResult
    tool.conv.demand_id='current';tool.conv.demand_rev=2
    tool.session.image_context=dict(tool.identity,url='current',demand_id='old',demand_rev=1)
    preserve_image_result(tool.session,{}, {},'current',FlowResult(ok=ok,template_keys=keys))
    assert tool.session.image_context['demand_id']==('current' if ok else 'old')
    assert tool.session.image_context['demand_rev']==(2 if ok else 1)



def test_supplement_quote_stamps_parent_image_version_after_actual_success(request):
    from tests.test_seat_correction import SeatCorrectionTests
    from application.buyer_assistant import BuyerAssistantTools
    from application.ticket_flow import FlowResult
    fixture=SeatCorrectionTests()
    request.addfinalizer(fixture.doCleanups)
    f=fixture.fixture()
    def after(conv,result,out):
        conv.demand_id='current';conv.demand_rev=2;conv.quote_id='confirmed-local-quote'
        return FlowResult(ok=True,state=conv.state,template_keys=['QUOTE_CARD'])
    f.service.flow._after_recognition=after
    t=BuyerAssistantTools(f.service,f.session,'8排13',f.cfg,None)
    t.enable_semantic_routing();t.record_buyer_intent('revise',t.text)
    result=t.execute('supplement_ticket_details',dict(evidence=t.text,seats='8排13'))
    assert result['ok']
    assert f.session.image_context['demand_id']=='current'
    assert f.session.image_context['demand_rev']==2



def test_literal_position_preference_can_use_a_current_buyer_source(tool):
    from datetime import datetime,timezone
    from application.buyer_evidence import sources
    tool.text='是';tool.semantic_intent='revise';tool.conv.channel='wanda'
    tool.session.history=[dict(role='user',content='7排最中间',created_at=datetime.now(timezone.utc).isoformat())]
    tool.session.image_context['ticket_details']=dict(demand_id='',demand_rev=0,ticket_count=2)
    source=sources(tool)[0];assert source['can_reference']
    schema=next(s for s in tool.schemas if s['function']['name']=='supplement_ticket_details')
    assert 'position_preference' in schema['function']['parameters']['properties']['source_refs']['properties']
    result=tool.execute('supplement_ticket_details',dict(evidence=tool.text,position_preference='7排最中间',source_refs=dict(position_preference=source['message_ref'])))
    assert result['ok'],result
    assert result['known_fields']['position_preference']=='7排最中间'
    assert 'seats' in result['missing_fields']


@pytest.mark.parametrize('name',['get_purchase_guidance','read_ticket_progress','search_faq'])
def test_failure_reference_is_valid_for_real_observation_not_just_required_tool_list(tool,name):
    tool.tool_results=[(name,dict(ok=False,status='HISTORICAL_EVIDENCE_UNAVAILABLE'))]
    tool.reply_metadata=dict(intent='other',kind='explanation',claims=[dict(kind='tool_failure',tool=name,status='HISTORICAL_EVIDENCE_UNAVAILABLE')])
    text='当前所需记录未取得，无法确认，请店家核对。'
    assert assess(tool,text).text==text


@pytest.mark.parametrize('recovered',[False,True])
def test_failure_reference_cannot_quote_wrong_status_or_recovered_failure(tool,recovered):
    tool.tool_results=[('search_faq',dict(ok=False,status='POLICY_NOT_FOUND'))]
    if recovered:tool.tool_results.append(('search_faq',dict(ok=True,status='COMPLETED')))
    tool.reply_metadata=dict(intent='other',kind='explanation',claims=[dict(kind='tool_failure',tool='search_faq',status='POLICY_NOT_FOUND' if recovered else 'QUERY_UNKNOWN')])
    text='查询未取得结果。'
    assert assess(tool,text).feedback


def test_invalid_final_response_does_not_blame_buyer_understanding(tool):
    from application.assistant_reply import unfinished_reply
    tool.session.image_context={}
    text=unfinished_reply(tool)
    assert '没理解' not in text
    assert '核对' in text


@pytest.mark.parametrize('stale',[False,True])
def test_position_reference_still_requires_recent_literal_buyer_evidence(tool,stale):
    from datetime import datetime,timezone,timedelta
    from application.buyer_evidence import sources
    tool.text='是';tool.semantic_intent='revise';tool.conv.channel='wanda'
    stamp=datetime.now(timezone.utc)-timedelta(hours=2 if stale else 0)
    tool.session.history=[dict(role='user',content='7排最中间',created_at=stamp.isoformat())]
    ref=sources(tool)[0]['message_ref']
    result=tool.execute('supplement_ticket_details',dict(evidence=tool.text,position_preference='7排最中间' if stale else '8排最中间',source_refs=dict(position_preference=ref)))
    assert not result['ok'] and result['status']=='INVALID_SOURCE_REFERENCE'
    assert not tool.session.image_context.get('ticket_details',{}).get('position_preference')



def test_ticket_progress_missing_history_explains_progress_not_price():
    from application.request_failure import explain
    text=explain('read_ticket_progress',dict(ok=False,status='HISTORICAL_EVIDENCE_UNAVAILABLE'))
    assert '出票进度' in text and '核价' not in text and '位置信息' not in text


def test_true_optional_failure_reference_does_not_authorize_reprice_success(tool):
    tool.tool_results=[('search_faq',dict(ok=False,status='POLICY_NOT_FOUND'))]
    tool.reply_metadata=dict(intent='other',kind='explanation',claims=[dict(kind='tool_failure',tool='search_faq',status='POLICY_NOT_FOUND')])
    text='已经改价成功，可以付款了。'
    assert assess(tool,text).text!=text
    assert tool.flow_result is None
