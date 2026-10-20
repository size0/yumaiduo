"""Immutable demand snapshots attached to quotes; money stays in the quote engine."""
from copy import deepcopy
from uuid import uuid4

IDENTITY = ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')


def version_matches(quote, demand_id, demand_rev):
    return bool(quote and demand_id and type(demand_rev) is int and demand_rev > 0
                and quote.demand_id == demand_id and quote.demand_rev == demand_rev)


def bind_quote(conv, quote, store):
    if any(str(getattr(conv, k, '') or '') != str(getattr(quote, k, '')) for k in IDENTITY):
        raise ValueError('DEMAND_IDENTITY_MISMATCH')
    ident = {k: getattr(quote, k) for k in IDENTITY}
    demand_id = getattr(conv, 'demand_id', '') or str(uuid4())
    previous = [q for q in store.by_identity(ident) if q.demand_id == demand_id]
    snapshot = {
        'cinema': quote.cinema, 'movie': quote.movie, 'show': quote.show,
        'provider_identity': deepcopy(quote.metadata.get('provider_identity', {})),
        'ticket_count': quote.ticket_count, 'seats': deepcopy(quote.selected_seats),
        'quote_kind': quote.metadata.get('quote_kind', 'ORDER_TOTAL'),
    }
    last = max(previous, key=lambda q: q.demand_rev) if previous else None
    rev = (last.demand_rev + (last.metadata.get('demand_snapshot') != snapshot)) if last else 1
    quote.demand_id, quote.demand_rev = demand_id, rev
    quote.metadata['demand_snapshot'] = snapshot
    quote.metadata['inquiry_only'] = bool(getattr(conv, 'demand_inquiry', False))
    quote.metadata['requested_order_no'] = str(getattr(conv, 'requested_order_no', '') or '')
    conv.demand_id, conv.demand_rev = demand_id, rev
    # Previous order records retain their quote_id and snapshots. Supersession
    # prevents reusing obsolete offers; it never mutates a placed order.
    for old in previous:
        if old.status in ('READY', 'CONFIRMED'):
            store.update_status(old.quote_id, 'SUPERSEDED')


def reference(quote):
    return {'quote_id': quote.quote_id, 'demand_id': quote.demand_id,
            'demand_rev': quote.demand_rev, 'target_amount_fen': quote.total_sell_price_fen}
