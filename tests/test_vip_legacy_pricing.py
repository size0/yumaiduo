import json,sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from application.wanda_cost import resolve_cost,WandaCostError
from integrations.wanda.bridge import SeatMapResult,MemberArea


def test_live_legacy_setting_formula_and_floor(tmp_path):
    from application.vip_pricing import load_rule,quote_vip
    p=tmp_path/'settings.sqlite3'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE business_settings(key,value,revision)')
        c.execute('INSERT INTO business_settings VALUES (?,?,?)',('vip_discount_bps','8900',30))
    rule=load_rule(p)
    cost=SimpleNamespace(source='WANDA_VIP_FIXED_COST',unit_cost_fen=5500,seat_count=2,
        original_unit_fen=12700,original_total_fen=25400,total_cost_fen=11000)
    q=quote_vip(cost,rule)
    assert q.unit_price_fen==11303 and q.total_price_fen==22606
    assert q.cost_total_fen==11000 and q.pricing_rule_revision=='v5-settings-30'
    cost.original_unit_fen=5000;cost.original_total_fen=10000
    assert quote_vip(cost,rule).unit_price_fen==5500


def test_missing_vip_configuration_cannot_silently_use_regular_price(tmp_path):
    from application.vip_pricing import load_rule
    with pytest.raises(ValueError):load_rule(tmp_path/'missing.sqlite3')
    assert not (tmp_path/'missing.sqlite3').exists()


@pytest.mark.parametrize('available',[True,False])
def test_vip_cost_uses_fixed_policy_only_after_seat_check(available):
    from unittest.mock import Mock
    b=Mock();b.wanda_lookup.return_value=SimpleNamespace(use_wanda=True,wanda_cinema_id='w')
    b.seat_map.return_value=SeatMapResult(showtime_id='show',phone='p',areas=[MemberArea(
        area_id='1',area_name='普通区',sales_price_fen=12700,member_price_fen=100,
        seats=[{'name':'2排4座','status':1 if available else 2}])])
    args=dict(city_id='c',cinema_id='x',phone='p',showtime_resolver=lambda _:'show',
        seat_names=['2排4座'],fixed_cost_resolver=lambda total: 5500)
    if not available:
        with pytest.raises(WandaCostError):resolve_cost(b,**args)
    else:
        cost=resolve_cost(b,**args)
        assert cost.unit_cost_fen==5500 and cost.original_unit_fen==12700
        assert cost.source=='WANDA_RULE_FIXED_COST'


@pytest.mark.parametrize('config_available',[True,False])
def test_vip_full_quote_uses_configured_hall_rule_and_never_legacy_database(config_available):
    from copy import deepcopy
    from unittest.mock import Mock
    from tests import _check_channel_route as fixture
    from application.hall_pricing_migration import migrate_rules
    from domain.quote_rules import QuoteRule,QuoteRuleEngine
    from application.ticket_flow import FlowResult
    from domain.conversation import Conversation,ConvState
    shows=deepcopy(fixture.SHOWS);seats=deepcopy(fixture.SEATS)
    shows['data']['showtimeFilmInf'][0]['showtimeFilmDateInf'][0]['showtimesInf']['showtimeList'][0]['hallName']='VIP厅'
    seats['data']['realtimeSeats']['area'][0]['areaPrice']['salesPrice']=12700
    f,api,_=fixture.flow(fixture.MAP_WANDA)
    rows=migrate_rules([QuoteRule(name='普通万达',channel='WANDA',markup_yuan=1,capped=False)],keyword='VIP',discount_bps=8900,cost_fen=5500,floor_fen=5500)
    if not config_available: rows[-1].enabled=False
    f.rules=QuoteRuleEngine(rows,revision='hall-rules-1')
    image=fixture.result(hall_name='VIP厅',price_all_fen=25400,expected_seat_count=2,seat_matched=True,
        seats=[{'seatNo':'6排9座'},{'seatNo':'6排10座'}])
    conv=Conversation(chat_id='c',state=ConvState.RECOGNIZING)
    with patch.dict(fixture.SHOWS,shows),patch.dict(fixture.SEATS,seats),patch(
            'application.vip_pricing.load_rule',side_effect=AssertionError('runtime must not read V5 pricing')):
        out=f._after_recognition(conv,image,FlowResult())
    if config_available:
        assert out.ok and out.quote.unit_price_fen==11303 and out.quote.total_price_fen==22606
        assert out.quote.cost_basis=='WANDA_RULE_FIXED_COST'
        assert '113.03' in '\n'.join(out.messages)
    else:
        assert not out.ok and 'WANDA_QUOTE_FAILED' in out.template_keys
    assert not api.create_calls


def test_disabled_only_hall_rule_cannot_fallback_to_member_cost():
    from copy import deepcopy
    from tests import _check_channel_route as fixture
    from application.hall_pricing_migration import migrate_rules
    from application.ticket_flow import FlowResult
    from domain.conversation import Conversation, ConvState
    from domain.quote_rules import QuoteRuleEngine
    rows = migrate_rules([], keyword='VIP', discount_bps=8900, cost_fen=5500, floor_fen=5500)
    rows[0].enabled = False
    shows = deepcopy(fixture.SHOWS)
    shows['data']['showtimeFilmInf'][0]['showtimeFilmDateInf'][0]['showtimesInf']['showtimeList'][0]['hallName'] = 'VIP厅'
    f, api, _ = fixture.flow(fixture.MAP_WANDA)
    f.rules = QuoteRuleEngine(rows)
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    image = fixture.result(hall_name='VIP厅', expected_seat_count=2,
                           seats=[{'seatNo':'6排9座'},{'seatNo':'6排10座'}])
    with patch.dict(fixture.SHOWS, shows):
        out = f._after_recognition(conv, image, FlowResult())
    assert not out.ok and out.quote is None
    assert 'WANDA_QUOTE_FAILED' in out.template_keys
    assert not api.create_calls


def test_unselected_hall_reselects_price_range_after_official_price_is_known():
    from tests import _check_channel_route as fixture
    from application.ticket_flow import FlowResult
    from domain.conversation import Conversation, ConvState
    from domain.quote_rules import QuoteRule, QuoteRuleEngine
    f, api, _ = fixture.flow(fixture.MAP_WANDA)
    f.rules = QuoteRuleEngine([
        QuoteRule(name='高价区间', channel='WANDA', weight=30, excluded_hall_keywords=['VIP'],
                  price_left_yuan=100, price_right_yuan=200, markup_yuan=20, capped=False),
        QuoteRule(name='低价区间', channel='WANDA', weight=20, excluded_hall_keywords=['VIP'],
                  price_left_yuan=0, price_right_yuan=100, markup_yuan=5, capped=False)])
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    out = f._after_recognition(conv, fixture.result(expected_seat_count=1, seats=[]), FlowResult())
    assert out.ok and out.quote.rule_name == '低价区间'
    assert out.quote.unit_price_fen == out.quote.cost_unit_fen + 500
    assert not api.create_calls


def test_final_fixed_cost_rule_cannot_bypass_seat_verification_after_reselection():
    from tests import _check_channel_route as fixture
    from application.ticket_flow import FlowResult
    from domain.conversation import Conversation, ConvState
    from domain.quote_rules import QuoteRule, QuoteRuleEngine, QUOTE_FIXED
    f, api, _ = fixture.flow(fixture.MAP_WANDA)
    f.rules = QuoteRuleEngine([
        QuoteRule(name='高价普通', channel='WANDA', weight=30, hall_keywords=['IMAX'],
                  price_left_yuan=200, price_right_yuan=300, markup_yuan=1, capped=False),
        QuoteRule(name='低价固定', channel='WANDA', weight=20, hall_keywords=['IMAX'],
                  fixed_cost_fen=5500, original_discount_bps=8900, minimum_price_fen=5500,
                  quote_mode=QUOTE_FIXED, capped=False)])
    conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
    out = f._after_recognition(conv, fixture.result(expected_seat_count=1, seats=[]), FlowResult())
    assert not out.ok and out.quote is None
    assert 'WANDA_QUOTE_FAILED' in out.template_keys
    assert not api.create_calls
