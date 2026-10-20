"""Save text seats against the current image, then reverify through Wanda."""
from copy import deepcopy
from dataclasses import fields
from datetime import datetime,timezone


def available(tool, facts):
    bridge=getattr(tool.service.flow,'wanda_bridge',None)
    raw=facts.get('raw_results') or {}
    return bool(bridge is not None and (confirmed_image(tool, facts) or (
                callable(getattr(bridge,'match_facts',None))
                and all(raw.get(k) for k in ('city','cinema','film','hall','showtime')))))


def confirmed_image(tool, facts):
    """A failed price lookup does not erase an exact cinema/show observation."""
    return (tool.session.image_context.get('quote_failure') == 'WANDA_QUOTE_FAILED'
            and facts.get('match_level') == 'EXACT'
            and all(facts.get(k) for k in ('city_name', 'cinema_id', 'cinema_name',
                                           'movie_name', 'hall_name', 'start_time')))


def supplement(tool, literal_seats, count=None):
    from .ticket_details import parse_seats,respond
    from .ticket_flow import FlowResult
    from .wanda_recovery import recover
    from domain.conversation import Event
    from integrations.liangpiao.api import RecognitionResult
    ctx=tool.session.image_context;facts=ctx.get('recognition_facts') or {}
    if (ctx.get('unverified_image') or getattr(tool.conv,'demand_inquiry',False)
            or any(ctx.get(k)!=v for k,v in tool.identity.items())
            or any(ctx.get(k)!=getattr(tool.conv,k,default) for k,default in (('demand_id',''),('demand_rev',0)))
            or facts.get('price_mismatch') or facts.get('seat_confirm_required')):
        return {'ok':False,'status':'STALE_REFERENCE','reason':'当前图片的需求归属或座位信息还需核对，请先确认本次截图；未更新订单。'}
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(ctx.get('request_revalidated_at') or ctx['created_at'])).total_seconds()
        if not 0<=age<=min(24,max(1,tool.cfg.assistant.memory_hours))*3600:
            raise ValueError('stale image')
        seats=parse_seats(literal_seats)
        if count is not None and (type(count) is not int or count!=len(seats)):
            raise ValueError('quantity conflict')
    except (KeyError,TypeError,ValueError):
        return {'ok':False,'status':'INVALID_EVIDENCE','reason':'请核对本次截图及明确的几排几座、张数；不猜测数字含义。'}
    prior=ctx.get('ticket_details') or {}
    if prior and prior.get('source_image')!=ctx['url']:
        return {'ok':False,'status':'STALE_REFERENCE','reason':'已有其他待核对的场次补充，请先确认本次影片日期时间，再保存座位。'}
    pending=ctx.get('awaiting_image_request') or {}
    if (pending or any(prior.get(k) for k in ('movie','date','time','cinema','ambiguous_time','position_preference'))
            or (prior.get('ticket_count') and prior['ticket_count']!=len(seats))):
        return {'ok':False,'status':'PENDING_DETAILS_CONFLICT',
                'reason':'已保留之前补充的场次、张数或位置需求，请先确认这些信息与本次具体座位一致；不会按旧截图覆盖补充。'}
    demand=dict(demand_id=getattr(tool.conv,'demand_id',''),demand_rev=getattr(tool.conv,'demand_rev',0),
        source_image=ctx['url'],seats=seats,ticket_count=len(seats),evidence=tool.text,pending=True,awaiting='show_verification')
    ctx['ticket_details']=demand
    tool.conv.quote=None;tool.conv.quote_id='';tool.conv.quote_kind=''
    if tool.service.quote_store:
        tool.service.quote_store.supersede_for_identity(tool.identity,demand_id=getattr(tool.conv,'demand_id','') or None)
    try:
        names={f.name for f in fields(RecognitionResult)}
        recognized=RecognitionResult(**{k:v for k,v in facts.items() if k in names})
        # Exact supplier recognition already has the mapped cinema identity.
        # Re-enter the normal flow, which rechecks Wanda schedule/seats/prices;
        # do not force truncated OCR names through a second name-only matcher.
        verified=deepcopy(recognized) if confirmed_image(tool, facts) else recover(tool.service.flow.wanda_bridge,recognized)
        verified.seats=deepcopy(seats);verified.expected_seat_count=len(seats);verified.seat_matched=True
        verified.price_all_fen=0
        verified.raw_results={**(verified.raw_results or {}),'buyer_text_details':True,
                              'seat':[{'seatName':s['seatNo']} for s in seats]}
        verified.raw_results.pop('priceAll',None)
        tool.city_attempted=True
        decision=tool.conv.apply(Event.BUYER_IMAGE)
        if decision.needs_manual:
            return respond(tool,'当前订单进度需要店家核对，座位需求已保留。',False)
        tool.conv.buyer_ticket_count=len(seats)
        tool.flow_result=tool.service.flow._after_recognition(tool.conv,verified,FlowResult(channel='wanda'))
        if tool.flow_result.ok and tool.conv.quote_id:
            demand.update(demand_id=getattr(tool.conv,'demand_id',''),demand_rev=getattr(tool.conv,'demand_rev',0),pending=False)
            ctx['ticket_count']=len(seats)
            ctx.update(demand_id=getattr(tool.conv,'demand_id',''),demand_rev=getattr(tool.conv,'demand_rev',0),verified=True)
            ctx['recognition_facts']={f.name:deepcopy(getattr(verified,f.name)) for f in fields(RecognitionResult)}
            ctx.pop('quote_failure', None)
        return {'ok':bool(tool.flow_result.ok),'handled':True,
                'reason':'座位原文已保存并交万达核验本次场次、余座与价格；未改价、下单或付款。'}
    except Exception:
        return respond(tool,'已记下本次具体座位，但万达场次或余座价格暂未核实，请店家核对；不会要求您重选旧候选或沿用旧价格。',False)
