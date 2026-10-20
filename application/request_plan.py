"""One declared field set, one merge, one quote. Never authorize transactions."""

from copy import deepcopy
from datetime import datetime, timezone

EDITORS = {"update_ticket_request", "supplement_ticket_details", "supplement_city"}
FIELDS = {
    "ticket_count",
    "seats",
    "movie",
    "date",
    "time",
    "cinema",
    "city",
    "position_preference",
}
MONEY = {
    "request_latest_quote_reprice",
    "confirm_inquiry_purchase",
    "confirm_inquiry_replacement",
}
REWRITERS = {
    "recover_image_context",
    "confirm_previous_image",
    "confirm_image_cinema",
}


def stage(tool, name, args):
    draft = getattr(tool, "request_plan", None)
    from .request_state import project

    state = project(
        tool.conv, tool.session.image_context, tool.cfg.assistant.memory_hours
    )
    if name in MONEY and state["blocked"]:
        return dict(
            ok=False,
            status=state["status"],
            reason="本轮需求字段尚未合并，不能使用旧报价操作订单。",
        )
    if name in REWRITERS:
        if state["expired"] and name == "recover_image_context":
            from .request_recovery import refresh

            return refresh(tool)
        if has_plan(tool.conv, tool.session.image_context) or state["cleared"]:
            return dict(
                ok=False,
                status="REQUEST_FIELDS_INCOMPLETE",
                reason="请先补齐本次明确修改的字段，不从旧图恢复已撤回的信息。",
            )
    if name in EDITORS and state["expired"] and not tool.session.image_context.get("url"):
        return dict(
            ok=False, status="REQUEST_REVALIDATION_REQUIRED",
            reason="先前文字需求已超过核验时效，请发本次影院场次截图，或明确建立新需求；不能继续沿用旧文字字段核价。",
            next_actions=["请买家发送当前场次截图，或明确开始新需求；没有核价或操作订单。"],
        )
    if name not in EDITORS or (
        name == "update_ticket_request" and args.get("intent") == "inquire"
    ):
        return None
    # Independent inquiries have their own non-tradable demand and isolation
    # from old paid orders. Preserve that existing read-only tool contract.
    if (
        name == "supplement_ticket_details"
        and getattr(tool, "semantic_intent", None) == "inquire"
        and getattr(tool.conv, "demand_inquiry", False)
    ):
        return None
    if draft and draft.get("done"):
        return dict(
            ok=False,
            status="STALE_REFERENCE",
            reason="本轮需求已统一核验，不能在同轮继续覆盖。",
        )
    ctx = tool.session.image_context
    previous = ctx.get("agent_request_plan") or {}
    plan = (
        args.get("request_fields")
        or (draft or {}).get("expected")
        or previous.get("expected")
    )
    if (
        not isinstance(plan, list)
        or not plan
        or any(not isinstance(k, str) or k not in FIELDS for k in plan)
        or len(set(plan)) != len(plan)
    ):
        return dict(
            ok=False,
            status="REQUEST_PLAN_REQUIRED",
            reason="请先声明本轮完整变更字段request_fields，包括张数、座位、日期、时间等所有明确字段，再提交本轮原文。",
            next_actions=[
                "重新调用同一需求工具，request_fields包含本轮全部变更字段；没有核价或更新订单。"
            ],
        )
    if draft and set(plan) != set(draft["expected"]):
        return dict(
            ok=False,
            status="REQUEST_FIELDS_INCOMPLETE",
            reason="本轮已声明字段集合不能中途删减；请补齐已声明字段或向买家澄清。",
        )
    if args.get("evidence", tool.text) != tool.text:
        return dict(
            ok=False, status="INVALID_FIELD_EVIDENCE", reason="必须绑定本轮完整原话。"
        )
    if getattr(tool, "semantic_intent", None) not in (None, "revise", "reprice"):
        return dict(
            ok=False,
            status="INTENT_REQUIRED",
            reason="请先确认本轮是在补充或修改同一需求。",
        )
    values = {k: args[k] for k in FIELDS - {"ticket_count", "city"} if args.get(k)}
    refs = {}
    try:
        if name == "update_ticket_request":
            from .semantic_request import number

            n = args.get("quantity")
            fragment = args.get("quantity_text")
            if (
                type(n) is not int
                or not 1 <= n <= 99
                or not isinstance(fragment, str)
                or fragment not in tool.text
                or number(fragment) != n
                or args.get("intent") != "revise"
            ):
                raise ValueError("数量必须有本轮原话佐证。")
            values = {"ticket_count": n}
        elif name == "supplement_city":
            import re

            city = args.get("city")
            if (
                not isinstance(city, str)
                or not re.fullmatch(r"[\u4e00-\u9fff]{2,16}", city)
                or city not in tool.text
            ):
                raise ValueError("城市必须逐字来自本轮原话。")
            values = {"city": city}
        else:
            from .buyer_evidence import resolve

            refs = resolve(tool, values, args.get("source_refs"))
            if not values or any(
                not isinstance(v, str)
                or (v not in tool.text and (refs.get(k) or {}).get("value") != v)
                for k, v in values.items()
            ):
                raise ValueError("字段必须逐字来自本轮原话或已核验引用。")
            if values.get("seats") and values.get("position_preference"):
                raise ValueError("不能同时将区域偏好和具体座号当作已确认位置。")
        if not set(values) <= set(plan):
            raise ValueError("本次字段不在已声明的完整字段集合中。")
    except (ValueError, TypeError) as exc:
        return dict(ok=False, status="INVALID_FIELD_EVIDENCE", reason=str(exc))
    ctx = tool.session.image_context
    scope = {
        k: getattr(tool.conv, k, default)
        for k, default in (("demand_id", ""), ("demand_rev", 0))
    }
    if ctx.get("url"):
        try:
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(
                    ctx.get("request_revalidated_at") or ctx["created_at"]
                )
            ).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = -1
        if (
            any(ctx.get(k) != v for k, v in tool.identity.items())
            or any(
                ctx.get(k, "" if k == "demand_id" else 0) != v for k, v in scope.items()
            )
            or not 0 <= age <= max(1, min(24, tool.cfg.assistant.memory_hours)) * 3600
            or ctx.get("unverified_image")
        ):
            return dict(
                ok=False,
                status="STALE_REFERENCE",
                reason="图片归属、版本或时效尚未核实，不能合并到当前报价。",
            )
    if not ctx.get("url"):
        if any(k in ctx and ctx[k] != v for k, v in {**tool.identity, **scope}.items()):
            return dict(
                ok=False,
                status="STALE_REFERENCE",
                reason="已有文字需求的归属或版本不同，不能合并到当前需求。",
            )
        ctx.update(tool.identity, **scope)
        ctx.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    if draft and (
        ctx.get("url") != draft["url"]
        or scope != draft["scope"]
        or any(ctx.get(k) != v for k, v in tool.identity.items())
    ):
        return dict(
            ok=False,
            status="STALE_REFERENCE",
            reason="图片或需求版本已变化，不能将旧字段提交到新需求。",
        )
    from .buyer_assistant import CLOSED_STATES
    from .purchase_continuation import relevant_orders

    try:
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
    if not draft:
        from .quote_confirmation import current_quote

        q = current_quote(tool)
        compatible = (
            previous.get("pending") is True
            and previous.get("source_image") == ctx.get("url")
            and all(previous.get(k) == v for k, v in {**tool.identity, **scope}.items())
        )
        if compatible:
            plan = sorted(set(plan) | set(previous.get("expected") or []))
        proofs = deepcopy(previous.get("field_evidence") or {}) if compatible else {}
        provided = args.get("field_evidence") or {}
        if not isinstance(provided, dict):
            return dict(ok=False, status="REQUEST_FIELDS_UNSUPPORTED")
        from .request_recovery import validate_proof

        for key, literal in provided.items():
            referenced = (
                key in values
                and literal == values[key]
                and (refs.get(key) or {}).get("value") == literal
            )
            if key not in plan or not (
                validate_proof(key, literal, tool.text) or referenced
            ):
                return dict(ok=False, status="REQUEST_FIELDS_UNSUPPORTED")
            proofs[key] = literal
        # A submitted value is already literal-checked; later tool fields need
        # their original fragments declared now, not fabricated placeholders.
        for key, value in values.items():
            if key not in provided:
                proofs[key] = (
                    args.get("quantity_text") if key == "ticket_count" else value
                )
        unsupported = sorted(
            set(plan) - set(proofs) - set(previous.get("values") or {})
            if compatible
            else set(plan) - set(proofs)
        )
        if unsupported:
            return dict(
                ok=False,
                status="REQUEST_FIELDS_UNSUPPORTED",
                unsupported_fields=unsupported,
                reason="声明字段缺少本轮原文佐证，请纠正字段集合，或提供稍后提交字段的原文片段；尚未修改报价。",
                next_actions=[
                    "更正request_fields和field_evidence后重调同一工具；不要向买家追问其未提出的修改字段。"
                ],
            )
        from .request_recovery import freeze

        canonical = (
            deepcopy(previous.get("canonical_values") or {}) if compatible else {}
        )
        from .buyer_evidence import sources

        source_rows = {r["message_ref"]: r for r in sources(tool)} if refs else {}
        for key in ("date", "time"):
            if key not in proofs:
                continue
            if (
                compatible
                and key not in provided
                and key not in values
                and key in canonical
            ):
                continue
            origin = (previous.get("origins") or {}).get(key) or {}
            if (
                compatible
                and key not in values
                and key not in canonical
                and key == "date"
                and proofs[key] in ("今天", "明天", "后天")
            ):
                return dict(
                    ok=False,
                    status="INVALID_FIELD_EVIDENCE",
                    reason="旧计划的相对日期没有保存当时的日期锚，请确认具体观影日期，不能按今天重新解释。",
                )
            source = source_rows.get((refs.get(key) or {}).get("message_ref")) or {}
            evidence = source.get("text") or (
                tool.text
                if key in provided or key in values
                else origin.get("evidence")
            )
            try:
                known_clock = None
                if key == "time" and proofs[key] in ("上午", "早上", "下午", "晚上"):
                    from .request_failure import pending_request

                    saved = pending_request(tool)
                    if (
                        saved.get("ambiguous_time")
                        and saved.get("pending", True)
                        and saved.get("source_image") in (None, ctx.get("url"))
                    ):
                        known_clock = saved.get("time")
                    elif compatible and previous.get("values", {}).get("time"):
                        known_clock = (previous.get("canonical_values") or {}).get(
                            "time"
                        )
                canonical[key] = freeze(
                    key,
                    proofs[key],
                    evidence or previous.get("evidence") or tool.text,
                    known_clock=known_clock,
                )
            except (TypeError, ValueError):
                return dict(
                    ok=False,
                    status="INVALID_FIELD_EVIDENCE",
                    missing_fields=[key],
                    reason="日期或开场时间尚未识别清楚，请确认原话中的具体内容；没有执行核价。",
                )
        draft = dict(
            expected=list(plan),
            values=deepcopy(previous.get("values") or {}) if compatible else {},
            refs=deepcopy(previous.get("refs") or {}) if compatible else {},
            origins=deepcopy(previous.get("origins") or {}) if compatible else {},
            scope=scope,
            url=ctx.get("url"),
            base=q,
            paused=bool(compatible and previous.get("paused")),
            received=set(),
            done=False,
            field_evidence=proofs,
            canonical_values=canonical,
            declared_now=set(provided) | set(values),
        )
    from .request_recovery import same_value

    if any(
        key in (draft.get("field_evidence") or {})
        and not same_value(key, draft["field_evidence"][key], value, tool.text)
        for key, value in values.items()
    ):
        return dict(
            ok=False,
            status="INVALID_FIELD_EVIDENCE",
            reason="提交值与本轮声明的原文片段不一致，请核对后澄清。",
        )
    for key, value in values.items():
        if key in draft["received"] and draft["values"][key] != value:
            return dict(
                ok=False,
                status="INVALID_FIELD_EVIDENCE",
                reason="本轮同一字段出现不同值，请确认真实需求，不按工具调用顺序选值。",
            )
    replacement = (
        "position_preference"
        if values.get("seats")
        else "seats"
        if values.get("position_preference")
        else None
    )
    if replacement and draft["values"].get(replacement):
        if replacement in draft["received"] or replacement in draft.get(
            "declared_now", set()
        ):
            return dict(
                ok=False,
                status="INVALID_FIELD_EVIDENCE",
                reason="本轮同时提交了区域偏好和具体座号，请先确认采用哪一种位置。",
            )
        for key in ("values", "refs", "origins", "field_evidence", "canonical_values"):
            draft.get(key, {}).pop(replacement, None)
        draft["expected"] = [k for k in draft["expected"] if k != replacement]
        plan = list(draft["expected"])
    tool.request_plan = draft
    draft["values"].update(values)
    draft["refs"].update(refs)
    draft["received"].update(values)
    draft["origins"].update(
        {k: dict(value=v, evidence=tool.text) for k, v in values.items()}
    )
    tool.semantic_intent = getattr(tool, "semantic_intent", None) or "revise"
    effective = dict(draft["values"])
    effective.update(
        {k: v for k, v in draft.get("canonical_values", {}).items() if k in effective}
    )
    if ctx.get("url") and not equivalent(tool, draft["base"], effective):
        from .ticket_details import respond

        if not tool.cfg.recognize_quote.auto_recognize:
            return respond(tool, "自动识别报价已关闭，请店家核对。", False)
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
            return respond(
                tool,
                "当前订单进度需要先核对，暂不替换购票信息。",
                False,
                status="ORDER_STATE_LOCKED",
            )
    # Pause only when a supplied field actually differs. Same-field confirmations
    # keep their quote; future unprovided fields cannot authorize a transaction.
    if not draft["paused"] and not equivalent(tool, draft["base"], effective):
        if tool.service.quote_store:
            tool.service.quote_store.supersede_for_identity(
                tool.identity, demand_id=scope["demand_id"] or None
            )
        tool.conv.quote = None
        tool.conv.quote_id = ""
        tool.conv.quote_kind = ""
        draft["paused"] = True
    ctx["agent_request_plan"] = dict(
        expected=list(plan),
        values=deepcopy(draft["values"]),
        refs=deepcopy(draft["refs"]),
        origins=deepcopy(draft["origins"]),
        field_evidence=deepcopy(draft.get("field_evidence") or {}),
        canonical_values=deepcopy(draft.get("canonical_values") or {}),
        paused=draft["paused"],
        pending=True,
        memory_hours=max(1, min(24, tool.cfg.assistant.memory_hours)),
        evidence=tool.text,
        source_image=draft["url"],
        **scope,
        **tool.identity,
    )
    missing = sorted(set(plan) - set(draft["values"]))
    if missing:
        return dict(
            ok=True,
            status="REQUEST_PLAN_STAGED",
            handled=False,
            missing_fields=missing,
            known_fields=deepcopy(draft["values"]),
            reason="本轮字段已暂存，尚未统一核价；继续提交缺少的字段，不重复询问已有事实。",
            next_actions=[
                "继续用需求工具提交本轮剩余字段；全部到齐后代码统一核验并输出一次结果。"
            ],
        )
    return commit(tool, draft)


def pending(tool):
    draft = getattr(tool, "request_plan", None)
    if (
        not draft
        or draft.get("done")
        or not has_pending(tool.conv, tool.session.image_context)
    ):
        return []
    return sorted(set(draft["expected"]) - set(draft["values"]))


def has_pending(conv, ctx, memory_hours=24):
    from .request_state import project

    return project(conv, ctx, memory_hours)["blocked"]


def has_plan(conv, ctx):
    from .request_state import scoped

    plan = ctx.get("agent_request_plan") or {}
    return bool(
        plan.get("pending") is True
        and plan.get("source_image") == ctx.get("url")
        and scoped(conv, plan)
    )


def equivalent(tool, q, values):
    if q is None or not q.is_usable():
        return False
    from .ticket_details import parse_seats, parse_date, parse_time, CHINA

    try:
        for key, value in values.items():
            if key == "ticket_count" and (
                not q.is_order_ready() or value != q.ticket_count
            ):
                return False
            if key == "seats":

                def pairs(names):
                    return sorted(
                        (s["rowNo"], s["colNo"])
                        for name in names
                        for s in parse_seats(name)
                    )

                if not q.is_order_ready() or pairs([value]) != pairs(q.selected_seats):
                    return False
            if key == "position_preference":
                return False
            if key == "cinema" and value != q.cinema:
                return False
            if key == "movie":
                from .cinema_matching import title_key

                if title_key(value) != title_key(q.movie):
                    return False
            if key == "city" and value != (
                tool.session.image_context.get("recognition_facts") or {}
            ).get("city_name"):
                return False
            if key in ("date", "time"):
                stamp = datetime.fromisoformat(str(q.show).replace("Z", "+00:00"))
                stamp = (
                    stamp.replace(tzinfo=CHINA)
                    if stamp.tzinfo is None
                    else stamp.astimezone(CHINA)
                )
                if key == "date" and parse_date(value) != stamp.date().isoformat():
                    return False
                if key == "time":
                    clock, ambiguous = parse_time(value, evidence=tool.text)
                    if ambiguous or clock != stamp.strftime("%H:%M"):
                        return False
    except (TypeError, ValueError):
        return False
    return True


def commit(tool, draft):
    from .quote_confirmation import acknowledge
    from .ticket_details import supplement

    values = deepcopy(draft["values"])
    normalized = {
        k: v for k, v in (draft.get("canonical_values") or {}).items() if k in values
    }
    values.update(normalized)
    tool.image_snapshot = deepcopy(tool.session.image_context)
    if not draft["paused"] and equivalent(tool, draft["base"], values):
        result = acknowledge(tool, "收到，与刚才核实的购票信息一致，报价已保留。")
        draft["done"] = True
        tool.session.image_context.pop("agent_request_plan", None)
        return result
    city = values.pop("city", None)
    count = values.pop("ticket_count", None)
    if not draft["url"]:
        # No supplier can verify an absent image. Save every declared literal
        # in the pre-image request, including city, for the later image handoff.
        tool.referenced_fields = deepcopy(draft["refs"])
        for k, origin in draft["origins"].items():
            raw = draft["values"].get(k)
            if isinstance(raw, str) and raw in str(origin.get("evidence") or ""):
                tool.referenced_fields[k] = dict(value=raw, source="saved_request_plan")
        for k, value in normalized.items():
            tool.referenced_fields[k] = dict(
                value=value, source="normalized_request_plan"
            )
        try:
            return supplement(
                tool, tool.text, city=city or "", ticket_count=count, **values
            )
        finally:
            tool.referenced_fields = {}
    if city:
        cleared = set(
            (tool.session.image_context.get("ticket_details") or {}).get(
                "cleared_fields"
            )
            or []
        )
        missing = cleared - set(values) - {"city"}
        if count is not None:
            missing.discard("ticket_count")
        if values.get("seats") or values.get("position_preference"):
            missing.difference_update(("seats", "position_preference"))
        if missing:
            return dict(
                ok=False,
                status="REQUEST_FIELDS_INCOMPLETE",
                missing_fields=sorted(missing),
                reason="城市已记下，但还有已撤回的场次或位置需要补齐；未调用旧图核价。",
            )
    if city:
        if values or count is not None:
            # Pass the complete request into recognition *before* any quote.
            # No independent city quote is permitted as an intermediate step.
            from .ticket_details import parse_seats, parse_date, parse_time

            request = {
                **draft["scope"],
                "city": city,
                "pending": True,
                "source_image": draft["url"],
            }
            try:
                request.update(
                    {
                        k: v
                        for k, v in values.items()
                        if k in ("movie", "cinema", "position_preference")
                    }
                )
                if values.get("date"):
                    request["date"] = parse_date(values["date"])
                if values.get("time"):
                    request["time"], request["ambiguous_time"] = parse_time(
                        values["time"], evidence=tool.text
                    )
                if values.get("seats"):
                    request["seats"] = parse_seats(values["seats"])
                if count is not None:
                    request["ticket_count"] = count
                tool.flow_result = tool.service.flow.on_screenshot(
                    tool.conv,
                    draft["url"],
                    city_name=city,
                    channel=tool.conv.channel,
                    ticket_count=count,
                    buyer_request=request,
                )
                from .image_context import preserve_image_result

                preserve_image_result(
                    tool.session, {}, {}, draft["url"], tool.flow_result
                )
                result = dict(ok=bool(tool.flow_result.ok), handled=True)
                if tool.flow_result.ok and tool.conv.quote_id:
                    request.update(
                        pending=False,
                        cleared_fields=[],
                        demand_id=tool.conv.demand_id,
                        demand_rev=tool.conv.demand_rev,
                    )
                    tool.session.image_context["ticket_details"] = request
            except (ValueError, TypeError):
                return dict(
                    ok=False,
                    status="INVALID_FIELD_EVIDENCE",
                    reason="本轮城市与场次字段未核对通过，尚未核价。",
                )
        else:
            result = tool.supplement_city(city)
    elif (
        not values
        and count is not None
        and draft["url"]
        and not tool.session.image_context.get("ticket_details")
    ):
        # No other declared fields: preserve provider-specific quantity handling.
        result = tool._resume_quote(ticket_count=count)
    else:
        tool.referenced_fields = deepcopy(draft["refs"])
        # Inherited fields were literally validated when the same scoped draft
        # was staged. They are local saved facts, never new model citations.
        for k, origin in draft["origins"].items():
            if (
                k in values
                and values[k] not in tool.text
                and origin.get("value") == values[k]
            ):
                if isinstance(values[k], str) and values[k] in str(
                    origin.get("evidence") or ""
                ):
                    tool.referenced_fields[k] = dict(
                        value=values[k], source="saved_request_plan"
                    )
        for k, value in normalized.items():
            tool.referenced_fields[k] = dict(
                value=value, source="normalized_request_plan"
            )
        try:
            result = supplement(tool, tool.text, ticket_count=count, **values)
        finally:
            tool.referenced_fields = {}
    success = bool(
        result.get("ok")
        and tool.flow_result is not None
        and tool.flow_result.ok
        and (tool.conv.quote_id or not draft["url"])
    )
    if success:
        draft["done"] = True
        tool.session.image_context.pop("agent_request_plan", None)
    return result
