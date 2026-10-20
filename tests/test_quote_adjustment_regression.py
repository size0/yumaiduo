from domain.quote_rules import ADJUST_ROUND_YUAN_MINUS_01, _adjust


def test_round_to_yuan_minus_ten_fen_is_applied_before_cost_floor():
    assert _adjust(3296, ADJUST_ROUND_YUAN_MINUS_01) == 3290
    assert _adjust(3250, ADJUST_ROUND_YUAN_MINUS_01) == 3290
