"""Authenticated, idempotent manual-Wanda issuance handoff. Never pays or issues."""
from contextlib import closing
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime
from . import ledger
from .purchase_actions import platform_order


def authenticate(headers, raw):
    key=os.getenv("TICKET_APP_KEY", ""); secret=os.getenv("TICKET_APP_SECRET", "")
    ts=headers.get("X-Timestamp", ""); nonce=headers.get("X-Nonce", "")
    if not key or not secret or headers.get("X-App-Key") != key:
        raise ValueError("AUTH_REQUIRED")
    if not ts.isdigit() or abs(time.time()-int(ts))>300 or not 16<=len(nonce)<=128:
        raise ValueError("AUTH_EXPIRED")
    expected=hmac.new(secret.encode(),key.encode()+ts.encode()+nonce.encode()+raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,headers.get("X-Sign", "")):
        raise ValueError("AUTH_INVALID")
    with closing(sqlite3.connect(str(ledger.orders_file().with_name("ticket-handoff-auth.sqlite3")))) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY, ts INTEGER)")
        db.execute("DELETE FROM nonces WHERE ts < ?",(int(time.time())-600,))
        try: db.execute("INSERT INTO nonces VALUES (?,?)",(nonce,int(ts)))
        except sqlite3.IntegrityError: raise ValueError("NONCE_REUSED") from None


def scope():
    tenant=os.getenv("TICKET_HANDOFF_TENANT_ID", "")
    shops=set(filter(None,os.getenv("TICKET_HANDOFF_SHOP_IDS", "").split(',')))
    if os.getenv("TICKET_HANDOFF_ENABLED")!='true' or not tenant or not shops:
        raise ValueError("HANDOFF_NOT_CONFIGURED")
    return tenant,shops


def rows(store):
    from domain.order import Order
    with store.lock:
        return [Order.from_dict(json.loads(r[0])) for r in store.connection.execute("SELECT payload FROM orders")]


def eligible(o, tenant, shops):
    return (o.provider=='WANDA' and o.tenant_id==tenant and o.shop_id in shops
            and o.payment_state=='CONFIRMED' and o.paid_fen>=o.amount_fen>0
            and not (o.payload.get('manual_settlement') or {}).get('fulfillment_hold')
            and not o.is_terminal and not o.payload.get('quote_revision_pending')
            and not o.ticket_codes and not o.payload.get('wanda_ticket_image') and not o.shipped and not o.failure_code)


def projection(o):
    quotes = getattr(ledger.stores(), 'quotes', None)
    quote = quotes.get(o.quote_id) if quotes is not None else None
    identity = (quote.metadata.get('provider_identity') or {}) if quote else {}
    return dict(order_id=o.order_id,quote_id=o.quote_id,tenant_id=o.tenant_id,shop_id=o.shop_id,
        buyer_id=o.buyer_id,chat_id=o.chat_id,platform_order_id=o.provider_order_id,
        cinema=o.cinema,movie=o.movie,show=o.show,ticket_count=o.ticket_count,
        seats=o.seats,amount_fen=o.amount_fen,binding=o.payload.get('ticket_system_binding'),
        provider_identity={k:str(identity.get(k) or '') for k in
            ('wanda_cinema_id','liangpiao_city_id','liangpiao_cinema_id')})


def buyer_labels(platform_facts, buyer_orders):
    """Read names from the scoped plugin profile; never derive one from an ID."""
    from .record_view import buyer_names
    buyer_orders = [row if isinstance(row, dict) else {
        key: getattr(row, key) for key in ('tenant_id', 'shop_id', 'buyer_id')
    } for row in buyer_orders]
    identities = list({
        (row['tenant_id'], row['shop_id'], row['buyer_id']):
            {key: row[key] for key in ('tenant_id', 'shop_id', 'buyer_id')}
        for row in [*platform_facts, *buyer_orders]
    }.values())
    # The handoff RPC has a 20-second timeout. Bound the synchronous lookup;
    # remaining profiles are cached in the background and picked up on retry.
    names = buyer_names(identities[:12], wait=True)
    pending = len(identities) > 12
    if pending:
        names.update(buyer_names(identities[12:]))
    return names, pending


def display_name(row, names):
    name = names.get((row['tenant_id'], row['shop_id'], row['buyer_id']), '')
    return name if name and name != row['buyer_id'] else ''


def platform_allows_creation(store, o):
    platforms = getattr(store, 'platforms', None)
    fact = platforms.get(o.tenant_id, o.shop_id, o.platform_order_id or o.provider_order_id) if platforms else None
    return fact is None or (all(fact[k] == getattr(o,k) for k in ('tenant_id','shop_id','buyer_id','chat_id'))
        and fact['platform_status'] == 2 and fact.get('paid_at') and not fact.get('sync_error') and not fact.get('shipped_observed'))


def handle(body):
    tenant,shops=scope();store=ledger.stores().orders;action=body.get('action')
    if action=='list':
        bound = rows(store)
        eligible_orders = [o for o in bound if eligible(o,tenant,shops) and platform_allows_creation(store,o)]
        # Separate collection: old workbench consumers must not mistake facts
        # without a quote for orders authorized to create supplier purchases.
        platforms = getattr(store, 'platforms', None)
        occupied = {(o.tenant_id, o.shop_id, o.platform_order_id or o.provider_order_id) for o in bound}
        platform_facts = [r for r in (platforms.all() if platforms else [])
            if r['tenant_id']==tenant and r['shop_id'] in shops
            and (r['tenant_id'],r['shop_id'],r['platform_order_id']) not in occupied
            and r['platform_status'] in (2,3,4) and r.get('paid_at')]
        names, pending = buyer_labels(platform_facts, eligible_orders)
        result = {'ok':True, 'profiles_pending':pending,
                  'orders':[dict(projection(o), buyer_name=display_name({
                      'tenant_id':o.tenant_id, 'shop_id':o.shop_id, 'buyer_id':o.buyer_id}, names))
                      for o in eligible_orders]}
        result['platform_orders'] = [dict(
            order_id=r['order_id'], platform_order_id=r['platform_order_id'],
            shop_id=r['shop_id'], buyer_id=r['buyer_id'], buyer_name=display_name(r,names),
            platform_status=r['platform_status'],
            amount_fen=r.get('amount_fen'), paid_at=r.get('paid_at'),
            checked_at=r.get('checked_at'), needs_review=True, can_create=False)
            for r in platform_facts]
        return result
    order_id=str(body.get('order_id') or ''); o=store.get(order_id)
    if o is None or o.tenant_id!=tenant or o.shop_id not in shops:
        raise ValueError('ORDER_OUTSIDE_SCOPE')
    if action == 'prepare':
        if not eligible(o, tenant, shops) or not platform_allows_creation(store, o) or o.payload.get('ticket_system_binding'):
            raise ValueError('ORDER_NOT_DELIVERABLE')
        identity = {k: getattr(o,k) for k in ('tenant_id','shop_id','buyer_id','chat_id')}
        detail = platform_order(o, identity)
        if (type(detail.get('orderStatus')) is not int or detail['orderStatus'] != 2
                or int(detail.get('payment',-1)) != o.amount_fen or not detail.get('payTime')
                or detail.get('grouponPending') is not False):
            raise ValueError('PLATFORM_PAYMENT_NOT_CONFIRMED')
        with store.lock:
            current = store.get(order_id)
            if current is None or current.to_dict() != o.to_dict():
                raise ValueError('ORDER_CHANGED_RETRY_READ')
            if not eligible(current, tenant, shops) or not platform_allows_creation(store, current) or current.payload.get('ticket_system_binding'):
                raise ValueError('ORDER_NOT_DELIVERABLE')
            return {'ok': True, 'order': projection(current)}
    binding=o.payload.get('ticket_system_binding')
    if action=='status':
        from .plugin_runtime import queue
        from integrations.yumaiduo.commands import dedupe_for
        q=queue()
        with q.lock:
            commands=[json.loads(x[0]) for x in q.connection.execute('SELECT payload FROM yumaiduo_commands WHERE dedupe_key LIKE ?', (dedupe_for(order_id,kind='notice')+'%',))]
            ship=q.connection.execute('SELECT status FROM yumaiduo_commands WHERE dedupe_key=?',(dedupe_for(order_id,kind='ship'),)).fetchone()
        return {'ok':True,'binding':binding,'codes_received':bool(o.ticket_codes or o.payload.get('wanda_ticket_image')),'qr_received':bool(o.payload.get('wanda_ticket_image')),
                'shipping_status':ship[0] if ship else 'NOT_QUEUED',
                'message_statuses':[x.get('status') for x in commands]}
    expected={k:str(body.get(k) or '') for k in ('tenant_id','shop_id','buyer_id','chat_id','quote_id')}
    if any(str(getattr(o,k))!=v for k,v in expected.items()):raise ValueError('ORDER_IDENTITY_CHANGED')
    candidate=body.get('binding') or {}
    if not all(candidate.get(k) for k in ('binding_id','wanda_order_id','phone','quote_id')) or candidate['quote_id']!=o.quote_id:
        raise ValueError('BINDING_REQUIRED')
    if action=='deliver':
        from .wanda_qr import receive
        return receive(order_id, body.get('image_data'), tenant=tenant, shops=shops,
                       binding=candidate, upstream_id=candidate['wanda_order_id'])
    if not eligible(o,tenant,shops) or not platform_allows_creation(store,o):raise ValueError('ORDER_NOT_DELIVERABLE')
    identity={k:getattr(o,k) for k in ('tenant_id','shop_id','buyer_id','chat_id')}
    detail=platform_order(o,identity)
    if type(detail.get('orderStatus')) is not int or detail['orderStatus'] != 2 or int(detail.get('payment',-1))!=o.amount_fen:
        raise ValueError('PLATFORM_PAYMENT_NOT_CONFIRMED')
    with store.lock:
        current=store.get(order_id)
        if current.to_dict()!=o.to_dict():raise ValueError('ORDER_CHANGED_RETRY_READ')
        if binding and binding!=candidate:raise ValueError('BINDING_CONFLICT')
        for other in rows(store):
            b=other.payload.get('ticket_system_binding') or {}
            if other.order_id!=order_id and b.get('wanda_order_id')==candidate['wanda_order_id']:
                raise ValueError('WANDA_ORDER_ALREADY_BOUND')
        if action=='bind':
            o.payload['ticket_system_binding']=candidate;store.put(o)
            return {'ok':True,'binding':candidate}
        raise ValueError('INVALID_ACTION')
