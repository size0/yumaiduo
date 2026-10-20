from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import settings
from application.buyer_assistant import BuyerAssistantTools
from application.service import Session
from application.ticket_details import parse_seats
from application.ticket_flow import FlowResult
from application.wanda_cost import resolve_cost, WandaCostError
from domain.conversation import Conversation
from domain.order import OrderStore
from domain.quote import QuoteStore
from tests import _check_channel_route as fixture
from tests._check_wanda_cost import client, SEATS


@pytest.mark.parametrize('text', ['6排11座 6排12座', '6排11座，6排12座',
    '六排十一座以及六排十二座', '6排11座 和 6排12座', '6排11座\n6排12座'])
def test_explicit_repeated_row_labels_are_format_conversion_not_new_intent(text):
    assert [s['seatNo'] for s in parse_seats(text)] == ['6排11座', '6排12座']


@pytest.mark.parametrize('text', ['6排11座 6排11座', '不要6排11座 6排12座',
    '6排11座 或者6排12座', '6排11座6排12座', '6排11座 还没决定'])
def test_negation_alternatives_unseparated_pairs_and_duplicates_remain_rejected(text):
    with pytest.raises(ValueError): parse_seats(text)


def cost(body, count=1):
    return resolve_cost(client(seats=body), city_id='80', cinema_id='1', phone='test',
                        showtime_resolver=lambda _: 'show', seat_count=count)


def test_unselected_member_area_missing_activity_ignores_other_cheaper_area_prices():
    body = deepcopy(SEATS)
    for area in body['data']['realtimeSeats']['area']: area['wPlusActivity'] = None
    assert cost(body, 2).unit_cost_fen == 5790
    with pytest.raises(WandaCostError) as error: cost(body, 3)
    assert error.value.code == 'SEAT_UNAVAILABLE'


@pytest.mark.parametrize('activity', [{}, {'price': 0}])
def test_broken_present_activity_is_not_treated_as_missing(activity):
    body = deepcopy(SEATS)
    body['data']['realtimeSeats']['area'][0]['wPlusActivity'] = activity
    with pytest.raises(WandaCostError) as error: cost(body)
    assert error.value.code == 'NO_MEMBER_PRICE'


def test_multiple_member_area_prices_require_specific_seats_instead_of_cheapest():
    body = deepcopy(SEATS)
    area = deepcopy(body['data']['realtimeSeats']['area'][0])
    area['areaId'] = 'different'
    area['wPlusActivity']['price'] = 4000
    for seat in area['seat']: seat['name'] = '9排' + seat['name'].split('排')[1]
    body['data']['realtimeSeats']['area'].append(area)
    with pytest.raises(WandaCostError) as error: cost(body)
    assert error.value.code == 'MEMBER_AREA_PRICE_AMBIGUOUS'


def test_same_member_cost_different_original_prices_cannot_pool_availability():
    body = deepcopy(SEATS)
    area = deepcopy(body['data']['realtimeSeats']['area'][0])
    area['areaId'] = 'different-original'
    area['areaPrice']['salesPrice'] = 10000
    for seat in area['seat']: seat['name'] = '9排' + seat['name'].split('排')[1]
    body['data']['realtimeSeats']['area'].append(area)
    with pytest.raises(WandaCostError) as error: cost(body, 3)
    assert error.value.code == 'MEMBER_AREA_PRICE_AMBIGUOUS'


@pytest.mark.parametrize('changed', [{'chat_id':'another-chat'}, {'demand_rev':2},
    {'unverified_image':True}])
def test_exact_failed_price_image_does_not_bypass_identity_or_version(changed):
    from application.wanda_position import supplement
    conv = SimpleNamespace(demand_id='current',demand_rev=1,demand_inquiry=False)
    ctx = dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c',demand_id='current',
        demand_rev=1,quote_failure='WANDA_QUOTE_FAILED',recognition_facts={'match_level':'EXACT'})
    ctx.update(changed)
    tool = SimpleNamespace(session=SimpleNamespace(image_context=ctx),conv=conv,
        identity=dict(tenant_id='t',shop_id='s',buyer_id='b',chat_id='c'))
    result = supplement(tool,'6排11座 6排12座')
    assert not result['ok'] and result['status'] == 'STALE_REFERENCE'


def test_two_links_image_only_and_text_continuation_reach_readonly_wanda_costing():
    body = deepcopy(fixture.SEATS)
    for area in body['data']['realtimeSeats']['area']: area['wPlusActivity'] = None
    members = body['data']['realtimeSeats']['area'][0]
    members['areaPrice']['salesPrice'] = 5390
    members['seat'][0]['name'], members['seat'][1]['name'] = '6排11座', '6排12座'
    orders, quotes = OrderStore(), QuoteStore()
    try:
        f, api, transport = fixture.flow(fixture.MAP_WANDA)
        f.quote_store = quotes
        sessions = []
        with patch.dict(fixture.SEATS, body):
            for chat in ('link-one', 'link-two'):
                conv = Conversation(tenant_id='107', shop_id='s', buyer_id='b', chat_id=chat, channel='wanda')
                image = fixture.result(expected_seat_count=None, price_all_fen=0,
                    raw_results={'city': None, 'cinema': '影院截断…', 'film':'生化危机：爆发夜'})
                # No buyer text: the image alone must obtain the member-area unit price.
                first = f._after_recognition(conv, image, FlowResult(channel='wanda'))
                assert first.ok and 'WANDA_UNIT_PRICE' in first.template_keys
                assert quotes.get(conv.quote_id).unit_cost_fen == 5390
                session = Session(chat_id=chat, conversation=conv)
                # Also cover recovery of sessions which failed before this release.
                session.image_context = dict(tenant_id='107',shop_id='s',buyer_id='b',chat_id=chat,
                    demand_id=conv.demand_id,demand_rev=conv.demand_rev,url='https://image.test/same.jpg',
                    created_at=datetime.now(timezone.utc).isoformat(),verified=False,
                    quote_failure='WANDA_QUOTE_FAILED',recognition_facts=asdict(image))
                tools = BuyerAssistantTools(SimpleNamespace(flow=f,order_store=orders,quote_store=quotes),
                    session,'6排11座 6排12座',settings.Settings(),lambda _: [])
                tools.enable_semantic_routing();tools.semantic_intent='revise'
                result = tools.supplement_ticket_details(evidence=tools.text,seats=tools.text)
                assert result['ok'], result
                q = quotes.get(conv.quote_id)
                assert q.ticket_count == 2 and q.unit_cost_fen == 5390
                assert q.selected_seats == ['6排11座','6排12座'] and q.chat_id == chat
                assert session.image_context['verified'] and not session.image_context.get('quote_failure')
                sessions.append(session)
        assert sessions[0].conversation.quote_id != sessions[1].conversation.quote_id
        assert not api.create_calls and not api.preflight_calls
    finally:
        orders.close();quotes.close()
