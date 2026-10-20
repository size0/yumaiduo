"""Platform payment facts and explicit staff settlements, without fulfillment."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from uuid import uuid5, NAMESPACE_URL

KEYS = ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')


def facts(order, detail, source):
    if not isinstance(detail, dict):
        return None
    try:
        stamp = datetime.fromisoformat(str(detail['payTime']).replace('Z', '+00:00'))
        amount = int(str(detail['payment']))
    except (KeyError, ValueError, TypeError):
        return None
    if (str(detail.get('orderId')) != order.provider_order_id
            or str(detail.get('accountUnb')) != order.shop_id
            or type(detail.get('orderStatus')) is not int or detail['orderStatus'] not in (2, 3, 4)
            or detail.get('grouponPending') is not False or stamp.tzinfo is None or amount <= 0):
        return None
    return {'order_id': order.provider_order_id, 'shop_id': order.shop_id,
            'platform_status': detail['orderStatus'], 'amount_fen': amount,
            'paid_at': stamp.isoformat(), 'checked_at': datetime.now(timezone.utc).isoformat(),
            'source': source}


def observe(orders, order, detail, *, source):
    observation = facts(order, detail, source)
    if not observation:
        return False
    with orders.lock:
        current = orders.get(order.order_id)
        if not current or any(getattr(current, key) != getattr(order, key) for key in (*KEYS, 'provider_order_id')):
            return False
        prior = current.payload.get('platform_payment_observation') or {}
        if prior and (prior.get('amount_fen') != observation['amount_fen']
                      or prior.get('platform_status', 0) > observation['platform_status']):
            return False
        current.payload = dict(current.payload, platform_payment_observation=observation)
        orders.put(current)
    return True


def confirm_manual_price(stores, *, order_id, tenant_id, shop_id, amount_fen,
                         expected_quote_id, expected_amount_fen, expected_updated_at,
                         actor, approval_id, platform_read):
    """Only an authenticated staff adapter may call this; never exposed to Agent.

    A closed settlement quote is created first. A failed order CAS can leave an
    unbound closed record, which is excluded from all ordinary quote selection.
    Payment remains AMBIGUOUS and fulfillment stays paused: no event replay.
    """
    if (type(amount_fen) is not int or amount_fen <= 0 or type(expected_amount_fen) is not int
            or not all((tenant_id, shop_id, expected_quote_id, expected_updated_at, actor, approval_id))):
        raise ValueError('MANUAL_CONFIRMATION_REQUIRED')
    order = stores.orders.get(order_id)
    if not order or order.tenant_id != tenant_id or order.shop_id != shop_id:
        raise ValueError('ORDER_SCOPE_MISMATCH')
    old = order.payload.get('manual_settlement') or {}
    repeated = (old.get('source_quote_id') == expected_quote_id and old.get('amount_fen') == amount_fen)
    if not repeated and (order.quote_id != expected_quote_id or order.amount_fen != expected_amount_fen
                         or order.updated_at.isoformat() != expected_updated_at):
        raise ValueError('ORDER_CHANGED_RELOAD')
    if (order.provider != 'WANDA' or not order.payload.get('manual_ticketing')
            or order.payload.get('upstream_created') or order.payload.get('quote_revision_pending')
            or order.failure_code or order.state not in ('CREATED', 'AWAITING_PAYMENT', 'REPRICE_PENDING')
            or (not repeated and order.payment_state != 'UNPAID')):
        raise ValueError('MANUAL_SETTLEMENT_BLOCKED')
    detail = platform_read(order, {key: getattr(order, key) for key in KEYS})
    observation = facts(order, detail, 'STAFF_PLATFORM_READ')
    if not observation or observation['amount_fen'] != amount_fen:
        raise ValueError('PLATFORM_PAYMENT_MISMATCH')
    with stores.orders.lock:
        current = stores.orders.get(order_id)
        if not current or current.to_dict() != order.to_dict():
            raise ValueError('ORDER_CHANGED_RELOAD')
        if repeated:
            return {'ok': True, 'already': True, 'order_id': order_id, 'fulfillment_paused': True}
        quote = stores.quotes.get(expected_quote_id)
        if (not quote or quote.total_sell_price_fen != expected_amount_fen or quote.provider != 'WANDA'
                or any(getattr(quote, key) != getattr(order, key) for key in KEYS)):
            raise ValueError('BOUND_QUOTE_MISMATCH')
        audit = {'approval_id': approval_id, 'actor': actor, 'amount_fen': amount_fen,
                 'source_quote_id': quote.quote_id, 'source_amount_fen': order.amount_fen,
                 'confirmed_at': datetime.now(timezone.utc).isoformat(), 'fulfillment_hold': True}
        settled = deepcopy(quote)
        settled.quote_id = str(uuid5(NAMESPACE_URL, 'manual-settlement:' + order_id + ':' + approval_id))
        settled.created_at = datetime.now(timezone.utc)
        settled.status = 'CONFIRMED'
        settled.total_sell_price_fen = amount_fen
        settled.unit_sell_price_fen = amount_fen // max(1, settled.ticket_count)
        settled.metadata = dict(settled.metadata, purpose='MANUAL_SETTLEMENT',
                                bound_order_id=order_id, manual_settlement=audit)
        existing = stores.quotes.get(settled.quote_id)
        if existing and (existing.total_sell_price_fen != amount_fen
                         or existing.metadata.get('bound_order_id') != order_id
                         or existing.metadata.get('manual_settlement', {}).get('source_quote_id') != quote.quote_id):
            raise ValueError('MANUAL_APPROVAL_CONFLICT')
        if not existing:
            stores.quotes.create(settled)
        current.quote_id = settled.quote_id
        current.amount_fen = current.paid_fen = amount_fen
        current.payment_state = 'AMBIGUOUS'  # factual receipt, not fulfillment authorization
        current.shipped = observation['platform_status'] in (3, 4)
        current.payload = dict(current.payload, manual_settlement=audit,
                               platform_payment_observation=observation)
        current.touch()
        # Update redundant quote_id too; OrderStore.put historically only updates payload.
        db = stores.orders.connection
        raw = db.execute('SELECT payload FROM orders WHERE order_id=?', (order_id,)).fetchone()[0]
        from domain.order import Order
        if Order.from_dict(json.loads(raw)).to_dict() != order.to_dict():
            raise ValueError('ORDER_CHANGED_RELOAD')
        from domain.order.models import PaymentReceipt
        receipt = PaymentReceipt(tenant_id=tenant_id, event_id='manual-settlement:' + order_id + ':' + approval_id,
            order_id=order_id, amount_fen=amount_fen, state='AMBIGUOUS',
            fingerprint=hashlib.sha256(json.dumps([order_id, approval_id, amount_fen]).encode()).hexdigest(),
            payload={'source': 'STAFF_PLATFORM_READ', 'manual_settlement': audit, 'platform': observation})
        try:
            changed = db.execute('UPDATE orders SET quote_id=?,updated_at=?,payload=? WHERE order_id=? AND payload=?',
                                 (current.quote_id, current.updated_at.isoformat(),
                                  json.dumps(current.to_dict(), ensure_ascii=False), order_id, raw)).rowcount
            if changed != 1:
                raise ValueError('ORDER_CHANGED_RELOAD')
            db.execute('INSERT INTO order_payments (tenant_id,event_id,order_id,fingerprint,state,received_at,payload) VALUES (?,?,?,?,?,?,?)',
                       (tenant_id, receipt.event_id, order_id, receipt.fingerprint, receipt.state,
                        receipt.received_at.isoformat(), json.dumps(receipt.to_dict(), ensure_ascii=False)))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'ok': True, 'order_id': order_id, 'amount_fen': amount_fen,
            'fulfillment_paused': True, 'quote_id': settled.quote_id}
