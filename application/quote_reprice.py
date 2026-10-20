"""Replace a unique unpaid order with the buyer's new screenshot quote."""
import json
import uuid
from datetime import datetime, timezone

from integrations.yumaiduo.commands import Command
from domain.order import Order

IDENTITY = ("tenant_id", "shop_id", "buyer_id", "chat_id")


def revision_conversation_matches(order, conv) -> bool:
    """Check a detached conversation snapshot against the committed order."""
    from .ticket_flow import _seat_names_of
    result = getattr(conv, "recognition", None)
    start = str(getattr(result, "start_time", "") or "")
    return (getattr(conv, "quote_id", "") == order.quote_id
            and getattr(getattr(conv, "quote", None), "total_price_fen", None) == order.amount_fen
            and all(getattr(conv, k, None) == getattr(order, k) for k in IDENTITY)
            and getattr(result, "cinema_name", None) == order.cinema
            and getattr(result, "movie_name", None) == order.movie
            and f"{start[:10]} {start[11:16]}".strip() == order.show
            and _seat_names_of(getattr(conv, "seats", [])) == order.seats
            and (len(order.seats) == order.ticket_count or (
                order.provider == "WANDA" and not order.seats
                and getattr(conv, "quote_kind", "") == "ORDER_TOTAL"
                and getattr(result, "expected_seat_count", None) == order.ticket_count)))


def _replaceable(order):
    return (order.state in ("CREATED", "AWAITING_PAYMENT", "REPRICE_PENDING")
            and order.payment_state == "UNPAID" and order.paid_fen == 0
            and not order.shipped and not order.ticket_codes
            and ((order.payload or {}).get("upstream_created") is False
                 or (order.provider == "WANDA"
                     and (order.payload or {}).get("manual_ticketing") is True
                     and not (order.payload or {}).get("upstream_created")))
            and not (order.payload or {}).get("freeze_amount_fen")
            and not order.failure_code
            and bool(order.provider_order_id))


def _route_allowed(order, quote, allow_wanda_switch=False):
    return (order.provider == quote.provider or (
        allow_wanda_switch and order.provider == "LIANGPIAO" and quote.provider == "WANDA"
        and _replaceable(order) and order.payload.get("upstream_created") is False))


def build_quote_reprice(stores, ident: dict, quote_id: str, *, allow_wanda_switch=False, repair_bound=False,
                        expected_order_no=None) -> list[Command]:
    """Plan only; platform status is checked by the normal execution gate."""
    if not all(ident.get(k) for k in IDENTITY) or not stores.orders or not stores.quotes:
        return []
    quote = stores.quotes.get(quote_id)
    if (quote is None or not quote.is_order_ready()
            or quote.metadata.get("price_mode") != "FIXED"
            or type(quote.total_sell_price_fen) is not int or quote.total_sell_price_fen <= 0
            or any(getattr(quote, k) != ident[k] for k in IDENTITY)):
        return []
    orders = [o for o in stores.orders.by_identity(ident) if _replaceable(o)]
    if len(orders) != 1:
        return []
    order = orders[0]
    if quote.metadata.get('requested_order_no') and quote.metadata['requested_order_no'] != order.provider_order_id:
        return []
    if expected_order_no is not None and order.provider_order_id != expected_order_no:
        return []
    if ((order.quote_id == quote_id and not repair_bound) or not _route_allowed(order, quote, allow_wanda_switch)
            or (order.payload or {}).get("quote_revision_pending")):
        return []
    return [Command(
        command_id=str(uuid.uuid4()), type="CHANGE_ORDER_PRICE",
        event_id=order.provider_order_id, session_id=ident["chat_id"],
        dedupe_key=f"reprice:{order.provider_order_id}:quote:{quote_id}",
        extra={"order_id": order.provider_order_id,
               "target_amount_fen": quote.total_sell_price_fen,
               "demand_id": quote.demand_id, "demand_rev": quote.demand_rev,
               "expected_order_no": expected_order_no or order.provider_order_id,
               "replacement_quote_id": quote_id, "previous_quote_id": order.quote_id,
               "allow_wanda_switch": allow_wanda_switch},
        **{k: ident[k] for k in IDENTITY})]


def authorize_quote_revision(stores, command) -> None:
    """Reserve one revision, keeping the old binding until confirmed success.

    Called only after the plugin has read the platform order as unpaid. A CAS
    protects against a concurrent payment update. Unknown results remain pending.
    """
    ident = {k: getattr(command, k) for k in IDENTITY}
    qid = command.extra["replacement_quote_id"]
    if command.extra.get('expected_order_no', command.event_id) != command.event_id:
        raise ValueError('REPLACEMENT_ORDER_REFERENCE_MISMATCH')
    quote = stores.quotes.get(qid)
    if quote and quote.demand_id:
        from .demand import version_matches
        if not version_matches(quote, command.extra.get('demand_id'), command.extra.get('demand_rev')):
            raise ValueError('REPLACEMENT_DEMAND_VERSION_MISMATCH')
    latest = stores.quotes.by_identity(ident)
    if (not latest or latest[0].quote_id != qid or quote is None
            or not quote.is_order_ready()
            or quote.metadata.get("price_mode") != "FIXED"
            or quote.total_sell_price_fen != command.extra.get("target_amount_fen")
            or any(getattr(quote, k) != ident[k] for k in IDENTITY)):
        raise ValueError("REPLACEMENT_QUOTE_STALE_OR_MISMATCH")
    store = stores.orders
    with store.lock:
        from domain.order.models import TERMINAL_STATES
        active = [o for o in store.by_identity(ident)
                  if o.state not in TERMINAL_STATES and o.state != "CLOSED"]
        if len(active) != 1:
            raise ValueError("REPLACEMENT_ORDER_AMBIGUOUS")
        row = store.connection.execute(
            "SELECT payload FROM orders WHERE provider_order_id=?", (command.event_id,)).fetchone()
        raw = row[0] if row else None
        order = Order.from_dict(json.loads(raw)) if raw else None
        if (order is None or not _replaceable(order)
                or any(getattr(order, k) != ident[k] for k in IDENTITY)
                or not _route_allowed(order, quote, command.extra.get("allow_wanda_switch") is True)):
            raise ValueError("REPLACEMENT_ORDER_NOT_UNPAID")
        pending = (order.payload or {}).get("quote_revision_pending")
        if pending:
            if pending.get("command_id") != command.command_id:
                raise ValueError("REPLACEMENT_ALREADY_IN_PROGRESS")
            return
        if order.quote_id != command.extra.get("previous_quote_id"):
            raise ValueError("REPLACEMENT_ORDER_CHANGED")
        order.payload = dict(order.payload or {})
        order.payload["quote_revision_pending"] = {
            "command_id": command.command_id, "quote_id": qid,
            "demand_id": quote.demand_id, "demand_rev": quote.demand_rev,
            "previous_quote_id": order.quote_id, "target_amount_fen": quote.total_sell_price_fen}
        order.state = "REPRICE_PENDING"
        order.updated_at = datetime.now(timezone.utc)
        try:
            cur = store.connection.execute(
                "UPDATE orders SET state=?, updated_at=?, payload=? WHERE order_id=? AND payload=?",
                (order.state, order.updated_at.isoformat(), json.dumps(order.to_dict(), ensure_ascii=False),
                 order.order_id, raw))
            if cur.rowcount != 1:
                raise ValueError("REPLACEMENT_ORDER_CHANGED")
            store.connection.commit()
        except Exception:
            store.connection.rollback()
            raise


def settle_quote_revision(stores, command) -> None:
    """Commit exactly the reserved revision after a verified price receipt."""
    if (command.status != "SUCCEEDED"
            or command.extra.get("price_after_fen") != command.extra.get("target_amount_fen")):
        raise ValueError("REPLACEMENT_RESULT_UNVERIFIED")
    quote = stores.quotes.get(command.extra["replacement_quote_id"])
    if quote is None:
        raise ValueError("REPLACEMENT_QUOTE_MISSING")
    store = stores.orders
    with store.lock:
        row = store.connection.execute(
            "SELECT payload FROM orders WHERE provider_order_id=?", (command.event_id,)).fetchone()
        raw = row[0] if row else None
        order = Order.from_dict(json.loads(raw)) if raw else None
        if order is None or any(getattr(order, k) != getattr(command, k) for k in IDENTITY):
            raise ValueError("REPLACEMENT_RECEIPT_IDENTITY_MISMATCH")
        if order.state in ('CANCELLED', 'REFUNDED', 'DELIVERED'):
            raise ValueError('REPLACEMENT_ORDER_TERMINAL')
        if any(r.get("command_id") == command.command_id
               for r in order.payload.get("quote_revisions", [])):
            return
        pending = (order.payload or {}).get("quote_revision_pending")
        if not pending:
            if order.quote_id == quote.quote_id and order.amount_fen == quote.total_sell_price_fen:
                return
            raise ValueError("REPLACEMENT_RECEIPT_STALE")
        if (pending.get("command_id") != command.command_id
                or pending.get("quote_id") != quote.quote_id
                or quote.total_sell_price_fen != command.extra["price_after_fen"]):
            raise ValueError("REPLACEMENT_RECEIPT_STALE")
        history = list(order.payload.get("quote_revisions") or [])
        history.append({"quote_id": order.quote_id, "amount_fen": order.amount_fen,
                        "demand_id": order.demand_id, "demand_rev": order.demand_rev,
                        "movie": order.movie, "show": order.show, "seats": list(order.seats),
                        "command_id": command.command_id})
        order.payload["quote_revisions"] = history
        del order.payload["quote_revision_pending"]
        order.provider = quote.provider
        if quote.provider == "WANDA":
            order.payload.update(manual_ticketing=True, upstream_created=False, pickup_url="")
        order.quote_id = quote.quote_id
        order.demand_id, order.demand_rev = quote.demand_id, quote.demand_rev
        order.cinema, order.movie, order.show = quote.cinema, quote.movie, quote.show
        order.seats = list(quote.selected_seats)
        order.ticket_count = quote.ticket_count
        order.amount_fen = quote.total_sell_price_fen
        order.payload["quote_binding"] = {"quote_id": quote.quote_id,
                                            "demand_id": quote.demand_id, "demand_rev": quote.demand_rev,
                                           "amount_fen": order.amount_fen,
                                           "ticket_count": order.ticket_count}
        order.payload["price_mode"] = quote.metadata.get("price_mode")
        order.payload["ticket_mode"] = quote.metadata.get("ticket_mode")
        order.state = "AWAITING_PAYMENT"
        order.updated_at = datetime.now(timezone.utc)
        try:
            cur = store.connection.execute(
                "UPDATE orders SET quote_id=?, state=?, updated_at=?, payload=? "
                "WHERE order_id=? AND payload=?",
                (order.quote_id, order.state, order.updated_at.isoformat(),
                 json.dumps(order.to_dict(), ensure_ascii=False), order.order_id, raw))
            if cur.rowcount != 1:
                raise ValueError("REPLACEMENT_ORDER_CHANGED")
            store.connection.commit()
        except Exception:
            store.connection.rollback()
            raise
