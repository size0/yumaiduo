"""A new purchase and an old order must never share execution authority."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from domain.conversation import Conversation
from domain.order import OrderStore
from domain.quote import QuoteStore

I = dict(tenant_id='107', shop_id='s', buyer_id='b', chat_id='c')


@pytest.fixture
def tools():
    orders, quotes = OrderStore(), QuoteStore()
    session = Session(chat_id='c', conversation=Conversation(**I))
    session.conversation.demand_id, session.conversation.demand_rev = '', 0
    service = SimpleNamespace(order_store=orders, quote_store=quotes, flow=SimpleNamespace())
    tool = BuyerAssistantTools(service, session, '再买一张', settings.Settings(), lambda _: [])
    tool.enable_semantic_routing()
    yield tool
    orders.close(); quotes.close()


def platform(tools, number='123', status=3, amount=4655):
    row = tools.service.order_store.platforms.discover(I, number, source='SIGNED_ORDER_EVENT')
    row.update(platform_status=status, amount_fen=amount, paid_at='2026-10-17T09:00:00Z',
               sync_state='VERIFIED', checked_at=datetime.now(timezone.utc).isoformat())
    tools.service.order_store.platforms.put(row)
    return row


def begin(tools):
    tools.record_buyer_intent('revise', tools.text)
    return tools.begin_new_purchase(evidence=tools.text, quantity=1, quantity_text='一张')


def test_new_purchase_keeps_old_order_and_records_literal_quantity(tools):
    old = platform(tools)
    old_demand = tools.conv.demand_id
    assert begin(tools)['ok']
    assert tools.conv.demand_id and tools.conv.demand_id != old_demand
    ctx = tools.session.image_context
    assert ctx['awaiting_image_request']['ticket_count'] == 1
    assert ctx['awaiting_image_request']['demand_id'] == tools.conv.demand_id
    assert tools.service.order_store.platforms.get('107', 's', '123') == old
    assert not tools.conv.quote_id


@pytest.mark.parametrize('evidence,quantity,fragment', [('再买一张', 2, '一张'), ('改成两张', 1, '一张'), ('再买一张', 1, '不存在')])
def test_new_purchase_requires_current_literal_quantity(tools, evidence, quantity, fragment):
    tools.record_buyer_intent('revise', tools.text)
    assert not tools.begin_new_purchase(evidence=evidence, quantity=quantity, quantity_text=fragment)['ok']
    assert not tools.conv.demand_id


def test_inquiry_cannot_open_purchase(tools):
    tools.record_buyer_intent('inquire', tools.text)
    assert not tools.begin_new_purchase(evidence=tools.text, quantity=1, quantity_text='一张')['ok']


@pytest.mark.parametrize('creation,marker,accepted', [(None, 'current', False), ('2000-01-01T00:00:00Z', 'current', False), ('future', 'old', False), ('future', 'current', True)])
def test_delayed_cards_require_original_demand_and_platform_creation(tools, creation, marker, accepted):
    from application.purchase_continuation import card_number
    begin(tools)
    row = platform(tools, number='789')
    if creation:
        row['platform_created_at'] = (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat() if creation == 'future' else creation
        tools.service.order_store.platforms.put(row)
    tools.session.pending_order_card = dict(tenantId='107', payload=dict(accountUnb='s',peerUnb='b',chatId='c',orderId='789'),
        _demand_id=tools.conv.demand_id if marker == 'current' else 'old')
    assert bool(card_number(tools)) == accepted


def test_card_attribution_survives_retry_and_rejects_delayed_old_event(tools):
    from application.purchase_continuation import remember_card
    begin(tools)
    old = dict(_demand_id='old', _order_detail=dict(createTime='2000-01-01T00:00:00Z'))
    assert remember_card(tools.session, old)['_demand_id'] == 'old'
    assert '_demand_id' not in remember_card(tools.session, {'_order_detail': old['_order_detail']})
    assert '_demand_id' not in remember_card(tools.session, {})
    current = dict(_order_detail=dict(createTime=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()))
    assert remember_card(tools.session, current)['_demand_id'] == tools.conv.demand_id


def test_old_platform_order_does_not_become_new_purchase_block(tools):
    platform(tools)
    assert begin(tools)['ok']
    from application.purchase_actions import inspect
    with patch('application.platform_orders._read') as read:
        result = inspect(tools)
    assert result['status'] == 'NEW_PURCHASE_PENDING'
    assert not result.get('order_no') and result['payment_blocked']
    assert '本次' in result['guidance'] and '已发货' not in result['guidance']
    read.assert_not_called()


def test_closed_platform_order_is_not_reported_as_missing(tools):
    platform(tools, status=5)
    from application.purchase_actions import inspect
    with patch('application.platform_orders._read', return_value=dict(orderId='123',accountUnb='s',orderStatus=5,payment='4655',payTime='2026-10-17T09:00:00Z')):
        result = inspect(tools)
    assert result['platform_status'] == 5
    assert '关闭' in result['guidance'] or '退款' in result['guidance']
    assert '未关联到平台订单' not in result['guidance']


def test_latest_order_card_selects_new_order_without_losing_old_refund(tools):
    platform(tools, number='111', status=5)
    platform(tools, number='222', status=3)
    tools.session.pending_order_card = dict(tenantId='107', payload=dict(accountUnb='s',peerUnb='b',chatId='c',orderId='222'))
    with patch('application.platform_orders._read', return_value=dict(orderId='222',accountUnb='s',orderStatus=3,payment='4655',payTime='2026-10-17T09:00:00Z')):
        result = tools.read_ticket_progress()
    assert result['order_no'] == '222' and result['platform_status'] == 3
    assert '取票' in result['guidance']
    assert len(tools.service.order_store.platforms.all()) == 2


def test_progress_does_not_claim_an_unverified_seller_image_is_ticket(tools):
    platform(tools)
    tools.official_dialogue = dict(status='AVAILABLE', images=[dict(**I, speaker='seller',url='https://img.alicdn.com/example.png',at=datetime.now(timezone.utc).isoformat(),message_id='pic')])
    with patch('application.platform_orders._read', return_value=dict(orderId='123',accountUnb='s',orderStatus=3,payment='4655',payTime='2026-10-17T09:00:00Z')):
        result = tools.read_ticket_progress()
    assert result['ticket_delivery_verified'] is False
    assert '尚未' in result['guidance'] and '图片' in result['guidance']
    assert '已出票成功' not in result['guidance']


def test_progress_lookup_failure_explains_it_instead_of_unknown_intent(tools):
    platform(tools)
    with patch('application.platform_orders._read', side_effect=TimeoutError()):
        result = tools.read_ticket_progress()
    assert not result['ok'] and '查询' in result['guidance']
    assert '没理解' not in result['guidance']


def test_all_event_send_commands_receive_scoped_history_snapshot():
    from application.plugin_runtime import events_to_commands
    from integrations.yumaiduo.commands import Command
    event = dict(tenantId='107', event='order.paid', payload=dict(accountUnb='s',peerUnb='b',chatId='c'))
    command = Command(command_id='cmd',type='SEND_MESSAGE',event_id='e',session_id='c',text='付款核对中',dedupe_key='k',**I)
    with patch('application.plugin_runtime._scoped_events_to_commands', return_value=[command]), patch(
            'application.plugin_runtime._sent_message_ids', return_value=['sent']) as snapshot:
        result = events_to_commands(event)
    assert result[0].extra['history_message_ids'] == ['sent']
    snapshot.assert_called_once_with(I)


def test_history_snapshot_never_attached_to_other_identity_command():
    from application.plugin_runtime import events_to_commands
    from integrations.yumaiduo.commands import Command
    event = dict(tenantId='107', event='order.paid', payload=dict(accountUnb='s',peerUnb='b',chatId='c'))
    command = Command(command_id='cmd',type='SEND_MESSAGE',event_id='e',session_id='c',text='x',dedupe_key='k',**{**I,'buyer_id':'other'})
    with patch('application.plugin_runtime._scoped_events_to_commands', return_value=[command]), patch(
            'application.plugin_runtime._sent_message_ids') as snapshot:
        events_to_commands(event)
    assert 'history_message_ids' not in command.extra
    snapshot.assert_not_called()


def test_order_notice_context_does_not_become_a_buyer_message():
    from tests.test_official_dialogue import row, read
    result = read([row('current', '什么意思', seconds=0),
                   {**row('notice', '你关闭了订单，钱款已原路退回'), 'messageType': 14}])
    assert result['messages'] == []
    assert result['events'][0]['is_buyer_request'] is False
    assert '原路退回' in result['events'][0]['text']


def test_official_images_are_scoped_and_anchored():
    from tests.test_official_dialogue import row, read
    result = read([row('current', '这两个', seconds=0),
        {**row('image', 'https://img.alicdn.com/old.png'), 'messageType': 2},
        {**row('foreign', 'https://img.alicdn.com/no.png'), 'messageType': 2, 'peerUnb': 'other'},
        {**row('future', 'https://img.alicdn.com/future.png', seconds=5), 'messageType': 2}])
    assert [r['message_id'] for r in result['images']] == ['image']
    assert result['images'][0]['buyer_id'] == 'b'


def test_history_recovers_only_cinema_not_old_show(tools):
    from application.image_history import restore
    stamp = datetime.now(timezone.utc)
    tools.session.image_context = dict(I, url='current',created_at=stamp.isoformat(), recognition_facts={},
                                      inquiry_started_at=(stamp-timedelta(minutes=3)).isoformat())
    tools.official_dialogue = dict(status='AVAILABLE', images=[dict(I, url='first', speaker='buyer',
        at=(stamp-timedelta(minutes=2)).isoformat(),message_id='old-image')])
    with patch('application.image_history.read_image', return_value=dict(kind='schedule',cinema_complete=True,
            cinema='东莞南城蜂汇店',city='东莞',film='旧影片',showtime='2020-01-01')):
        assert restore(tools)
    saved = tools.session.image_context['cinema_evidence']
    assert saved['source_message_id'] == 'old-image'
    assert 'film' not in saved and 'showtime' not in saved


@pytest.mark.parametrize('change', ['identity','future','new_purchase','unavailable','completed'])
def test_history_will_not_recover_foreign_or_new_demand_images(tools, change):
    from application.image_history import restore
    stamp = datetime.now(timezone.utc)
    ctx = dict(I,url='current',created_at=stamp.isoformat(),recognition_facts={})
    image = dict(I,url='first',speaker='buyer',at=(stamp-timedelta(minutes=2)).isoformat(),message_id='old')
    dialogue = dict(status='AVAILABLE',images=[image])
    if change == 'identity':image['buyer_id']='foreign'
    if change == 'future':image['at']=(stamp+timedelta(minutes=2)).isoformat()
    if change == 'new_purchase':ctx['purchase_started_at']=(stamp-timedelta(minutes=1)).isoformat()
    if change == 'unavailable':dialogue['status']='UNAVAILABLE'
    if change == 'completed':ctx['verified']=True
    tools.session.image_context=ctx;tools.official_dialogue=dialogue
    with patch('application.image_history.read_image') as read:
        assert not restore(tools)
    read.assert_not_called()


def test_resume_of_failed_old_image_uses_same_cross_image_recovery(tools):
    from application.ticket_flow import FlowResult
    from application.image_sequence import recover_sequence
    tools.session.image_context=dict(I,url='old-seat-image',created_at=datetime.now(timezone.utc).isoformat(),recognition_facts={})
    tools.image_snapshot=dict(tools.session.image_context)
    tools.service.flow.on_screenshot=Mock(return_value=FlowResult(ok=False))
    with patch('application.image_history.restore',return_value=True) as restore, patch(
            'application.image_sequence.recover_sequence',return_value=FlowResult(ok=True,messages=['真实核价'])) as recovery:
        result=tools._resume_quote(ticket_count=2)
    assert result['ok']
    restore.assert_called_once_with(tools)
    assert recovery.call_args.args[3]=='old-seat-image'


def test_cinema_confirmation_does_not_lose_the_new_quantity(tools):
    tools.session.image_context=dict(I,url='old-seat-image',created_at=datetime.now(timezone.utc).isoformat(),recognition_facts={})
    tools.image_snapshot=dict(tools.session.image_context)
    def restore(tool):
        tool.session.image_context['cinema_candidate']=dict(cinema='东莞南城蜂汇店',source_message_id='first')
    with patch('application.image_history.restore',side_effect=restore):
        assert tools._resume_quote(ticket_count=2)['ok']
    assert tools.session.image_context['ticket_count']==2
    assert tools.city_attempted is False


def test_explicit_image_recovery_tool_reuses_saved_quantity(tools):
    tools.session.image_context['url']='current-image'
    tools.session.image_context['ticket_count']=2
    with patch.object(tools,'_resume_quote',return_value={'ok':True}) as resume:
        assert not tools.recover_image_context()['ok']
        resume.assert_not_called()
        tools.semantic_intent='revise'
        assert tools.recover_image_context()['ok']
        resume.assert_called_once_with(ticket_count=2)


def test_new_purchase_cannot_read_old_paid_business_order(tools):
    from domain.order import Order
    order=Order(order_id='old',quote_id='old-q',**I,provider='WANDA',provider_order_id='111',
        state='PAID',payment_state='CONFIRMED',amount_fen=9900)
    tools.service.order_store.put(order)
    assert begin(tools)['ok']
    from application.purchase_actions import inspect
    with patch('application.purchase_actions.platform_order') as read:
        result=inspect(tools)
    assert result['status']=='NEW_PURCHASE_PENDING'
    read.assert_not_called()


def test_discovery_time_does_not_turn_backfilled_old_order_into_new_purchase(tools):
    assert begin(tools)['ok']
    platform(tools,number='111',status=2)
    from application.purchase_actions import inspect
    with patch('application.platform_orders._read') as read:
        assert inspect(tools)['status']=='NEW_PURCHASE_PENDING'
    read.assert_not_called()


def test_legacy_image_is_only_candidate_until_buyer_confirms(tools):
    from application.image_history import restore,confirm
    stamp=datetime.now(timezone.utc)
    tools.session.image_context=dict(I,url='current',created_at=stamp.isoformat(),recognition_facts={})
    tools.official_dialogue=dict(status='AVAILABLE',images=[dict(I,url='first',speaker='buyer',
        at=(stamp-timedelta(minutes=2)).isoformat(),message_id='first-image')])
    with patch('application.image_history.read_image',return_value=dict(kind='schedule',cinema_complete=True,cinema='东莞南城蜂汇店',city='东莞')):
        assert not restore(tools)
    assert 'cinema_evidence' not in tools.session.image_context
    assert tools.session.image_context['cinema_candidate']['source_message_id']=='first-image'
    tools.text='是这家';tools.semantic_intent='revise'
    with patch.object(tools,'_resume_quote',return_value={'ok':True}) as resume:
        assert confirm(tools,tools.text,'first-image')['ok']
    assert tools.session.image_context['cinema_evidence']['cinema']=='东莞南城蜂汇店'
    resume.assert_called_once()


def test_history_revision_mismatch_is_never_relabelled(tools):
    from application.image_history import restore
    stamp=datetime.now(timezone.utc)
    tools.session.image_context=dict(I,url='current',created_at=stamp.isoformat(),recognition_facts={},demand_id='',demand_rev=9)
    tools.official_dialogue=dict(status='AVAILABLE',images=[dict(I,url='first',speaker='buyer',at=(stamp-timedelta(minutes=2)).isoformat(),message_id='first-image')])
    with patch('application.image_history.read_image') as read:
        assert not restore(tools)
    read.assert_not_called()
