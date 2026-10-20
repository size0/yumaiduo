"""One scoped demand projection; recovery and transaction permissions differ."""

from datetime import datetime, timezone

KEYS = ("tenant_id", "shop_id", "buyer_id", "chat_id", "demand_id", "demand_rev")


def scoped(conv, ctx):
    for k in KEYS:
        default = "" if k == "demand_id" else 0 if k == "demand_rev" else None
        if ctx.get(k, default) != getattr(conv, k, default):
            return False
    return True


def project(conv, ctx, memory_hours=24):
    if not scoped(conv, ctx):
        foreign = any(
            k in ctx
            and ctx[k]
            != getattr(
                conv, k, "" if k == "demand_id" else 0 if k == "demand_rev" else None
            )
            for k in KEYS
        )
        has_state = bool(
            foreign
            or ctx.get("agent_request_plan")
            or ctx.get("ticket_details")
            or ctx.get("unverified_image")
            or ctx.get("url")
            and getattr(conv, "demand_id", "")
        )
        return dict(
            blocked=has_state,
            status="STALE_REFERENCE" if has_state else "READY",
            collecting=False,
            expired=False,
            cleared=[],
        )
    plan = ctx.get("agent_request_plan") or {}
    active = bool(
        plan.get("pending") is True
        and scoped(conv, plan)
        and plan.get("source_image") == ctx.get("url")
    )
    details = ctx.get("ticket_details") or {}
    valid_details = all(
        k not in details
        or details[k]
        == getattr(
            conv, k, "" if k == "demand_id" else 0 if k == "demand_rev" else None
        )
        for k in KEYS
    )
    valid_details = valid_details and (
        not details.get("source_image") or details["source_image"] == ctx.get("url")
    )
    cleared = list(details.get("cleared_fields") or []) if valid_details else []
    awaiting = ctx.get("awaiting_image_request") or {}
    preimage_pending = bool(
        not ctx.get("url") and awaiting
        and all(k not in awaiting or awaiting[k] == ctx.get(k) for k in KEYS)
    )
    pending = bool(valid_details and (details.get("pending") or cleared) or preimage_pending)
    expired = False
    # This is the lifetime of an unfinished request, not a new quote TTL.
    # Completed Wanda quotes retain their existing show-based validity rules.
    if active or pending:
        try:
            limit = (
                min(memory_hours, plan.get("memory_hours", 24))
                if active
                else memory_hours
            )
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(
                    ctx.get("request_revalidated_at") or ctx["created_at"]
                )
            ).total_seconds()
            expired = not 0 <= age <= max(1, min(24, limit)) * 3600
        except (KeyError, TypeError, ValueError):
            expired = True
    blocked = bool(active or pending or expired or ctx.get("unverified_image"))
    return dict(
        blocked=blocked,
        expired=expired,
        collecting=active and not expired,
        cleared=cleared,
        status="REQUEST_REVALIDATION_REQUIRED"
        if expired
        else "REQUEST_FIELDS_INCOMPLETE"
        if blocked
        else "READY",
    )


def pause(tool):
    if tool.service.quote_store:
        tool.service.quote_store.supersede_for_identity(
            tool.identity, demand_id=getattr(tool.conv, "demand_id", "") or None
        )
    tool.conv.quote = None
    tool.conv.quote_id = ""
    tool.conv.quote_kind = ""
    tool.conv.quote_comparison = {}


def clarify(tool, evidence, invalidated_fields=None):
    from .ticket_details import respond
    from .request_plan import FIELDS

    if evidence != tool.text or getattr(tool, "semantic_intent", None) != "revise":
        return dict(ok=False, status="INVALID_FIELD_EVIDENCE")
    fields = (
        invalidated_fields
        if invalidated_fields is not None
        else ["movie", "date", "time", "seats", "position_preference"]
    )
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(k, str) or k not in FIELDS for k in fields)
    ):
        return dict(ok=False, status="INVALID_FIELD_EVIDENCE")
    try:
        from .buyer_assistant import CLOSED_STATES
        from .purchase_continuation import relevant_orders

        if any(
            o.state not in CLOSED_STATES
            and (
                o.payment_state != "UNPAID"
                or (o.payload or {}).get("quote_revision_pending")
                or o.state == "REPRICE_PENDING"
            )
            for o in relevant_orders(tool)
        ):
            return dict(ok=False, status="PAID_ORDER_LOCKED")
    except Exception:
        return dict(ok=False, status="QUERY_UNKNOWN")
    ctx = tool.session.image_context
    if not scoped(tool.conv, ctx):
        return dict(ok=False, status="STALE_REFERENCE")
    invalid = set(fields)
    if invalid & {"cinema", "city"}:
        invalid.update(("movie", "date", "time", "seats", "position_preference"))
    if invalid & {"movie", "date", "time"}:
        invalid.update(("time", "seats", "position_preference"))
    if "seats" in invalid:
        invalid.add("position_preference")
    saved = ctx.setdefault("ticket_details", {})
    invalid.update(saved.get("cleared_fields") or [])
    for name in ("ticket_details", "awaiting_image_request"):
        for key in invalid:
            (ctx.get(name) or {}).pop(key, None)
    saved.update(
        pending=True,
        awaiting="correction",
        correction_evidence=evidence,
        cleared_fields=sorted(invalid),
        source_image=ctx.get("url"),
        demand_id=getattr(tool.conv, "demand_id", ""),
        demand_rev=getattr(tool.conv, "demand_rev", 0),
    )
    ctx.pop("agent_request_plan", None)
    tool.request_plan = None
    pause(tool)
    return respond(
        tool,
        "已暂停原报价并撤回待更正的信息，请补充本次需要的位置或场次；原订单未修改。",
    )
