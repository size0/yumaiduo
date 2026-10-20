from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
from datetime import time

import pytest

from application.ticket_flow import FlowResult, _buyer_self_buy_total
from domain.conversation import Conversation, ConvState
from domain.quote import QuoteStore
from domain.quote_rules import QuoteRuleEngine
from tests._check_channel_route import MAP_WANDA, flow, result


def run_quote(self_buy, *, mismatch=False, rewritten=False, count=2):
    f, api, _ = flow(MAP_WANDA)
    f.quote_store = QuoteStore()
    conv = Conversation(chat_id='c', tenant_id='t', shop_id='s', buyer_id='b', state=ConvState.QUOTED)
    r = result(seats=[{'seatNo': '6排9座', 'seatPriceFen': '2490', 'originalPriceFen': '3000'},
                      {'seatNo': '6排10座', 'seatPriceFen': '2490', 'originalPriceFen': '3000'}],
               price_all_fen=self_buy, price_mismatch=mismatch,
               seat_matched=True,
               raw_results={'buyer_text_details': rewritten})
    quote = replace(QuoteRuleEngine([]).quote(seat_count=count, now=time(12), estimate_total_fen=5600,
                                              original_total_fen=6000),
                    unit_price_fen=2900, total_price_fen=5800, margin_total_fen=200)
    try:
        f.rules = SimpleNamespace(quote=lambda **kwargs: quote)
        with patch('application.ticket_flow._second_quote_wait_seconds', return_value=0):
            out = f._build_quote(conv, r, FlowResult())
        return out, conv.quote_id, f.quote_store.by_identity(dict(tenant_id='t', shop_id='s', buyer_id='b', chat_id='c')), api
    finally:
        f.quote_store.close()


@pytest.mark.parametrize('self_buy', [4980, 5800])
def test_wanda_at_or_above_self_buy_sends_hint_without_trade_quote(self_buy):
    out, quote_id, rows, api = run_quote(self_buy)
    assert 'SELF_BUY_HINT' in out.template_keys
    assert 'QUOTE_CARD' not in out.template_keys
    assert not quote_id and not rows
    assert not api.create_calls


def test_cheaper_wanda_quote_continues():
    out, _, _, _ = run_quote(6500)
    assert 'QUOTE_CARD' in out.template_keys and 'SELF_BUY_HINT' not in out.template_keys


@pytest.mark.parametrize('changes', [dict(mismatch=True), dict(rewritten=True), dict(count=3)])
def test_unverified_or_different_request_price_is_not_self_buy_evidence(changes):
    out, _, _, _ = run_quote(4980, **changes)
    assert 'SELF_BUY_HINT' not in out.template_keys


def test_pending_seat_confirmation_is_not_self_buy_evidence():
    r = result(seats=[{'seatNo': '9排9座'}], seat_matched=True,
               price_all_fen=2490, seat_confirm_required=True)
    assert _buyer_self_buy_total(r, 1) is None


def test_complete_per_seat_actual_prices_can_replace_missing_total():
    r = result(seats=[{'seatPriceFen': '2490'}, {'seatPriceFen': '2490'}],
               seat_matched=True, price_all_fen=None)
    assert _buyer_self_buy_total(r, 2) == 4980


def test_missing_one_seat_price_does_not_underestimate_self_buy_total():
    r = result(seats=[{'seatPriceFen': '2490'}, {}], seat_matched=True, price_all_fen=None)
    assert _buyer_self_buy_total(r, 2) is None


def test_self_buy_retains_nontradable_comparison_for_followup():
    from application.quote_comparison import current
    f, _, _=flow(MAP_WANDA)
    f.quote_store=QuoteStore()
    conv=Conversation(chat_id='c',tenant_id='t',shop_id='s',buyer_id='b',state=ConvState.QUOTED)
    r=result(seats=[{'seatNo':'6排9座','seatPriceFen':'100'}],seat_matched=True,price_all_fen=100)
    try:
        f._build_quote(conv,r,FlowResult())
        comparison=current(conv)
        assert comparison['provider']=='WANDA' and comparison['tradable'] is False
        assert comparison['buyer_total_fen']==100
        assert comparison['sell_total_fen']>=100 and comparison['cost_total_fen']>0
        assert comparison['cinema']==r.cinema_name and not conv.quote_id
        conv.demand_rev=getattr(conv,'demand_rev',0)+1
        assert current(conv) is None
    finally:
        f.quote_store.close()
