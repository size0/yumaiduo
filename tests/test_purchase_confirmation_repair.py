from unittest.mock import patch

import pytest

from application import purchase_actions, request_failure
from tests import test_agent_purchase as purchase_fixture


@pytest.fixture
def case():
    instance = purchase_fixture.AgentPurchaseTests()
    instance.setUp()
    instance.tools.conv.quote_id = 'new'
    yield instance
    instance.doCleanups()


def test_no_order_with_current_full_quote_explains_purchase_without_assuming_not_placed(case):
    with patch.object(case.tools, '_orders', return_value=[]):
        result = purchase_actions.inspect(case.tools)
    assert result['status'] == 'NO_BOUND_ORDER'
    assert result['purchase_ready'] is True
    assert result['next_tool'] == 'get_purchase_guidance'
    text = request_failure.explain('read_platform_order', result)
    assert '如果已经拍下，请勿重复下单' in text
    assert '尚未关联' not in text and '已改价' not in text
    assert case.tools.flow_result is None


@pytest.mark.parametrize('unsafe', ['stale_demand', 'unverified_image', 'pending_change', 'seller_review'])
def test_no_order_does_not_encourage_purchase_from_unsafe_quote(case, unsafe):
    if unsafe == 'stale_demand':
        case.tools.conv.demand_id = 'another-demand'
    elif unsafe == 'unverified_image':
        case.tools.session.image_context['unverified_image'] = True
    elif unsafe == 'pending_change':
        case.tools.session.image_context['ticket_details'] = {'pending': True}
    else:
        case.tools.seller_context = {'active': True}
    with patch.object(case.tools, '_orders', return_value=[]):
        result = purchase_actions.inspect(case.tools)
    assert result['purchase_ready'] is False
    assert '本次报价有效' not in request_failure.explain('read_platform_order', result)
    assert case.tools.flow_result is None


def test_purchase_guidance_is_read_only_and_describes_unbound_order_conditionally(case):
    with patch.object(case.tools, '_orders', return_value=[]):
        result = case.tools.get_purchase_guidance()
    assert result['status'] == 'READY_TO_PURCHASE'
    assert '若已拍下，请勿重复下单' in result['guidance']
    assert case.tools.flow_result is None


@pytest.mark.parametrize('state', ['AWAIT_PAY', 'AWAIT_REPRICE', 'SELF_BUY', 'AWAIT_TICKET'])
def test_existing_purchase_states_cannot_be_reset_by_missing_order(case, state):
    from domain.conversation import ConvState
    case.tools.conv.state = getattr(ConvState, state)
    with patch.object(case.tools, '_orders', return_value=[]):
        result = purchase_actions.inspect(case.tools)
        guidance = case.tools.get_purchase_guidance()
    assert result['purchase_ready'] is False
    assert guidance['ok'] is False
    assert '可以拍下' not in request_failure.explain('read_platform_order', result)
    assert case.tools.flow_result is None


def test_multiple_unbound_platform_orders_never_encourage_new_purchase(case):
    candidates = [{'platform_order_id': 'one', 'platform_status': 1},
                  {'platform_order_id': 'two', 'platform_status': 2}]
    with patch.object(case.tools, '_orders', return_value=[]), patch.object(
            case.orders.platforms, 'by_identity', return_value=candidates):
        result = purchase_actions.inspect(case.tools)
    assert result['status'] == 'ORDER_AMBIGUOUS'
    assert not result.get('purchase_ready')
    assert '可以拍下' not in request_failure.explain('read_platform_order', result)
