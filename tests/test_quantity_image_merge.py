from copy import deepcopy

from application.seat_request import reconcile
from application.ticket_flow import FlowResult
from domain.conversation import Conversation
from integrations.liangpiao.api import RecognitionResult


def recognition(count=3, **kwargs):
    return RecognitionResult(match_level='EXACT', cinema_name='万达影城（贵阳数博万达IMAX店）',
        movie_name='生化危机：爆发夜', start_time='2026-10-17T19:05:00+08:00',
        cinema_truncated=False, seats=[{'seatNo': f'8排{11+i}座'} for i in range(count)],
        seat_matched=True, **kwargs)


def quantity(count=3, **extra):
    return dict(ticket_count=count, demand_id='', demand_rev=0, **extra)


def test_quantity_supplement_keeps_three_recognized_seats():
    conv = Conversation(chat_id='c', channel='wanda')
    image = recognition()
    seats = deepcopy(image.seats)
    assert reconcile(conv, image, quantity(evidence='买三张票'), None, FlowResult()) is None
    assert image.seats == seats and image.seat_matched
    assert image.expected_seat_count == conv.buyer_ticket_count == 3


def test_quantity_supplement_does_not_clear_price_discrepancy():
    conv = Conversation(chat_id='c', channel='wanda')
    image = recognition(price_mismatch=True, price_all_fen=13623,
                        seat_confirm_required=True, seat_confirm_reasons=['PRICE_MISMATCH'])
    assert reconcile(conv, image, quantity(), None, FlowResult()) is None
    assert image.price_mismatch and image.seat_confirm_required
    assert image.price_all_fen == 13623 and image.seat_confirm_reasons == ['PRICE_MISMATCH']


def test_quantity_mismatch_is_a_specific_question():
    conv = Conversation(chat_id='c', channel='wanda')
    out = reconcile(conv, recognition(2), quantity(), None, FlowResult())
    assert not out.ok
    assert '3张' in out.messages[0] and '2个' in out.messages[0]


def test_quantity_does_not_override_explicit_movie_or_position_constraints():
    for constraint in ({'movie': '其他影片'}, {'position_preference': '第一排'}):
        conv = Conversation(chat_id='c', channel='wanda')
        out = reconcile(conv, recognition(), quantity(**constraint), None, FlowResult())
        assert out is not None and not out.ok


def test_quantity_does_not_resolve_ambiguous_cinema():
    conv = Conversation(chat_id='c', channel='wanda')
    image = recognition()
    image.match_level = 'CANDIDATES'
    out = reconcile(conv, image, quantity(), None, FlowResult())
    assert out is not None and not out.ok


def test_quantity_without_numbered_seats_does_not_invent_seats():
    conv = Conversation(chat_id='c', channel='wanda')
    image = recognition(0)
    image.seat_matched = False
    assert reconcile(conv, image, quantity(), None, FlowResult()) is None
    assert image.seats == [] and not image.seat_matched
    assert image.expected_seat_count == 3


def test_old_demand_or_revision_cannot_supply_quantity_to_current_image():
    for stale in ({'demand_id': 'old', 'demand_rev': 2}, {'demand_id': 'current', 'demand_rev': 1}):
        conv = Conversation(chat_id='c', channel='wanda')
        conv.demand_id, conv.demand_rev = 'current', 2
        out = reconcile(conv, recognition(), {'ticket_count': 3, **stale}, None, FlowResult())
        assert out is not None and not out.ok
        assert '旧需求' in out.messages[0]


def test_current_demand_version_accepts_quantity():
    conv = Conversation(chat_id='c', channel='wanda')
    conv.demand_id, conv.demand_rev = 'current', 2
    assert reconcile(conv, recognition(), {'ticket_count': 3, 'demand_id': 'current',
                     'demand_rev': 2}, None, FlowResult()) is None


def test_unversioned_legacy_quantity_does_not_bind_new_image():
    conv = Conversation(chat_id='c', channel='wanda')
    conv.demand_id, conv.demand_rev = 'current', 2
    out = reconcile(conv, recognition(), {'ticket_count': 3}, None, FlowResult())
    assert out is not None and not out.ok
