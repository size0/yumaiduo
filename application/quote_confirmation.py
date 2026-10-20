"""Idempotent supplements: compare explicit fields with a scoped current quote."""
from datetime import datetime, timezone

from domain.conversation import ConvState


def current_quote(tool):
    ctx = tool.session.image_context or {}
    saved = ctx.get('ticket_details') or {}
    if (ctx != getattr(tool,'image_snapshot',None) or not ctx.get('verified') or ctx.get('unverified_image')
            or ctx.get('awaiting_image_request') or ctx.get('cinema_candidate') or ctx.get('quote_failure')
            or tool.conv.state != ConvState.QUOTED or getattr(tool.conv,'demand_inquiry',False)
            or getattr(tool,'seller_context',{}).get('active')
            or any(ctx.get(k) != v for k,v in tool.identity.items())
            or any(ctx.get(k) != getattr(tool.conv,k,None) for k in ('demand_id','demand_rev'))):
        return None
    if saved and (saved.get('pending') is not False or saved.get('source_image') != ctx.get('url')
            or any(saved.get(k) != getattr(tool.conv,k,None) for k in ('demand_id','demand_rev'))):
        return None
    facts = ctx.get('recognition_facts') or {}
    if facts.get('match_level') != 'EXACT':
        return None
    try:
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(ctx.get('request_revalidated_at') or ctx['created_at'])).total_seconds()
        if not 0 <= age <= max(1,min(24,tool.cfg.assistant.memory_hours))*3600:
            return None
        store = getattr(tool.service,'quote_store',None)
        q = store.get(tool.conv.quote_id) if store and tool.conv.quote_id else None
        if (not q or not q.is_usable()
                or any(getattr(q,k,None) != v for k,v in tool.identity.items())
                or any(getattr(q,k,None) != getattr(tool.conv,k,None) for k in ('demand_id','demand_rev'))):
            return None
        from .buyer_assistant import CLOSED_STATES
        from .purchase_continuation import relevant_orders
        if any(o.state not in CLOSED_STATES and (o.payment_state != 'UNPAID'
                or (o.payload or {}).get('quote_revision_pending') or o.state == 'REPRICE_PENDING')
                for o in relevant_orders(tool)):
            return None
    except (KeyError,TypeError,ValueError,RuntimeError):
        return None
    return q


def acknowledge(tool, message):
    from .ticket_flow import FlowResult
    tool.city_attempted = True
    tool.flow_result = FlowResult(ok=True,state=tool.conv.state,channel=tool.conv.channel,
        messages=[message],template_keys=['QUOTE_DETAILS_CONFIRMED'])
    return {'ok':True,'status':'UNCHANGED','handled':True,'quote_id':tool.conv.quote_id,
            'reason':'补充字段与当前已核实报价一致；未重新识别、核价或修改订单。'}


def unchanged_details(tool, *, movie='', date='', time='', seats='', cinema='',
                      ticket_count=None, position_preference='', observe_uncovered=False):
    # A preference is not an exact seat fact. It must pass the normal selection
    # path even when count matches; do not infer "middle" from numeric seats.
    if position_preference:
        return None
    # Do not finish a multi-field message after only one matching tool field.
    # Uncovered words may be another change; let the normal agent/tool flow
    # process them rather than acknowledging the entire request prematurely.
    rest = tool.text
    for literal in (movie,date,time,seats,cinema):
        if literal:
            if literal not in rest:
                return None
            rest = rest.replace(literal,'',1)
    rest = rest.strip(' \t\r\n。！!，,；;、')
    covered = not rest
    if rest:
        from .buyer_assistant import explicit_ticket_count
        from .semantic_request import number
        literal_count = explicit_ticket_count(rest) or number(rest)
        if ticket_count is None and any((movie,date,time,seats,cinema)) and literal_count is not None:
            ticket_count = literal_count
        covered = ticket_count is not None and (explicit_ticket_count(rest) == ticket_count or number(rest) == ticket_count)
        if not covered and not observe_uncovered:
            return None
    q = current_quote(tool)
    if q is None:
        return None
    from .ticket_details import parse_date, parse_time, parse_seats, CHINA
    try:
        if ticket_count is not None:
            if type(ticket_count) is not int or not q.is_order_ready() or q.ticket_count != ticket_count:
                return None
        if seats:
            if not q.is_order_ready() or not q.selected_seats:
                return None
            def keys(rows):
                parsed = [s for row in rows for s in parse_seats(row)]
                return sorted((s['rowNo'],s['colNo']) for s in parsed)
            if keys([seats]) != keys(q.selected_seats):
                return None
        if movie:
            from .cinema_matching import title_key
            if title_key(movie) != title_key(q.movie):
                return None
        if cinema and cinema != q.cinema:
            return None
        if date or time:
            stamp = datetime.fromisoformat(str(q.show).replace('Z','+00:00'))
            stamp = stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp.astimezone(CHINA)
            if date and parse_date(date) != stamp.date().isoformat():
                return None
            if time:
                clock, ambiguous = parse_time(time,evidence=tool.text)
                if ambiguous or clock != stamp.strftime('%H:%M'):
                    return None
    except (TypeError,ValueError):
        return None
    if not any((movie,date,time,seats,cinema,ticket_count is not None)):
        return None
    if not covered:
        # The submitted fields match, but the message may contain other changes.
        # Return an observation, never a terminal reply or an OCR/requote.
        return {'ok':True,'status':'UNCHANGED_FIELDS','handled':False,'quote_id':q.quote_id,
                'known_fields':{'ticket_count':q.ticket_count,'seats':q.selected_seats,
                                'cinema':q.cinema,'movie':q.movie,'show':q.show},
                'reason':'本次提交字段与现有报价一致；未重新识别或核价，也尚未确认整句所有需求。',
                'next_actions':['继续保存本轮其余座位、场次等变更；若仅确认相同张数，简短确认并保留报价，不要重新核价。']}
    message = (f'收到，仍是这{q.ticket_count}张、原来的座位，刚才的报价已保留。' if seats else
               f'收到，共{q.ticket_count}张，刚才的报价已保留。' if ticket_count is not None else
               '收到，与刚才核实的购票信息一致，报价已保留。')
    return acknowledge(tool,message)
