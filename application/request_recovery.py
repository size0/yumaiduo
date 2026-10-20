"""Validate declared evidence and renew context with read-only catalogue facts."""

from datetime import datetime, timezone, timedelta


def validate_proof(key, literal, text):
    if not isinstance(literal, str) or not literal or literal not in text:
        return False
    try:
        from .ticket_details import parse_date, parse_time, parse_seats

        if key == "date":
            parse_date(literal)
        elif key == "time":
            if literal not in ("上午", "早上", "下午", "晚上"):
                parse_time(literal, evidence=text)
        elif key == "seats":
            parse_seats(literal)
        elif key == "ticket_count":
            from .semantic_request import number

            value = number(literal)
            if value is None or not 1 <= value <= 99:
                return False
    except (TypeError, ValueError):
        return False
    return True


def same_value(key, proof, value, text):
    try:
        from .ticket_details import parse_date, parse_time, parse_seats

        if key == "ticket_count":
            from .semantic_request import number

            return number(proof) == value
        if key == "date":
            return parse_date(proof) == parse_date(value)
        if key == "time":
            if proof in ("上午", "早上", "下午", "晚上"):
                return proof == value
            return parse_time(proof, evidence=text) == parse_time(value, evidence=text)
        if key == "seats":
            return parse_seats(proof) == parse_seats(value)
        return proof == value
    except (TypeError, ValueError):
        return False


def freeze(key, literal, evidence, known_clock=None):
    """Resolve relative date/period once, while keeping literal evidence separate."""
    from .ticket_details import parse_date, parse_time

    if key == "date":
        return parse_date(literal)
    if literal in ("上午", "早上", "下午", "晚上"):
        if not known_clock:
            raise ValueError("no scoped ambiguous clock")
        clock, ambiguous = parse_time(known_clock)
        if not ambiguous:
            raise ValueError("clock does not need a period")
        literal = literal + clock
    clock, ambiguous = parse_time(literal, evidence=evidence)
    if ambiguous:
        return literal
    return ("上午" if int(clock[:2]) < 12 else "下午") + clock


def refresh(tool):
    from .request_state import scoped, pause
    from .ticket_details import CHINA
    from .cinema_matching import title_key
    from .buyer_assistant import CLOSED_STATES

    ctx = tool.session.image_context
    if (getattr(tool, "semantic_intent", None) == "revise"
            and scoped(tool.conv, ctx) and not ctx.get("url")):
        return dict(
            ok=False, status="REQUEST_REVALIDATION_REQUIRED",
            reason="先前文字需求已超过核验时效，当前没有图片可重新核验。请发本次影院场次截图，或明确开始新需求；旧报价不能用于付款。",
            next_actions=["发送当前场次截图，或明确建立新需求；不要重复恢复不存在的图片。"],
        )
    if (
        getattr(tool, "semantic_intent", None) != "revise"
        or not scoped(tool.conv, ctx)
        or not ctx.get("url")
        or ctx.get("unverified_image")
    ):
        return dict(ok=False, status="STALE_REFERENCE")
    if not tool.cfg.recognize_quote.auto_recognize:
        return dict(ok=False, status="REQUEST_REVALIDATION_REQUIRED")
    from domain.conversation import ConvState

    allowed = {
        ConvState.IDLE,
        ConvState.RECOGNIZING,
        ConvState.QUOTED,
        ConvState.QT_FAIL,
        ConvState.REWORK,
        ConvState.PICK_CINEMA,
        ConvState.PICK_MOVIE,
        ConvState.PICK_SHOW,
        ConvState.AWAIT_PAY,
        ConvState.AWAIT_REPRICE,
        ConvState.SELF_BUY,
    }
    if tool.conv.state not in allowed:
        return dict(ok=False, status="ORDER_STATE_LOCKED")
    try:
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
    facts = ctx.get("recognition_facts") or {}
    pause(tool)  # An old READY quote must never revive when the context is renewed.
    try:
        stamp = datetime.fromisoformat(
            str(facts.get("start_time") or "").replace("Z", "+00:00")
        )
        stamp = (
            stamp.replace(tzinfo=CHINA)
            if stamp.tzinfo is None
            else stamp.astimezone(CHINA)
        )
        if (
            facts.get("match_level") != "EXACT"
            or not facts.get("cinema_id")
            or stamp <= datetime.now(CHINA)
        ):
            raise ValueError("stale show")
        day = stamp.date() - timedelta(days=1) if stamp.hour < 8 else stamp.date()
        matches = []
        for row in tool.service.flow.api.ch4_show_list(
            facts["cinema_id"], day.isoformat()
        ):
            try:
                dt = datetime.fromisoformat(
                    str(row.get("startTime") or "").replace("Z", "+00:00")
                )
                dt = (
                    dt.replace(tzinfo=CHINA)
                    if dt.tzinfo is None
                    else dt.astimezone(CHINA)
                )
                if dt == stamp and title_key(row.get("movieName")) == title_key(
                    facts.get("movie_name")
                ):
                    matches.append(row)
            except (TypeError, ValueError):
                continue
        if len(matches) != 1:
            raise ValueError("show not unique")
    except Exception:
        return dict(
            ok=False,
            status="REQUEST_REVALIDATION_REQUIRED",
            reason="旧图片的当前排片尚未重新核实，请发最新场次图；旧报价已暂停。",
        )
    ctx["request_revalidated_at"] = datetime.now(timezone.utc).isoformat()
    tool.image_snapshot = dict(ctx)
    return dict(
        ok=True,
        status="REQUEST_CONTEXT_REVALIDATED",
        handled=False,
        reason="仅重新核对了图片对应的当前排片；旧报价已作废，已撤回字段未恢复，仍需合并本次字段后重新核价。",
        next_actions=["继续提交已声明的字段；尚无新报价，不得改价或引导付款。"],
    )
