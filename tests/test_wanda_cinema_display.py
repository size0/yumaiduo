"""Use the mapped Wanda name for display without changing source identity."""
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from application.ticket_flow import FlowResult
from domain.conversation import Conversation
from tests import _check_channel_route as fixture


@pytest.mark.parametrize('unit_only', [True, False])
@pytest.mark.parametrize('mapped_name', ['太原龙湖万达广场店', '  ', ''])
def test_quote_uses_mapped_name_only_for_display(unit_only, mapped_name):
    mapping = deepcopy(fixture.MAP_WANDA)
    mapping['data']['wandaCinemaName'] = mapped_name
    flow, api, transport = fixture.flow(mapping)
    source = '万达影城 太原龙湖激光IMAX万达广场店（解放路店）'
    result = fixture.result(cinema_name=source,
        seats=[] if unit_only else [{'seatNo': '6排9座'}],
        expected_seat_count=None if unit_only else 1)
    captured = []
    def capture(out, key, values=None):
        captured.append((key, values or {}))
        out.template_keys.append(key)
    with patch.object(FlowResult, 'add', capture):
        out = flow._after_recognition(Conversation(chat_id='test'), result, FlowResult())
    key = 'WANDA_UNIT_PRICE' if unit_only else 'QUOTE_CARD'
    assert out.ok and out.quote
    card = next(values for name, values in captured if name == key)
    assert card['cinema'] == (mapped_name.strip() or source)
    assert result.cinema_name == source
    assert not api.preflight_calls and not api.create_calls


def test_liangpiao_quote_keeps_source_cinema_name():
    flow, api, transport = fixture.flow(fixture.MAP_NONE)
    result = fixture.result(seats=[{'seatNo': '6排9座'}], seat_matched=True,
                            cinema_name='星河影城', brand_name='星河')
    quote = SimpleNamespace(unit_price_fen=5000,total_price_fen=5000,seat_count=1,
                            original_total_fen=5790)
    captured = []
    with patch.object(FlowResult, 'add', lambda out, key, values=None: captured.append((key, values or {}))):
        out = flow._emit_quote_card(result,result.seats,quote,FlowResult(channel='liangpiao'))
    assert out.ok
    assert next(values for key, values in captured if key in ('QUOTE_CARD', 'QUOTE_CARD_MULTI'))['cinema'] == result.cinema_name
