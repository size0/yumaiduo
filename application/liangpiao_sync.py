"""Durable supplier callbacks. Supplier refunds never imply a buyer refund."""
import hashlib
import json
import time
from datetime import datetime, timezone

from integrations.liangpiao import callbacks as cb


def _schema(queue):
    queue.connection.execute('''CREATE TABLE IF NOT EXISTS liangpiao_callbacks (
        event_key TEXT PRIMARY KEY, digest TEXT NOT NULL, raw TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'RECEIVED', error TEXT NOT NULL DEFAULT '',
        received_at TEXT NOT NULL, processed_at TEXT)''')
    queue.connection.commit()


def receive(raw, headers, app_key, app_secret, queue, *, now=None):
    cb.verify(app_key, app_secret, headers, raw, now=int(time.time()) if now is None else now)
    event = cb.parse(raw)
    if not event.order_no or event.order_no == 'None':
        raise ValueError('CALLBACK_ORDER_REQUIRED')
    if event.is_ticket_event and (type(event.data.get('version', 0)) is not int
                                  or event.version < 0):
        raise ValueError('CALLBACK_VERSION_INVALID')
    if event.is_ticket_event:
        tickets = event.data.get('tickets') or []
        seats = event.data.get('actualSeats') or []
        if (not isinstance(tickets, list) or any(not isinstance(t, dict) for t in tickets)
                or not isinstance(seats, list) or any(not isinstance(s, str) for s in seats)):
            raise ValueError('CALLBACK_TICKETS_INVALID')
    digest = hashlib.sha256(json.dumps(event.data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with queue.lock:
        _schema(queue)
        row = queue.connection.execute('SELECT digest FROM liangpiao_callbacks WHERE event_key=?',
                                       (event.dedupe_key,)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError('CALLBACK_CONTENT_CONFLICT')
            return cb.ACK_BODY
        queue.connection.execute('INSERT INTO liangpiao_callbacks(event_key,digest,raw,received_at) VALUES(?,?,?,?)',
                                 (event.dedupe_key, digest, raw, datetime.now(timezone.utc).isoformat()))
        queue.connection.commit()
    return cb.ACK_BODY


def apply(event, orders):
    # Resolve only the bound supplier identifier, never a buyer/session guess.
    matches = orders.connection.execute('SELECT order_id,payload FROM orders WHERE provider_order_id=?',
                                        (event.order_no,)).fetchall()
    matches = [row for row in matches if json.loads(row[1]).get('provider') == 'LIANGPIAO']
    if not matches:
        raise ValueError('CALLBACK_ORDER_NOT_BOUND')
    if len(matches) != 1:
        raise ValueError('CALLBACK_ORDER_AMBIGUOUS')
    order = orders.get(matches[0][0])
    expected = str(order.payload.get('out_order_no') or '')
    if event.out_order_no and (not expected or event.out_order_no != expected):
        raise ValueError('CALLBACK_OUT_ORDER_MISMATCH')
    payload = dict(order.payload)
    payload['liangpiao_last_event'] = event.event
    payload['liangpiao_callback_at'] = datetime.now(timezone.utc).isoformat()
    if event.is_ticket_event:
        version = event.version
        previous = int(payload.get('liangpiao_ticket_version', -1))
        if version < previous:
            return
        codes = event.ticket_codes
        if not codes:
            # Version zero can precede the actual ticket.updated receipt.
            payload['liangpiao_waiting_ticket_codes'] = True
        else:
            if version == previous and order.ticket_codes != codes:
                raise ValueError('CALLBACK_TICKET_VERSION_CONFLICT')
            if order.state in ('CANCELLED', 'REFUNDED', 'FAILED'):
                raise ValueError('CALLBACK_TERMINAL_CONFLICT')
            order.ticket_codes = codes
            if event.actual_seats:
                order.seats = event.actual_seats
            payload['liangpiao_ticket_version'] = version
            payload['liangpiao_waiting_ticket_codes'] = False
            payload['liangpiao_ticket_link'] = str(event.data.get('ticketLink') or '')
            if version > previous or order.state != 'DELIVERED':
                order.state = 'TICKETED'
    elif event.event == cb.EVENT_FAILED:
        if order.ticket_codes or order.state == 'DELIVERED':
            raise ValueError('CALLBACK_FAILED_AFTER_TICKET')
        order.state = 'CANCELLED' if event.is_cancelled else 'FAILED'
        order.failure_code = event.fail_code or 'SUPPLIER_CANCELLED'
        payload['ticket_failed'] = {'fail_code': event.fail_code,
            'fail_reason': event.data.get('failReason'), 'loss_fen': event.loss_amount_fen,
            'refund_fen': event.refund_amount_fen, 'cancelled': event.is_cancelled}
        payload['buyer_refund_required'] = order.payment_state == 'CONFIRMED'
    elif event.event == cb.EVENT_REFUNDED:
        # Upstream wallet refund is not proof of a refund on Xianyu.
        payload['supplier_refund'] = event.data
        payload['buyer_refund_required'] = order.payment_state == 'CONFIRMED'
        order.state = 'FAILED'
        order.failure_code = 'SUPPLIER_REFUNDED_BUYER_REFUND_UNVERIFIED'
    elif event.event == cb.EVENT_SETTLED:
        payload['supplier_settlement'] = event.data
    elif event.event == cb.EVENT_REFUND_REJECTED:
        payload['supplier_refund_rejected'] = event.data
    else:
        payload['liangpiao_unhandled_event'] = event.event
    order.payload = payload
    orders.put(order)


def process(queue, orders, *, limit=50):
    applied = 0
    with queue.lock:
        _schema(queue)
        rows = queue.connection.execute("SELECT event_key,raw FROM liangpiao_callbacks WHERE state='RECEIVED' ORDER BY COALESCE(processed_at,received_at) LIMIT ?", (limit,)).fetchall()
        for key, raw in rows:
            try:
                with orders.lock:
                    apply(cb.parse(raw), orders)
                # If interrupted between stores, replay is deterministic.
                queue.connection.execute("UPDATE liangpiao_callbacks SET state='APPLIED',error='',processed_at=? WHERE event_key=?", (datetime.now(timezone.utc).isoformat(), key))
                applied += 1
            except (ValueError, RuntimeError) as exc:
                # A binding can arrive after a fast callback: keep that retryable.
                reason = str(exc)
                state = 'RECEIVED' if reason == 'CALLBACK_ORDER_NOT_BOUND' else 'ATTENTION'
                queue.connection.execute('UPDATE liangpiao_callbacks SET state=?,error=?,processed_at=? WHERE event_key=?', (state, reason, datetime.now(timezone.utc).isoformat(), key))
            queue.connection.commit()
    return applied
