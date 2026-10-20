"""Quote templates describe quotes and do not invent reservations or missing counts."""
from copy import deepcopy
import templates


def test_default_quote_preserves_owner_copy_without_repeating_known_count():
    values=dict(city='长治',cinema='影院',movie='影片',hall='影厅',show_time='2026-10-18 15:50',
        seats='10排12座，10排13座，10排14座',count='3',origin_unit='54.90',origin_total='164.70',
        unit_price='48.00',total_price='144.00',cinema_addr='地址',confirm_cmd='正确')
    for key in ('QUOTE_CARD','QUOTE_CARD_MULTI'):
        text='\n'.join(templates.render_messages(key,values))
        assert '已抢到' in text and '限时2分钟秒杀' in text
        assert '需要几张' not in text and '张数：3张' in text
        assert '没有识别到完整' not in text


def test_deferred_recognition_notice_does_not_claim_work_is_still_running():
    assert '正在识别' not in templates.text_for('RECOGNIZING_TIP')


def test_secondary_quote_preserves_owner_copy():
    assert '留着' in templates.text_for('SECOND_QUOTE')


def test_saved_template_migration_is_scoped_idempotent_and_preserves_blank_overrides():
    from application.template_upgrade import upgrade
    data=dict(assistant={'persona':'保留原人设'},templates={
        'QUOTE_CARD':dict(text='自定义开头\n恭喜：已抢到特价票限时2分钟秒杀~\n座位是否在图片中标记？需要几张票？[ggx1z82tg84]',channel_text={'wanda':''}),
        'RECOGNIZING_TIP':dict(text='系统正在识别，请核对确认后再拍',channel_text={'wanda':''}),
        'SECOND_QUOTE':'位置我这边先给您留着，拍下就能安排出票~',
        'TICKET_CODE':dict(text='保持原来的票码文案')})
    before=deepcopy(data);out=upgrade(data)
    assert data==before and out==upgrade(out)
    assert out['assistant']==data['assistant']
    assert out['templates']['TICKET_CODE']==data['templates']['TICKET_CODE']
    assert out['templates']['QUOTE_CARD']['text'].startswith('自定义开头')
    assert '需要几张' not in out['templates']['QUOTE_CARD']['text']
    assert out['templates']['RECOGNIZING_TIP']['channel_text']['wanda']==''
    assert out['templates']['SECOND_QUOTE']==data['templates']['SECOND_QUOTE']
    assert '已抢到特价票限时2分钟秒杀' in out['templates']['QUOTE_CARD']['text']


def test_plain_saved_templates_migrate_only_known_legacy_fragments():
    from application.template_upgrade import upgrade
    data={'QUOTE_CARD_MULTI':'恭喜：已抢到特价票限时2分钟秒杀~[kdpdte8g6cg]',
          'SECOND_QUOTE':{'text':None},'untouched':'原样'}
    out=upgrade(data)
    assert out['QUOTE_CARD_MULTI']=='恭喜：已抢到特价票限时2分钟秒杀~' and out['untouched']=='原样'
    assert out['SECOND_QUOTE']['text'] is None


def test_agent_question_does_not_prefetch_faq_by_a_word_in_the_message():
    from unittest.mock import patch
    from tests.test_buyer_assistant import BuyerAssistantTests
    from application.conversation_ai import ConversationAI
    from tests.test_buyer_assistant import ScriptedModel
    from integrations.model.client import ModelReply
    fixture=BuyerAssistantTests();fixture.setUp()
    try:
        fixture.conv.channel='wanda';fixture.cfg.reply.intelligent_qa_enabled=True
        fixture.svc.ai=ConversationAI(ScriptedModel([ModelReply(content='{"answer":"请问你指的是哪种颜色？","intent":"other","kind":"clarification","claims":[]}')]))
        with patch('application.service._faq_lookup') as lookup:
            fixture.svc._answer_with_ai(fixture.session,'灰色是什么意思',fixture.cfg)
        lookup.assert_not_called()
    finally:
        fixture.tearDown()


def test_missing_image_is_an_observation_that_allows_targeted_clarification():
    from tests.test_buyer_assistant import BuyerAssistantTests
    fixture=BuyerAssistantTests();fixture.setUp()
    try:
        fixture.session.image_context={}
        tools=fixture.tools('15 15');tools.enable_semantic_routing();tools.semantic_intent='revise'
        result=tools.execute('recover_image_context',{})
        assert result['status']=='NEEDS_INFORMATION' and result['missing_fields']
        assert tools.request_failure_reply()==''
        assert not fixture.session.image_context and tools.flow_result is None
    finally:
        fixture.tearDown()


def test_successful_faq_observation_does_not_require_an_extra_intent_label():
    from tests.test_buyer_assistant import BuyerAssistantTests
    fixture=BuyerAssistantTests();fixture.setUp()
    try:
        tools=fixture.tools('会员座可以买吗');tools.enable_semantic_routing()
        tools.tool_results=[('search_faq',{'ok':True,'answer':'可以申请购买，余座需核对。'})]
        tools.reply_metadata={}
        assert tools.review_reply('可以申请购买，请圈出位置，具体余座还需核对。').reason=='ACCEPTED'
        assert tools.review_reply('已经帮你改好价格了，请付款。').reason!='ACCEPTED'
        tools.reply_metadata={'invalid_response_metadata':True}
        assert tools.review_reply('可以申请购买。').reason!='ACCEPTED'
    finally:
        fixture.tearDown()


def test_migration_preserves_owner_night_copy_schedule_and_money():
    from application.template_upgrade import upgrade
    data={'night_recognize':{'start':'00:00:00','end':'08:00',
        'message':'当前时间段，人工可能休息，可能会出票失败，如果一直没有出票，将会在第二天上午出票哈~'},
        'fish_reprice':{'auto_reprice':False}}
    out=upgrade(data)
    assert out['night_recognize']==data['night_recognize']
    assert out['night_recognize']['start']=='00:00:00' and out['fish_reprice']==data['fish_reprice']


def test_shared_ticketing_copy_preserves_owner_design():
    from application.template_upgrade import upgrade
    data={'TICKETING':{'channel_text':{'wanda':'稍等我这边出票哈，大概1-10分钟出票','other':123}}}
    out=upgrade(data)
    assert out['TICKETING']==data['TICKETING']
    assert out['TICKETING']['channel_text']['other']==123
    assert upgrade({'templates':[]})=={'templates':[]}
