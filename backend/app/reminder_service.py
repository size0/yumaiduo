from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .quote_record_store import QuoteRecordStore
from .reminder_store import ReminderStore
from .reply_template_store import ReplyTemplates


def _text(source: object, *names: str) -> str | None:
    if not isinstance(source, Mapping):
        return None
    for name in names:
        value = str(source.get(name) or "").strip()
        if value:
            return value
    return None


def plan_shipped_order_reminders(
    body: Mapping[str, Any], *, quote_store: QuoteRecordStore, reminder_store: ReminderStore,
    reply_templates: ReplyTemplates | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    envelope = body.get("envelope") if isinstance(body.get("envelope"), Mapping) else {}
    if _text(envelope, "event") != "order.shipped":
        return []
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    session = body.get("session") if isinstance(body.get("session"), Mapping) else {}
    order = body.get("order") if isinstance(body.get("order"), Mapping) else {}
    status = (_text(order, "orderStatus", "order_status", "status") or "").lower()
    if status not in {"3", "shipped", "已发货"}:
        return []
    bindings = {
        "tenant_id": _text(envelope, "tenantId", "tenant_id"),
        "order_id": _text(order, "orderId", "order_id") or _text(payload, "orderId", "order_id"),
        "shop_id": _text(session, "accountUnb", "account_unb") or _text(order, "accountUnb", "account_unb") or _text(payload, "accountUnb", "account_unb"),
        "buyer_id": _text(session, "peerUnb", "peer_unb") or _text(order, "buyerUnb", "buyer_unb"),
        "chat_id": _text(session, "chatId", "chat_id") or _text(order, "chatId", "chat_id"),
    }
    if any(not value for value in bindings.values()):
        return []
    record = quote_store.find_by_order(**bindings)
    if not isinstance(record, Mapping):
        return []
    facts = {
        **bindings,
        "movie": record.get("movie"), "cinema": record.get("cinema"),
        "quote_date": record.get("quote_date"), "showtime_start": record.get("showtime_start"),
        "showtime_end": record.get("showtime_end"), "hall": record.get("hall"),
        "seat_display": record.get("seat_display"),
    }
    try:
        plan_kwargs: dict[str, Any] = {"now": (now or datetime.now(timezone.utc))}
        if reply_templates is not None:
            plan_kwargs["pre_show_template"] = reply_templates.movie_reminder_template
        return reminder_store.plan_order(facts, **plan_kwargs)
    except ValueError:
        return []
