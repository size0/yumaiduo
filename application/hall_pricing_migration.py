"""Explicit, one-time conversion of a legacy policy into editable quote rules."""
from copy import deepcopy

from domain.quote_rules import QuoteRule, QUOTE_FIXED, validate_rules


def migrate_rules(rows, *, keyword, discount_bps, cost_fen, floor_fen):
    result = deepcopy(rows)
    if any(r.channel == 'WANDA' and keyword in r.hall_keywords
           and r.original_discount_bps is not None for r in result):
        return result
    # Split shared rules into equivalent route-specific copies before adding
    # a Wanda-only exclusion; Liangpiao coverage must remain unchanged.
    expanded = []
    names = {r.name for r in result}
    for rule in result:
        if not rule.channel:
            clone = deepcopy(rule)
            clone.channel = 'WANDA'
            clone.name += '（万达路线）'
            while clone.name in names:
                clone.name += '副本'
            names.add(clone.name)
            expanded.append(clone)
            rule.channel = 'LIANGPIAO'
        expanded.append(rule)
    result = expanded
    for rule in result:
        if rule.channel == 'WANDA' and keyword not in rule.excluded_hall_keywords:
            rule.excluded_hall_keywords.append(keyword)
    names = {r.name for r in result}
    name = '万达VIP厅报价'
    while name in names:
        name += '（影厅规则）'
    result.append(QuoteRule(name=name, channel='WANDA', hall_keywords=[keyword],
        fixed_cost_fen=cost_fen, minimum_price_fen=floor_fen,
        original_discount_bps=discount_bps, quote_mode=QUOTE_FIXED, capped=False,
        weight=max((r.weight for r in result), default=0)+1,
        created_seq=max((r.created_seq for r in result), default=0)+1))
    problems = validate_rules(result)
    if problems:
        raise ValueError('; '.join(problems))
    return result
