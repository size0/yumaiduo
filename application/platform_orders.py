"""Synchronize marketplace facts without creating or authorizing transactions."""
from datetime import datetime, timezone
from threading import RLock
import json
import time

from domain.order.platform import KEYS

_SYNC_LOCK = RLock()
_LAST_TICK = 0.0


def _read(row, identity):
    from integrations.yumaiduo.shop_client import PluginShopsClient, resolve_plugin_secret
    from .official_dialogue import _ReadTransport
    secret = resolve_plugin_secret()
    if not secret:
        raise ValueError('PLATFORM_READ_UNAVAILABLE')
    client = PluginShopsClient(secret=secret, timeout=2, transport=_ReadTransport())
    status, raw = client._post('/internal/orders/read', {'tenant_id': identity['tenant_id'],
                                                       'order_id': row['platform_order_id']})
    if status != 200:
        raise ValueError('PLATFORM_READ_UNAVAILABLE')
    return json.loads(raw)


def refresh_record(store, row, *, reader=None):
    # A platform has no monotonic revision in this response. Serialize reads,
    # then refuse regressive snapshots rather than racing their completion.
    with _SYNC_LOCK:
        current = store.platforms.get(row['tenant_id'], row['shop_id'], row['platform_order_id'])
        if current is None:
            return {'ok': False, 'status': 'NOT_FOUND'}
        previous_snapshot = dict(current)
        try:
            identity = {key: current[key] for key in KEYS}
            detail = (reader or _read)(current, identity)
            if (str(detail.get('orderId')) != current['platform_order_id']
                    or str(detail.get('accountUnb')) != current['shop_id']
                    or type(detail.get('orderStatus')) is not int or detail['orderStatus'] not in range(1, 7)):
                raise ValueError('PLATFORM_ORDER_SCOPE_MISMATCH')
            for field, key in (('buyerId', 'buyer_id'), ('buyer_id', 'buyer_id'), ('peerUnb', 'buyer_id'),
                               ('chatId', 'chat_id'), ('tenantId', 'tenant_id')):
                if detail.get(field) is not None and str(detail[field]) != identity[key]:
                    raise ValueError('PLATFORM_ORDER_OWNER_MISMATCH')
            amount = detail.get('payment')
            if isinstance(amount, bool) or not str(amount).isdigit():
                raise ValueError('PLATFORM_AMOUNT_UNKNOWN')
            amount = int(amount)
            paid_at = detail.get('payTime')
            if paid_at:
                parsed = datetime.fromisoformat(str(paid_at).replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('PLATFORM_PAYMENT_TIME_UNKNOWN')
            before, after = current['platform_status'], detail['orderStatus']
            if (before in (2, 3, 4) and after < before
                    or before == 6 and after != before
                    or current.get('paid_at') and (not paid_at or amount != current.get('amount_fen'))):
                raise ValueError('PLATFORM_SNAPSHOT_CONFLICT')
            current.update(platform_status=after, amount_fen=amount, paid_at=paid_at,
                           platform_created_at=detail.get('createTime'),
                           shipped_observed=bool(current.get('shipped_observed') or after in (3,4)),
                           platform_buyer_verified=any(detail.get(k) is not None for k in ('buyerId','buyer_id','peerUnb')),
                           sync_state='VERIFIED', sync_error='',
                           checked_at=datetime.now(timezone.utc).isoformat(),
                           next_sync_at=time.time() + (3600 if after in (3, 4, 5, 6) else 30))
            store.platforms.put(current, expected=previous_snapshot)
            return {'ok': True, 'status': 'VERIFIED'}
        except Exception as exc:
            if str(exc) == 'PLATFORM_RECORD_CHANGED':
                return {'ok': False, 'status': 'PLATFORM_RECORD_CHANGED'}
            current = store.platforms.get(row['tenant_id'], row['shop_id'], row['platform_order_id'])
            if current is None:
                return {'ok':False,'status':'NOT_FOUND'}
            previous_snapshot = dict(current)
            current.update(sync_error=str(exc) if isinstance(exc, ValueError) and str(exc).startswith('PLATFORM_')
                           else 'PLATFORM_READ_UNAVAILABLE', next_sync_at=time.time()+30)
            try:
                store.platforms.put(current, expected=previous_snapshot)
            except ValueError:
                return {'ok': False, 'status': 'PLATFORM_RECORD_CHANGED'}
            return {'ok': False, 'status': current['sync_error']}


def observe_event(event, store, *, reader=None, trusted_event=False):
    if not trusted_event:
        return {'ok': False, 'status': 'UNTRUSTED_EVENT'}
    if store is None or not hasattr(store, 'platforms'):
        return {'ok': False, 'status': 'STORAGE_UNAVAILABLE'}
    from .plugin_runtime import _identity
    p = event.get('payload') or {}
    number = str(p.get('orderId') or p.get('order_id') or '')
    identity = _identity(event)
    if not number.isdigit() or not identity['tenant_id'] or not identity['shop_id']:
        return {'ok': False, 'status': 'IDENTITY_REQUIRED'}
    old = store.platforms.get(identity['tenant_id'], identity['shop_id'], number)
    if old:
        # Missing keys may be recovered only from this exact persisted order.
        identity = {k: identity[k] or old[k] for k in KEYS}
    try:
        row = store.platforms.discover(identity, number, str(event.get('id') or event.get('eventId') or ''),
                                       source='SIGNED_ORDER_EVENT')
    except ValueError:
        return {'ok': False, 'status': 'IDENTITY_CONFLICT'}
    return refresh_record(store, row, reader=reader)


def process_due(store, *, reader=None, clock=None):
    global _LAST_TICK
    now = time.monotonic() if clock is None else clock
    if now - _LAST_TICK < 10 or store is None or not hasattr(store, 'platforms'):
        return 0
    _LAST_TICK = now
    due = sorted((r for r in store.platforms.all() if r.get('next_sync_at', 0) <= time.time()
                  and not (r.get('platform_status') in (4,6) and r.get('sync_state')=='VERIFIED')),
                 key=lambda r: r.get('next_sync_at', 0))[:2]
    for row in due:
        refresh_record(store, row, reader=reader)
    return len(due)


def platform_row(row):
    status = row.get('platform_status')
    paid = bool(row.get('paid_at') and (row.get('amount_fen') or 0) > 0)
    shipped = bool(row.get('shipped_observed') or status in (3, 4))
    labels = {1: '待付款', 2: '已付款（购票信息待核对）', 3: '平台已发货（交付记录待核对）',
              4: '平台交易已完成', 5: '平台状态待核对', 6: '平台已关闭'}
    return dict(row, provider='PLATFORM', provider_order_id='', quote_id='', state='PLATFORM_'+str(status or 'UNKNOWN'),
        state_label=labels.get(status, '平台状态待核实'),
        bucket='closed' if status in (4, 6) else 'unpaid' if status == 1 else 'attention',
        payment_state='PLATFORM_PAID' if paid else 'UNVERIFIED',
        payment_label='已付款（平台核实）' if paid else '未核实付款',
        amount_yuan=(row.get('amount_fen') or 0)/100, paid_yuan=(row.get('amount_fen') or 0)/100 if paid else 0,
        platform_paid_yuan=(row.get('amount_fen') or 0)/100 if paid else None,
        platform_shipped_observed=shipped, shipped=shipped, cinema='', movie='', show='', seats=[], ticket_count=0,
        ticket_codes=[], wanda_ticket_image=None, can_deliver=False,
        can_deliver_reason='平台订单尚未关联经核实的购票信息，不能自动出票或发货',
        needs_attention=status not in (4, 6), created_at=row['first_seen_at'], updated_at=row.get('checked_at') or row['first_seen_at'],
        manual_settlement={}, can_confirm_manual_price=False, failure_code='', idempotency_key='',
        delivered_at='', needs_review=True)


def merge_rows(transaction_rows, platform_store):
    facts = {(r['tenant_id'], r['shop_id'], r['platform_order_id']): r for r in platform_store.all()}
    for row in transaction_rows:
        number = row.get('platform_order_id') or (row.get('provider_order_id') if row.get('provider') == 'WANDA' else '')
        key = (row['tenant_id'], row['shop_id'], number)
        fact = facts.get(key)
        if not fact:
            continue
        if any(row[k] != fact[k] for k in KEYS):
            row.update(needs_attention=True, platform_sync_error='IDENTITY_CONFLICT')
            continue
        facts.pop(key)
        projected = platform_row(fact)
        row['platform_sync_state'] = fact['sync_state']
        row['platform_sync_error'] = fact['sync_error']
        if projected['platform_paid_yuan'] is not None:
            row['platform_paid_yuan'] = projected['platform_paid_yuan']
            if row['payment_state'] != 'CONFIRMED' or projected['platform_paid_yuan'] != row['amount_yuan']:
                row['payment_label'] = '平台已付款（金额／履约待核对）'
        row['platform_shipped_observed'] = projected['platform_shipped_observed']
        if row['state'] not in ('DELIVERED', 'CANCELLED', 'REFUNDED') and (
                projected['platform_shipped_observed'] or projected['platform_paid_yuan'] is not None and
                (row['payment_state'] != 'CONFIRMED' or projected['platform_paid_yuan'] != row['amount_yuan'])):
            row['state_label'] = projected['state_label']
            row['bucket'] = 'attention'
            row['needs_attention'] = True
        if fact['platform_status'] in (4, 5, 6) and row['state'] not in ('DELIVERED', 'CANCELLED', 'REFUNDED'):
            row['state_label'], row['bucket'] = projected['state_label'], projected['bucket']
    return sorted([*transaction_rows, *(platform_row(r) for r in facts.values())],
                  key=lambda r: r['created_at'], reverse=True)
