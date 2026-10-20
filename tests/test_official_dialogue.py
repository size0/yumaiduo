import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from application.official_dialogue import read_dialogue


NOW = datetime.now(timezone.utc)
IDENT = dict(tenant_id='107', shop_id='s', buyer_id='b', chat_id='c')


def row(mid, content, *, seller=False, seconds=-10, **extra):
    return dict(messageId=mid, content=content, chatId='c', accountUnb='s', peerUnb='b',
                direction='outbound' if seller else 'inbound', senderUnb='s' if seller else 'b',
                messageType=1, sentAt=(NOW + timedelta(seconds=seconds)).isoformat(), **extra)


def read(rows, status=200):
    client = SimpleNamespace(_post=lambda path, payload: (status, json.dumps(rows)))
    session = SimpleNamespace(conversation=SimpleNamespace(**IDENT), current_message_id='current')
    config = SimpleNamespace(memory_hours=24, memory_messages=50)
    return read_dialogue(session, config, client=client, now=NOW)


def test_seller_question_precedes_buyer_answer_and_future_is_excluded():
    result = read([row('future', '新问题', seconds=5), row('current', '好', seconds=0),
                   row('manual', '中间三张，137元可以吗？', seller=True)])
    assert result['status'] == 'AVAILABLE'
    assert [r['message_id'] for r in result['messages']] == ['manual']
    assert result['messages'][0]['speaker'] == 'seller'
    assert '137' in result['messages'][0]['text']


def test_recalled_and_system_messages_are_not_context():
    result = read([row('current', '好', seconds=0), row('bad', '错误报价', seller=True, recalledAt='yes'),
                   {**row('notice', '营销小助手答复了你'), 'messageType': 14},
                   row('kept', '人工消息', seller=True)])
    assert [r['message_id'] for r in result['messages']] == ['kept']


def test_other_shop_buyer_chat_and_sender_are_rejected():
    rows = [row('current', '好', seconds=0)]
    for key in ('accountUnb', 'peerUnb', 'chatId', 'senderUnb'):
        rows.append({**row(key, '别人的价格'), key: 'other'})
    assert read(rows)['messages'] == []


def test_no_anchor_never_uses_later_conversation():
    assert read([row('other', '后来的消息')])['status'] == 'ANCHOR_UNAVAILABLE'


def test_failed_read_does_not_fabricate_history():
    result = read([], status=502)
    assert result['status'] == 'UNAVAILABLE' and result['messages'] == []


def test_expired_duplicates_and_inconsistent_direction_are_filtered():
    result = read([row('current', '好', seconds=0), row('old', '旧报价', seconds=-90000),
                   row('a', '报价', seller=True), row('a', '报价', seller=True),
                   {**row('wrong', '假卖家'), 'direction': 'outbound'}])
    assert [r['message_id'] for r in result['messages']] == ['a']


def test_equal_anchor_timestamp_and_recalled_duplicate_are_excluded():
    result = read([row('current', '好', seconds=0), row('later', '之后改四张', seconds=0),
                   row('recalled', '已撤回的价格', seller=True),
                   row('recalled', '已撤回的价格', seller=True, recalledAt='yes')])
    assert result['messages'] == []


def test_real_ai_request_contains_seller_dialogue_without_recalled_local_reply():
    from unittest.mock import patch
    from application.conversation_ai import ConversationAI
    from integrations.model.client import ModelReply
    from tests.test_buyer_assistant import BuyerAssistantTests, ScriptedModel
    fixture = BuyerAssistantTests()
    fixture.setUp()
    try:
        fixture.svc.persist = True
        fixture.session.history = [{'role': 'assistant', 'content': '已撤回的旧错误报价',
                                    'created_at': NOW.isoformat()}]
        model = ScriptedModel([ModelReply(content=json.dumps(dict(
            answer='收到，你是在回答店铺上一句询问。', intent='other', kind='explanation', claims=[])))])
        fixture.svc.ai = ConversationAI(model)
        official = dict(status='AVAILABLE', source='official_im', messages=[dict(
            message_id='seller1', speaker='seller', text='中间三个位置可以吗？', at=NOW.isoformat())])
        with patch('application.official_dialogue.read_dialogue', return_value=official) as reader, \
             patch.object(fixture.svc, '_save'), patch.object(fixture.svc, '_record_reply_metrics'), \
             patch.object(fixture.svc, '_blacklist_gate', return_value=None):
            fixture.svc.on_message('c', '好', config=fixture.cfg, remote_message_id='current')
        reader.assert_called_once()
        assert fixture.session.current_message_id == 'current'
        messages, kwargs = model.seen[0]
        assert '中间三个位置可以吗' in kwargs['system']
        assert '历史报价不能充当有效报价' in kwargs['system']
        assert '已撤回的旧错误报价' not in json.dumps(messages, ensure_ascii=False)
        assert fixture.session.last_ai_trace['official_dialogue_count'] == 1
        assert not fixture.orders.by_identity(fixture.ident)
    finally:
        fixture.tearDown()
