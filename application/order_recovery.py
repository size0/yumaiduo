"""Recover Wanda platform bindings from an exact order card and immutable quote."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL

KEYS = ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')


def recover_identity(event, ident, stores, service):
    number = str((event.get('payload') or {}).get('orderId') or '')
    if not number or not ident.get('tenant_id') or not ident.get('shop_id'):
        return None
    matches = []
    order = stores.orders.find_by_provider_order(number)
    if order:
        matches.append({k: str(getattr(order, k)) for k in KEYS})
    for session in getattr(service, 'sessions', {}).values():
        card = getattr(session, 'pending_order_card', {}) or {}
        if str((card.get('payload') or {}).get('orderId') or '') == number:
            matches.append({k: str(getattr(session.conversation, k, '')) for k in KEYS})
    matches = [m for m in matches if all(m.values()) and all(not ident.get(k) or ident[k] == m[k] for k in KEYS)]
    unique = {tuple(m[k] for k in KEYS) for m in matches}
    return dict(zip(KEYS, next(iter(unique)))) if len(unique) == 1 else None


def bind_wanda_card(session, stores, ident, detail, *, paid=False):
    with stores.orders.lock:
        return _bind_wanda_card_locked(session, stores, ident, detail, paid=paid)


def _bind_wanda_card_locked(session, stores, ident, detail, *, paid=False):
    """The actual platform purchase confirms this demand, not arbitrary chat text."""
    if not isinstance(detail, dict):
        return None
    card = getattr(session, 'pending_order_card', {}) or {}
    number = str((card.get('payload') or {}).get('orderId') or '')
    if (not number or str(detail.get('orderId')) != number
            or str(detail.get('accountUnb')) != ident['shop_id']
            or any(str(getattr(session.conversation, k, '')) != ident[k] for k in KEYS)):
        return None
    if detail.get('grouponPending') is not False:
        return None
    if paid:
        if type(detail.get('orderStatus')) is not int or detail['orderStatus'] != 2 or not detail.get('payTime'):
            return None
    elif detail.get('orderStatus') != 1 or detail.get('payTime'):
        return None
    existing = stores.orders.find_by_provider_order(number)
    if existing:
        return existing if all(str(getattr(existing,k)) == ident[k] for k in KEYS) else None
    if any(o.state not in ('DELIVERED','CANCELLED','REFUNDED','FAILED','CLOSED') for o in stores.orders.by_identity(ident)):
        return None
    quotes = stores.quotes.by_identity(ident)
    q = quotes[0] if quotes else None
    conv = session.conversation
    ctx = session.image_context or {}
    from .request_plan import has_pending
    if (not q or q.provider != 'WANDA' or not q.is_usable()
            or has_pending(conv,ctx)
            or not ctx.get('verified') or ctx.get('unverified_image')
            or (ctx.get('ticket_details') or {}).get('pending')
            or q.demand_id != getattr(conv,'demand_id','')
            or q.demand_rev != getattr(conv,'demand_rev',0)):
        return None
    try:
        created = datetime.fromisoformat(detail['createTime'].replace('Z','+00:00'))
        if q.created_at > created or (paid and int(detail['payment']) != q.total_sell_price_fen):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    accepted = deepcopy(q)
    accepted.metadata = dict(q.metadata, inquiry_only=False, accepted_platform_order=number, source_quote_id=q.quote_id)
    if not accepted.is_order_ready():
        return None
    # Preserve the inquiry snapshot; acceptance gets its own immutable quote.
    if q.metadata.get('inquiry_only'):
        accepted.created_at = datetime.now(timezone.utc)
        accepted.quote_id = str(uuid5(NAMESPACE_URL, ':'.join(('accepted',ident['tenant_id'],ident['shop_id'],number,q.quote_id))))
        if not stores.quotes.get(accepted.quote_id):
            stores.quotes.create(accepted)
    from domain.order import Order, idempotency_key_for
    key = ':'.join(('platform-card',ident['tenant_id'],ident['shop_id'],number))
    order = Order(order_id=str(uuid5(NAMESPACE_URL,key)), quote_id=accepted.quote_id,
        demand_id=q.demand_id, demand_rev=q.demand_rev, **ident, provider='WANDA',
        provider_order_id=number, cinema=q.cinema, movie=q.movie, show=q.show,
        seats=list(q.selected_seats), ticket_count=q.ticket_count, amount_fen=q.total_sell_price_fen,
        state='AWAITING_PAYMENT', payment_state='UNPAID', idempotency_key=idempotency_key_for(accepted.quote_id, provider='WANDA'),
        payload={'manual_ticketing':True,'upstream_created':False,'quote_channel':'WANDA',
                 'price_mode':q.metadata.get('price_mode','FIXED'),'recovered_from_order_card':True})
    stores.orders.create(order)
    conv.demand_inquiry = False
    conv.quote_id = accepted.quote_id
    ctx['inquiry_only'] = False
    return order


def payment_conversation(order, quotes):
    """Build payment-only context from the bound quote; never from a newer inquiry."""
    from domain.conversation import Conversation, ConvState
    q = quotes.get(order.quote_id)
    if (not q or q.provider != 'WANDA' or q.total_sell_price_fen != order.amount_fen
            or any(str(getattr(q,k)) != str(getattr(order,k)) for k in KEYS)):
        raise ValueError('BOUND_QUOTE_MISMATCH')
    c = Conversation(**{k:getattr(order,k) for k in KEYS},channel='wanda',state=ConvState.AWAIT_PAY)
    c.quote_channel='WANDA';c.quote_kind='ORDER_TOTAL';c.quote_id=q.quote_id
    c.demand_id=q.demand_id;c.demand_rev=q.demand_rev
    c.quote=SimpleNamespace(total_price_fen=q.total_sell_price_fen,seat_count=q.ticket_count)
    c.recognition=SimpleNamespace(cinema_name=q.cinema,movie_name=q.movie,start_time=q.show.replace(' ', 'T'),expected_seat_count=q.ticket_count)
    c.seats=[{'seatNo':s} for s in q.selected_seats]
    return c


def verified_payment(order, detail):
    if not isinstance(detail, dict):
        return False
    try:
        paid_at = datetime.fromisoformat(str(detail['payTime']).replace('Z','+00:00'))
        return (str(detail.get('orderId')) == order.provider_order_id
                and str(detail.get('accountUnb')) == order.shop_id
                and type(detail.get('orderStatus')) is int and detail['orderStatus'] == 2
                and detail.get('grouponPending') is False
                and int(str(detail['payment'])) == order.amount_fen > 0
                and paid_at.tzinfo is not None)
    except (KeyError, TypeError, ValueError):
        return False
