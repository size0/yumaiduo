from datetime import time

import pytest

from domain.quote_rules import QuoteRule, QuoteRuleEngine, QUOTE_FIXED, validate_rule


def special_rule(**kwargs):
    return QuoteRule(name='特别厅', channel='WANDA', hall_keywords=['VIP'],
                     fixed_cost_fen=5500, original_discount_bps=8900,
                     minimum_price_fen=5500, quote_mode=QUOTE_FIXED,
                     capped=False, **kwargs)


def test_hall_rules_match_verified_hall_and_route_not_cinema_brand():
    special = special_rule(weight=30)
    regular = QuoteRule(name='普通万达', channel='WANDA', excluded_hall_keywords=['VIP'])
    engine = QuoteRuleEngine([special, regular])
    assert engine.find_rule(now=time(12), channel='WANDA', hall_name='3号ｖｉｐ厅') is special
    assert engine.find_rule(now=time(12), channel='WANDA', hall_name='7号IMAX厅') is regular
    assert engine.find_rule(now=time(12), channel='LIANGPIAO', hall_name='VIP厅') is None
    assert engine.find_rule(now=time(12), channel='WANDA', hall_name='') is None


@pytest.mark.parametrize('cost,discount,floor,expected', [
    (5500, 8900, 5500, 11303), (6100, 8000, 6500, 10160),
    (4000, 1000, 4500, 4500),
])
def test_configured_cost_discount_floor_not_fixed_in_code(cost, discount, floor, expected):
    rule = special_rule()
    rule.fixed_cost_fen = cost
    rule.original_discount_bps = discount
    rule.minimum_price_fen = floor
    q = QuoteRuleEngine([rule]).quote(now=time(12), channel='WANDA', hall_name='VIP厅',
        seat_count=2, estimate_total_fen=200, original_total_fen=25400)
    assert q.unit_price_fen == expected
    assert q.cost_total_fen == cost * 2
    assert q.pricing_rule_id == '特别厅'


def test_invalid_discount_and_ambiguous_calculation_rejected():
    rule = special_rule()
    rule.original_discount_bps = 10001
    assert validate_rule(rule)


@pytest.mark.parametrize('field,value', [('fixed_cost_fen', '55'), ('fixed_cost_fen', True),
    ('minimum_price_fen', None), ('original_discount_bps', '8900'), ('hall_keywords', 'VIP')])
def test_malformed_new_fields_are_validation_errors_not_exceptions(field, value):
    rule = special_rule()
    setattr(rule, field, value)
    assert validate_rule(rule)


def test_normal_hall_does_not_use_fixed_cost_or_discount():
    special = special_rule(weight=30)
    regular = QuoteRule(name='普通万达', channel='WANDA', markup_yuan=1, capped=False,
                        excluded_hall_keywords=['VIP'])
    q = QuoteRuleEngine([special, regular]).quote(now=time(12), channel='WANDA',
        hall_name='IMAX厅', seat_count=2, estimate_total_fen=5600, original_total_fen=8000)
    assert q.unit_price_fen == 2900 and q.cost_total_fen == 5600


def test_migration_preserves_shared_liangpiao_and_normal_wanda_prices_and_is_idempotent():
    from application.hall_pricing_migration import migrate_rules
    from dataclasses import asdict
    original = [QuoteRule(name='通用规则', brands=['万达影城'], markup_yuan=3, capped=False)]
    migrated = migrate_rules(original, keyword='VIP', discount_bps=8900, cost_fen=5500, floor_fen=5500)
    for channel in ('WANDA', 'LIANGPIAO'):
        for hall in ('普通厅', '') if channel == 'LIANGPIAO' else ('普通厅',):
            args = dict(now=time(12), channel=channel, hall_name=hall, brand='万达影城',
                        seat_count=2, estimate_total_fen=5600, original_total_fen=8000)
            before = QuoteRuleEngine(original).quote(**args)
            after = QuoteRuleEngine(migrated).quote(**args)
            assert before.total_price_fen == after.total_price_fen
    again = migrate_rules(migrated, keyword='VIP', discount_bps=8900, cost_fen=5500, floor_fen=5500)
    assert [asdict(r) for r in again] == [asdict(r) for r in migrated]
    assert not original[0].excluded_hall_keywords and original[0].channel == ''


def test_migration_keeps_stable_order_when_priority_and_sequence_tie():
    from application.hall_pricing_migration import migrate_rules
    rows = [QuoteRule(name='先添加通用', brands=['万达影城'], markup_yuan=5, capped=False),
            QuoteRule(name='后添加万达', channel='WANDA', markup_yuan=1, capped=False)]
    after = migrate_rules(rows, keyword='VIP', discount_bps=8900, cost_fen=5500, floor_fen=5500)
    args = dict(now=time(12), channel='WANDA', hall_name='普通厅', brand='万达影城',
                seat_count=1, estimate_total_fen=3000, original_total_fen=5000)
    assert QuoteRuleEngine(rows).quote(**args).unit_price_fen == 3500
    assert QuoteRuleEngine(after).quote(**args).unit_price_fen == 3500
