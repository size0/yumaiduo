"""Business facts survive natural-language handling; no transaction authority added."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from application.assistant_reply import assess, failure_reply
from application.request_failure import explain
from application.seat_request import reconcile
from application.ticket_details import parse_seats
from application.ticket_flow import FlowResult
from domain.conversation import Conversation, ConvState
from tests.test_quantity_image_merge import recognition
from tests._check_channel_route import flow, result, MAP_WANDA, MAP_NONE
from tests.test_image_sequence import case


@pytest.fixture
def tool():
    from tests.test_buyer_assistant import BuyerAssistantTests
    f = BuyerAssistantTests(); f.setUp()
    t = f.tools('改成四张'); t.enable_semantic_routing(); t.semantic_intent = 'revise'
    t.reply_metadata = {'intent': 'revise', 'kind': 'explanation', 'claims': [
        {'kind': 'tool_failure', 'tool': 'update_ticket_request', 'status': 'PAID_ORDER_LOCKED'}]}
    t.tool_results = [('update_ticket_request', {'ok': False, 'status': 'PAID_ORDER_LOCKED'})]
    yield t
    f.tearDown()


def test_verified_failure_explanation_is_not_replaced(tool):
    text = '原订单正在处理，不能自动改成四张，需要人工核对。'
    reviewed = assess(tool, text)
    assert reviewed.text == text and not reviewed.fallback


@pytest.mark.parametrize('text', ['已改好价格，现在可以付款了。', '已经出票成功。', '总价已经改为1元。',
    '已帮您把张数更新为四张，座位需求已保存。', '这次已经重新报价了。', '新的座位已经记下了。',
    '张数已改成四张。', '重新报价完成。', '新的数量已调整为四张。'])
def test_failure_reference_never_permits_success_or_payment(tool, text):
    assert assess(tool, text).text != text


def test_failure_claim_cannot_reference_a_different_status(tool):
    tool.reply_metadata['claims'][0]['status'] = 'QUERY_UNKNOWN'
    assert assess(tool, '原订单正在处理，不能自动修改。').reason != 'ACCEPTED'


def test_paid_explanation_is_preserved_only_with_current_order_fact(tool):
    text = '这笔订单已付款，不能自动改成四张，需要人工核对。'
    assert assess(tool, text).text != text
    tool.conv.demand_id, tool.conv.demand_rev = 'current', 1
    tool.context_snapshot = {'orders': {'orders': [dict(demand_id='current', demand_rev=1,
                                                     payment_state='CONFIRMED')]}}
    tool.safe_reply_projection = None
    assert assess(tool, text).text == text


def test_public_failure_code_preserves_business_cause_without_raw_error():
    text = explain('supplement_ticket_details', {'ok': False, 'status': 'BLOCKED',
        'reason_code': 'PAID_ORDER_LOCKED', 'reason': 'https://secret.invalid/?token=PRIVATE'})
    assert '人工核对' in text and '付款' in text and 'PRIVATE' not in text


def test_unknown_error_text_is_never_echoed():
    assert 'PRIVATE' not in explain('supplement_ticket_details', {'ok': False,
        'status': 'BLOCKED', 'reason': 'PRIVATE db traceback'})


@pytest.mark.parametrize('literal', ['6排的12、13、14座', '六排的十二座、十三座、十四座'])
def test_literal_chinese_connector_is_valid_seat_syntax(literal):
    assert [s['seatNo'] for s in parse_seats(literal)] == ['6排12座', '6排13座', '6排14座']


@pytest.mark.parametrize('literal', ['不要6排的12座', '6排的12或13座', '6排的12、12座'])
def test_connector_does_not_weaken_literal_or_duplicate_checks(literal):
    with pytest.raises(ValueError): parse_seats(literal)


def test_field_reconciliation_is_independent_of_cinema_brand():
    conv = Conversation(chat_id='c')
    image = recognition(); image.cinema_name = '寰映影城'; image.brand_name = '其他'
    out = reconcile(conv, image, {'movie': '另一部电影', 'demand_id': '', 'demand_rev': 0}, None, FlowResult())
    assert out is not None and not out.ok
    assert conv.channel is None  # Field merging does not invent a supplier.


def test_legacy_details_migrate_only_from_verified_parent_scope():
    from application.seat_request import scoped_request
    identity = dict(tenant_id='t', shop_id='s', buyer_id='b', chat_id='c')
    conv = Conversation(**identity); conv.demand_id = 'd'; conv.demand_rev = 2
    ctx = dict(identity, demand_id='d', demand_rev=2, url='image',
        created_at=datetime.now(timezone.utc).isoformat())
    request = dict(cinema='万达影城', ticket_count=3, pending=True, source_image='image')
    migrated = scoped_request(request, ctx, conv, identity, 24)
    assert migrated['demand_id'] == 'd' and migrated['demand_rev'] == 2
    assert 'demand_id' not in request
    assert reconcile(conv, recognition(), migrated, None, FlowResult()) is None


@pytest.mark.parametrize('bad', ['identity', 'version', 'expired', 'source', 'missing_parent_version'])
def test_legacy_details_never_inherit_uncertain_scope(bad):
    from application.seat_request import scoped_request
    identity = dict(tenant_id='t', shop_id='s', buyer_id='b', chat_id='c')
    conv = Conversation(**identity); conv.demand_id = 'd'; conv.demand_rev = 2
    ctx = dict(identity, demand_id='d', demand_rev=2, url='image', created_at=datetime.now(timezone.utc).isoformat())
    request = dict(cinema='万达影城', ticket_count=3, pending=True, source_image='image')
    if bad == 'identity': ctx['buyer_id'] = 'other'
    if bad == 'version': ctx['demand_rev'] = 1
    if bad == 'expired': ctx['created_at'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    if bad == 'source': request['source_image'] = 'other'
    if bad == 'missing_parent_version': ctx.pop('demand_rev')
    assert scoped_request(request, ctx, conv, identity, 24) == request


@pytest.mark.parametrize('mapped', [True, False])
def test_price_discrepancy_uses_live_wanda_only_after_mapped_route(mapped):
    f, api, transport = flow(MAP_WANDA if mapped else MAP_NONE)
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    image = result(price_mismatch=True, seat_matched=True, expected_seat_count=1,
                   seats=[{'seatNo': '6排9座'}])
    with patch('application.ticket_flow._second_quote_wait_seconds', return_value=0):
        out = f._after_recognition(conv, image, FlowResult())
    assert bool(any('seats' in url for url in transport.calls)) == mapped
    if mapped:
        assert 'QUOTE_CARD' in out.template_keys
        assert image.price_mismatch  # OCR total stays untrusted.
    else:
        assert not out.ok and not api.preflight_calls
    assert not api.create_calls


def test_price_discrepancy_with_incomplete_seats_is_still_blocked():
    f, api, transport = flow(MAP_WANDA)
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    image = result(price_mismatch=True, seat_matched=False, expected_seat_count=3,
                   seats=[{'seatNo': '6排9座'}])
    out = f._after_recognition(conv, image, FlowResult())
    assert not out.ok and not any('seats' in url for url in transport.calls)
    assert not api.create_calls


def test_cross_image_legacy_details_use_parent_scope_in_real_recovery_path(case):
    from tests.test_image_sequence import anchor, call, seat_image, cinema
    case.conv.demand_id, case.conv.demand_rev = 'current', 2
    previous = anchor(case)
    previous.update(demand_id='current', demand_rev=2)
    previous['ticket_details'] = dict(cinema=cinema()['cinema'], ticket_count=3,
        pending=True, source_image=previous['url'])
    verified = recognition(); verified.cinema_name = cinema()['cinema']
    with patch('application.image_sequence.recover', return_value=verified):
        data = seat_image(); data['marked_count'] = None
        out = call(case, data, previous)
    assert out.ok and case.service.flow._after_recognition.called
    assert case.conv.buyer_ticket_count == 3


def test_price_anomaly_never_caps_to_untrusted_screenshot_amount():
    f, api, _ = flow(MAP_WANDA)
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    image = result(price_mismatch=True, price_all_fen=1, seat_matched=True,
        expected_seat_count=1, seats=[{'seatNo': '6排9座','originalPriceFen':'1'}])
    with patch('application.ticket_flow._second_quote_wait_seconds', return_value=0):
        out = f._after_recognition(conv, image, FlowResult())
    assert out.ok and out.quote.total_price_fen > 1
    assert '0.01' not in '\n'.join(out.messages)
    assert not api.create_calls


def test_price_mismatch_setting_can_continue_to_live_liangpiao_preflight():
    f, api, _ = flow(MAP_NONE)
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    image = result(price_mismatch=True, seat_matched=True, expected_seat_count=2,
                   brand_name='良票影院', cinema_name='良票影城',
                   start_time='2099-10-13T14:20:00+08:00',
                   seats=[{'seatNo': '6排9座'}, {'seatNo': '6排10座'}])
    with patch('application.ticket_flow._price_mismatch_continue_enabled', return_value=True), \
            patch('application.ticket_flow._second_quote_wait_seconds', return_value=0):
        out = f._after_recognition(conv, image, FlowResult())
    assert api.preflight_calls, '成功处理仍必须进入良票实际预校验'
    assert 'QUOTE_CARD' in out.template_keys
    assert image.price_mismatch


def test_recognition_preserves_complete_raw_marked_seat_list():
    from integrations.liangpiao.api import RecognitionResult
    result = RecognitionResult.from_data({
        'finalResults': {
            'matchLevel': 'EXACT', 'expectedSeatCount': 6,
            'seat': [{'seatNo': '9排17座'}, {'seatNo': '9排16座'}, {'seatNo': '9排15座'}],
        },
        'rawResults': {'seat': [
            {'seatNo': '9排17座'}, {'seatNo': '9排16座'}, {'seatNo': '9排15座'},
            {'seatNo': '9排14座'}, {'seatNo': '9排13座'}, {'seatNo': '9排12座'},
        ]},
    })
    assert len(result.seats) == 6

