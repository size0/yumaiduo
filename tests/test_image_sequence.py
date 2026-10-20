"""Partial screenshots share evidence, never old transaction authority."""
import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import settings
from application.service import Session
from application.ticket_flow import FlowResult
from domain.conversation import Conversation, ConvState
from domain.order import OrderStore
from integrations.liangpiao.api import RecognitionResult


@pytest.fixture
def case():
    identity = dict(tenant_id='107', shop_id='2313315754', buyer_id='2208609528506', chat_id='c')
    conv = Conversation(**identity)
    session = Session(chat_id='c', conversation=conv)
    session.image_context = {**identity, 'url': 'first', 'created_at': datetime.now(timezone.utc).isoformat()}
    model = Mock()
    orders = OrderStore()
    flow = SimpleNamespace(wanda_bridge=Mock(), _after_recognition=Mock(return_value=FlowResult(ok=True, messages=['核价结果'], template_keys=['WANDA_UNIT_PRICE'])))
    service = SimpleNamespace(flow=flow, order_store=orders, quote_store=None, _ai_for_config=lambda _: SimpleNamespace(client=model))
    fixture = SimpleNamespace(session=session, conv=conv, identity=identity, model=model, service=service, cfg=settings.Settings())
    yield fixture
    orders.close()


def cinema():
    return dict(kind='schedule', cinema='东莞万达影城南城蜂汇店', city='东莞', cinema_complete=True,
                film='生化危机：爆发夜', hall='', showtime='', seats=[], marked_count=None)


def seat_image():
    return dict(kind='seat', cinema='', city='', cinema_complete=False, film='生化危机：爆发夜',
                hall='杜比影院（儿童需购票）', showtime='2026-10-18 17:05:00', seats=[], marked_count=2)


def test_marked_positions_ask_only_for_missing_seat_numbers(case):
    previous = anchor(case)
    case.service.flow._after_recognition.return_value.messages = ['优惠价：36.21元/张\n需要几张票？如需指定位置，可以在图上标记后发我。']
    with patch('application.image_sequence.recover', return_value=RecognitionResult(match_level='EXACT', cinema_name=cinema()['cinema'])):
        out=call(case,seat_image(),previous)
    assert '需要几张票' not in '\n'.join(out.messages)
    assert '具体座号' in '\n'.join(out.messages)


def call(case, data, previous=None):
    from application.image_sequence import recover_sequence
    case.model.chat.return_value = SimpleNamespace(content=json.dumps(data, ensure_ascii=False))
    return recover_sequence(case.service, case.session, previous or {}, case.session.image_context['url'], FlowResult(ok=False), case.cfg)


def anchor(case):
    out = call(case, cinema())
    assert out.ok and '东莞' in out.messages[0]
    previous = deepcopy(case.session.image_context)
    case.session.image_context = {**case.identity, 'url': 'second', 'created_at': datetime.now(timezone.utc).isoformat()}
    return previous


def test_captures_cinema_without_preexisting_wanda_channel(case):
    assert case.conv.channel is None
    out = call(case, cinema())
    assert 'IMAGE_CINEMA_CONTEXT' in out.template_keys
    assert case.session.image_context['cinema_evidence']['cinema'] == cinema()['cinema']
    case.service.flow._after_recognition.assert_not_called()


def test_second_image_combines_only_cinema_and_rechecks_route(case):
    previous = anchor(case)
    verified = RecognitionResult(match_level='EXACT', cinema_name=cinema()['cinema'])
    with patch('application.image_sequence.recover', return_value=verified) as recover:
        out = call(case, seat_image(), previous)
    raw = recover.call_args.args[1].raw_results
    assert raw['cinema'] == cinema()['cinema'] and raw['city'] == '东莞'
    assert raw['showtime'] == '2026-10-18 17:05:00' and raw['seat'] == []
    assert recover.call_args.args[1].expected_seat_count is None
    assert case.service.flow._after_recognition.call_args.args[1] is verified
    assert out.ok and '具体座号' in out.messages[-1]
    assert case.session.image_context['marked_positions']['count'] == 2
    assert not case.session.image_context['marked_positions']['confirmed']


@pytest.mark.parametrize('field,value', [('tenant_id','x'),('shop_id','x'),('buyer_id','x'),('chat_id','x')])
def test_no_cross_identity_inheritance(case, field, value):
    previous = anchor(case)
    previous['cinema_evidence'][field] = value
    with patch('application.image_sequence.recover') as recover:
        out = call(case, seat_image(), previous)
    recover.assert_not_called()
    assert '影院' in out.messages[0]


@pytest.mark.parametrize('field,value', [('demand_id','other'),('demand_rev',1)])
def test_no_cross_demand_inheritance(case, field, value):
    previous = anchor(case)
    previous['cinema_evidence'][field] = value
    with patch('application.image_sequence.recover') as recover:
        call(case, seat_image(), previous)
    recover.assert_not_called()


def test_expired_or_completed_anchor_not_used(case):
    previous = anchor(case)
    previous['cinema_evidence']['created_at'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    with patch('application.image_sequence.recover') as recover:
        call(case, seat_image(), previous)
    recover.assert_not_called()
    previous['cinema_evidence']['created_at'] = datetime.now(timezone.utc).isoformat()
    previous['verified'] = True
    with patch('application.image_sequence.recover') as recover:
        call(case, seat_image(), previous)
    recover.assert_not_called()


def test_film_misread_as_cinema_does_not_replace_previous_venue(case):
    previous = anchor(case)
    data = seat_image(); data.update(cinema=data['film'], cinema_complete=True)
    with patch('application.image_sequence.recover', return_value=RecognitionResult(match_level='EXACT')) as recover:
        call(case, data, previous)
    assert recover.call_args.args[1].raw_results['cinema'] == cinema()['cinema']


def test_current_different_cinema_never_inherits_old_city(case):
    previous = anchor(case)
    data = seat_image(); data.update(cinema='北京某影城', cinema_complete=True)
    with patch('application.image_sequence.recover') as recover:
        out = call(case, data, previous)
    recover.assert_not_called()
    assert '城市' in out.messages[0]


def test_truncated_new_cinema_cannot_borrow_old_cinema(case):
    previous = anchor(case)
    data = seat_image(); data.update(cinema='北京某影...', cinema_complete=False)
    with patch('application.image_sequence.recover') as recover:
        out = call(case, data, previous)
    recover.assert_not_called()
    assert '完整影院' in out.messages[0]


@pytest.mark.parametrize('template', ['TEXT_IMAGE_CONFIRM', 'POSITION_SELECTION_REQUIRED', 'WANDA_QUOTE_FAILED', 'WANDA_MATCH_PENDING'])
def test_business_rejection_is_never_overridden(case, template):
    from application.image_sequence import recover_sequence
    original = FlowResult(ok=False, template_keys=[template])
    assert recover_sequence(case.service, case.session, {}, 'first', original, case.cfg) is original
    case.model.chat.assert_not_called()


@pytest.mark.parametrize('facts', [{'price_mismatch': True}, {'seat_confirm_required': True},
                                  {'seats': [{'seatNo': '6排1座'}]}, {'match_level': 'EXACT'}])
def test_original_verification_facts_are_not_discarded(case, facts):
    case.conv.supplement_recognition = facts
    assert not call(case, cinema()).ok
    case.model.chat.assert_not_called()


def test_existing_position_request_still_blocks(case):
    previous = anchor(case)
    previous['ticket_details'] = dict(position_preference='中间会员座', pending=True)
    verified = RecognitionResult(match_level='EXACT')
    case.conv.channel = 'wanda'
    with patch('application.image_sequence.recover', return_value=verified):
        out = call(case, seat_image(), previous)
    assert 'POSITION_SELECTION_REQUIRED' in out.template_keys
    case.service.flow._after_recognition.assert_not_called()


@pytest.mark.parametrize('field', ['tenant_id','shop_id','buyer_id','chat_id'])
def test_foreign_text_seats_never_used_with_current_complete_cinema(case, field):
    previous = anchor(case)
    previous[field] = 'other'
    previous['awaiting_image_request'] = dict(movie='旧片', date='2026-10-18', time='17:05',
        seats=[{'seatNo': '6排1座'}], demand_id='', demand_rev=0)
    current = seat_image(); current.update(cinema=cinema()['cinema'], city='东莞', cinema_complete=True)
    verified = RecognitionResult(match_level='EXACT')
    with patch('application.image_sequence.recover', return_value=verified), patch(
            'application.seat_request.reconcile') as reconcile:
        call(case, current, previous)
    reconcile.assert_not_called()
    assert not verified.seats


def test_old_version_text_request_is_not_silently_dropped(case):
    previous = anchor(case)
    previous['ticket_details'] = dict(ticket_count=3, demand_id='old', demand_rev=1)
    current = seat_image(); current.update(cinema=cinema()['cinema'], city='东莞', cinema_complete=True)
    with patch('application.image_sequence.recover', return_value=RecognitionResult(match_level='EXACT')):
        out = call(case, current, previous)
    assert not out.ok and '本次需求' in out.messages[0]
    case.service.flow._after_recognition.assert_not_called()


def test_incomplete_new_show_does_not_inherit_old_film_or_time(case):
    previous = anchor(case)
    data = seat_image(); data['showtime'] = ''
    with patch('application.image_sequence.recover') as recover:
        out = call(case, data, previous)
    recover.assert_not_called()
    assert '日期' in out.messages[0]


def test_failed_mapping_does_not_quote_or_claim_no_show(case):
    previous = anchor(case)
    with patch('application.image_sequence.recover', side_effect=ValueError('route unavailable')):
        out = call(case, seat_image(), previous)
    case.service.flow._after_recognition.assert_not_called()
    assert not out.ok and '暂未核实' in out.messages[0]
    assert '猫眼' not in out.messages[0]


def test_new_cinema_failure_never_restores_previous_quote(case):
    from application.image_context import preserve_image_result
    previous = {**case.identity, 'verified': True, 'url': 'old', 'summary': '旧场次'}
    old_conv = deepcopy(case.conv.__dict__)
    old_conv['quote_id'] = 'old-quote'
    data = seat_image(); data.update(cinema=cinema()['cinema'], city='东莞', cinema_complete=True)
    with patch('application.image_sequence.recover', side_effect=ValueError('unavailable')):
        out = call(case, data, previous)
    preserve_image_result(case.session, previous, old_conv, 'new', out)
    assert not case.conv.quote_id
    assert case.session.image_context.get('cinema_evidence')
    assert not case.session.image_context['verified']


@pytest.mark.parametrize('state', ['PAID', 'REPRICE_PENDING'])
def test_active_order_blocks_fallback(case, state):
    case.service.order_store = SimpleNamespace(by_identity=lambda _: [SimpleNamespace(state=state, payment_state='CONFIRMED', payload={})])
    assert not call(case, cinema()).ok
    case.model.chat.assert_not_called()


def test_manual_or_successful_flow_not_reprocessed(case):
    from application.image_sequence import recover_sequence
    case.conv.state = ConvState.MANUAL
    original = FlowResult(ok=False)
    assert recover_sequence(case.service, case.session, {}, 'first', original, case.cfg) is original
    case.conv.state = ConvState.IDLE
    original = FlowResult(ok=True)
    assert recover_sequence(case.service, case.session, {}, 'first', original, case.cfg) is original
    case.model.chat.assert_not_called()


@pytest.mark.parametrize('value', ['not json', '{}', '{"kind":"other"}'])
def test_invalid_vision_does_not_change_facts(case, value):
    from application.image_sequence import recover_sequence
    case.model.chat.return_value = SimpleNamespace(content=value)
    old = deepcopy(case.session.image_context)
    original = FlowResult(ok=False)
    assert recover_sequence(case.service, case.session, {}, 'first', original, case.cfg) is original
    assert case.session.image_context == old


def test_real_service_two_images_reach_original_unit_quote_and_no_order(case):
    from application.service import TicketService
    from domain.quote import QuoteStore
    from integrations.liangpiao.client import LiangPiaoError
    from integrations.wanda.bridge import CinemaRoute
    from tests._check_channel_route import flow, result, rules, MAP_NONE
    flow_object, api, _ = flow(MAP_NONE)
    quotes = QuoteStore()
    try:
        flow_object.quote_store = quotes
        api.ch7_recognize = Mock(side_effect=[LiangPiaoError(700006, 'not seat'), RecognitionResult(
            raw_results=dict(cinema='生化危机：爆发夜', film='生化危机：爆发夜'))])
        service = TicketService(api, rules(), tel='', ai=SimpleNamespace(enabled=False), persist=False,
                                quote_store=quotes, order_store=case.service.order_store)
        service.flow = flow_object
        service.sessions['c'] = case.session
        service._ai_for_config = case.service._ai_for_config
        case.model.chat.side_effect = [SimpleNamespace(content=json.dumps(cinema())),
                                       SimpleNamespace(content=json.dumps(seat_image()))]
        # Match/seat map are supplied by the existing channel integration fixture;
        # the service, state transitions, costing, rule engine and quote store run.
        verified = result(expected_seat_count=None, price_all_fen=0)
        verified.cinema_id = verified.show_id = verified.movie_id = None
        verified.wanda_verified_route = CinemaRoute(mapped=True, wanda_city_id='80', wanda_cinema_id='379')
        verified.wanda_verified_show_id = '103155648'
        with patch('application.image_sequence.recover', return_value=verified), patch(
                'application.ticket_flow._second_quote_wait_seconds', return_value=0), patch(
                'domain.quote.models._now', return_value=datetime(2026, 10, 13, tzinfo=timezone.utc)):
            first = service.on_message('c', image_url='first', config=case.cfg)
            second = service.on_message('c', image_url='second', config=case.cfg)
        assert 'IMAGE_CINEMA_CONTEXT' in first.template_keys
        assert 'WANDA_UNIT_PRICE' in second.template_keys, second
        quote = quotes.get(case.conv.quote_id)
        assert quote.ticket_count == 0 and case.conv.quote_kind == 'UNIT_PRICE'
        assert not api.preflight_calls and not api.create_calls
        assert not case.service.order_store.connection.execute('SELECT 1 FROM orders').fetchall()
        assert case.session.image_context['marked_positions']['count'] == 2
    finally:
        quotes.close()


def test_real_service_price_conflict_cannot_be_recovered(case):
    from application.service import TicketService
    from tests._check_channel_route import flow, rules, MAP_NONE
    flow_object, api, _ = flow(MAP_NONE)
    api.ch7_recognize = Mock(return_value=RecognitionResult(match_level='NONE', price_mismatch=True))
    service = TicketService(api, rules(), tel='', ai=SimpleNamespace(enabled=False), persist=False,
                            order_store=case.service.order_store)
    service.flow = flow_object
    service.sessions['c'] = case.session
    service._ai_for_config = case.service._ai_for_config
    out = service.on_message('c', image_url='price-conflict', config=case.cfg)
    assert not out.ok and case.conv.supplement_recognition['price_mismatch']
    case.model.chat.assert_not_called()
    assert not api.preflight_calls and not api.create_calls
