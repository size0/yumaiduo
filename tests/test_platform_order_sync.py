from types import SimpleNamespace
from unittest.mock import patch

import pytest

from application import ledger
from application.platform_orders import observe_event, refresh_record, platform_row
from domain.order import OrderStore


IDENT = dict(tenant_id='107', shop_id='shop', buyer_id='buyer', chat_id='chat')
EVENT = dict(id='event', event='im.message.received', tenantId='107', payload=dict(
    accountUnb='shop', peerUnb='buyer', chatId='chat', orderId='5127543074018066935', messageType=26))


def detail(status=2, amount='13700', **extra):
    return dict(orderId=EVENT['payload']['orderId'], accountUnb='shop', orderStatus=status,
                payment=amount, payTime='2026-10-17T10:50:42Z' if status != 1 else None,
                grouponPending=False, **extra)


@pytest.fixture
def store():
    s = OrderStore()
    yield s
    s.close()


def test_manual_paid_order_persists_without_quote_or_commands(store):
    result = observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail())
    row = store.platforms.all()[0]
    assert result['ok'] and row['platform_status'] == 2 and row['amount_fen'] == 13700
    assert store.by_identity(IDENT) == []
    ui = platform_row(row)
    assert ui['record_kind'] == 'PLATFORM_ORDER' and ui['quote_id'] == ''
    assert not ui['can_deliver'] and ui['payment_state'] != 'CONFIRMED'


def test_duplicate_events_are_idempotent_and_identity_is_immutable(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail())
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail())
    assert len(store.platforms.all()) == 1
    wrong = {**EVENT, 'payload': {**EVENT['payload'], 'peerUnb': 'other'}}
    assert not observe_event(wrong, store, trusted_event=True, reader=lambda *_: detail())['ok']
    assert store.platforms.all()[0]['buyer_id'] == 'buyer'


def test_unavailable_read_still_records_order_and_later_refresh_recovers(store):
    def unavailable(*_):
        raise RuntimeError('unavailable')
    observe_event(EVENT, store, trusted_event=True, reader=unavailable)
    row = store.platforms.all()[0]
    assert row['sync_state'] == 'PENDING' and row['platform_status'] is None
    refresh_record(store, row, reader=lambda *_: detail())
    assert store.platforms.all()[0]['platform_status'] == 2


def test_stale_unpaid_snapshot_never_regresses_paid_or_shipped(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(3))
    row = store.platforms.all()[0]
    refresh_record(store, row, reader=lambda *_: detail(1, '2000'))
    current = store.platforms.all()[0]
    assert current['platform_status'] == 3 and current['amount_fen'] == 13700
    assert platform_row(current)['shipped']
    assert not platform_row(current)['can_deliver']


@pytest.mark.parametrize('response', [{'orderId':'wrong'},
    {**detail(), 'accountUnb':'other'}])
def test_wrong_platform_order_or_shop_not_applied(store, response):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: response)
    assert store.platforms.all()[0]['platform_status'] is None


def test_real_platform_order_appears_in_order_management_without_ai_quote(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(3))
    with patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)):
        payload = ledger.orders_payload()
    assert payload['counts']['orders'] == 1
    assert payload['orders'][0]['platform_order_id'] == EVENT['payload']['orderId']
    assert payload['orders'][0]['platform_paid_yuan'] == 137


def test_platform_record_is_not_transaction_order(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail())
    row = store.platforms.all()[0]
    assert store.get(row['order_id']) is None


def test_closed_platform_order_is_not_pending_issuance(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(6))
    assert platform_row(store.platforms.all()[0])['bucket'] == 'closed'


def test_unsigned_event_cannot_create_platform_record(store):
    assert not observe_event(EVENT, store, reader=lambda *_: detail())['ok']
    assert store.platforms.all() == []


@pytest.mark.parametrize('field', ['buyerId', 'peerUnb', 'chatId', 'tenantId'])
def test_mismatched_buyer_and_chat_response_stays_unverified(store, field):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: {**detail(), field: 'other'})
    row = store.platforms.all()[0]
    assert row['platform_status'] is None and row['sync_error'] == 'PLATFORM_ORDER_OWNER_MISMATCH'


def test_status_five_can_finish_and_closed_does_not_reopen(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(5))
    row = store.platforms.all()[0]
    refresh_record(store, row, reader=lambda *_: detail(6))
    assert store.platforms.all()[0]['platform_status'] == 6
    refresh_record(store, row, reader=lambda *_: detail(2))
    assert store.platforms.all()[0]['platform_status'] == 6


def test_platform_facts_survive_store_reload(tmp_path):
    path = str(tmp_path / 'orders.sqlite3')
    first = OrderStore(path)
    observe_event(EVENT, first, trusted_event=True, reader=lambda *_: detail())
    first.close()
    second = OrderStore(path)
    try:
        assert second.platforms.all()[0]['amount_fen'] == 13700
        assert second.by_identity(IDENT) == []
    finally:
        second.close()


def test_unbound_platform_rows_are_separate_and_trade_actions_reject_them(store):
    from application import ticket_system_handoff as handoff
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail())
    with patch.object(handoff, 'scope', return_value=('107', {'shop'})), \
         patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)):
        result = handoff.handle({'action': 'list'})
        assert result['orders'] == [] and len(result['platform_orders']) == 1
        assert not result['platform_orders'][0]['can_create']
        for action in ('prepare', 'bind', 'deliver'):
            with pytest.raises(ValueError, match='ORDER_OUTSIDE_SCOPE'):
                handoff.handle({'action': action, 'order_id': result['platform_orders'][0]['order_id']})


def test_cached_paid_but_live_shipped_cannot_prepare_new_supplier_order(store):
    from application import ticket_system_handoff as handoff
    from domain.order import Order
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(2))
    store.create(Order(order_id='o', quote_id='q', **IDENT, provider='WANDA', state='PAID',
        payment_state='CONFIRMED', amount_fen=13700, paid_fen=13700,
        provider_order_id=EVENT['payload']['orderId'], idempotency_key='key'))
    with patch.object(handoff, 'scope', return_value=('107', {'shop'})), \
         patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)), \
         patch.object(handoff, 'platform_order', return_value=detail(3)):
        with pytest.raises(ValueError, match='PLATFORM_PAYMENT_NOT_CONFIRMED'):
            handoff.handle({'action': 'prepare', 'order_id': 'o'})


def test_bound_record_is_deduplicated_but_amount_difference_is_visible(store):
    from domain.order import Order
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(3))
    store.create(Order(order_id='o', quote_id='q', **IDENT, provider='WANDA', state='CREATED',
        amount_fen=2000, provider_order_id=EVENT['payload']['orderId'], idempotency_key='key'))
    with patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)):
        payload = ledger.orders_payload()
    assert payload['counts']['orders'] == 1
    row = payload['orders'][0]
    assert row['amount_yuan'] == 20 and row['platform_paid_yuan'] == 137
    assert row['bucket'] == 'attention'
    assert store.get('o').payment_state == 'UNPAID'


def test_event_observation_creates_no_message_or_trade_commands(store):
    from application import plugin_runtime
    with patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)), \
         patch('application.platform_orders._read', return_value=detail(3)), \
         patch.object(plugin_runtime, 'get_local_service', return_value=None):
        assert plugin_runtime.events_to_commands(EVENT) == []
        assert plugin_runtime.events_to_commands(EVENT) == []
    assert len(store.platforms.all()) == 1


def test_periodic_retry_refreshes_stale_unpaid_data_without_actions(store):
    from application.platform_orders import process_due
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(1, '2000'))
    row = store.platforms.all()[0]
    row['next_sync_at'] = 0
    store.platforms.put(row)
    assert process_due(store, reader=lambda *_: detail(3), clock=1e9) == 1
    assert store.platforms.all()[0]['amount_fen'] == 13700


def test_prepare_rechecks_order_and_hold_after_network_read(store):
    from application import ticket_system_handoff as handoff
    from domain.order import Order
    store.create(Order(order_id='o', quote_id='q', **IDENT, provider='WANDA', state='PAID',
        payment_state='CONFIRMED', amount_fen=13700, paid_fen=13700,
        provider_order_id=EVENT['payload']['orderId'], idempotency_key='key'))
    def changed_during_read(*_):
        order = store.get('o')
        order.payload['manual_settlement'] = {'fulfillment_hold': True}
        store.put(order)
        return detail(2)
    with patch.object(handoff, 'scope', return_value=('107', {'shop'})), \
         patch.object(ledger, 'stores', return_value=SimpleNamespace(orders=store, quotes=None)), \
         patch.object(handoff, 'platform_order', side_effect=changed_during_read):
        with pytest.raises(ValueError, match='ORDER_CHANGED_RETRY_READ'):
            handoff.handle({'action': 'prepare', 'order_id': 'o'})


def test_concurrent_database_writer_cannot_be_overwritten_by_stale_read(store):
    observe_event(EVENT, store, trusted_event=True, reader=lambda *_: detail(1, '2000'))
    old = store.platforms.all()[0]
    def another_writer_during_read(*_):
        current = store.platforms.all()[0]
        current.update(platform_status=3, amount_fen=13700, paid_at=detail()['payTime'])
        store.platforms.put(current)
        return detail(2)
    assert refresh_record(store, old, reader=another_writer_during_read)['status'] == 'PLATFORM_RECORD_CHANGED'
    assert store.platforms.all()[0]['platform_status'] == 3
