"""Project saved request facts and missing fields; never compose a buyer reply."""
from copy import deepcopy

LABELS={'cinema':'影院','city':'城市','position_preference':'位置偏好','movie':'影片','date':'观影日期','time':'开场时间',
        'ticket_count':'张数','seats':'具体座号','image':'场次截图',
        'time_period':'上午或晚上','cinema_confirmation':'影院确认','cinema_identity':'城市与完整影院名称',
        'show_verification':'场次核验','image_confirmation':'本次图片确认'}


def project(tool,ok,status=None,reason='',observation=None):
    from .request_failure import pending_request
    ctx=tool.session.image_context
    saved=deepcopy(pending_request(tool))
    known={k:deepcopy(v) for k,v in saved.items() if k in ('cinema','city','movie','date','time','ticket_count','seats','position_preference') and v}
    facts=ctx.get('recognition_facts') or {}
    if (not ctx.get('unverified_image') and not getattr(tool.conv,'demand_inquiry',False)
            and all(ctx.get(k)==v for k,v in tool.identity.items())
            and type(ctx.get('demand_rev')) is int
            and all(k in ctx and ctx[k]==getattr(tool.conv,k,d)
                    for k,d in (('demand_id',''),('demand_rev',0)))
            and facts.get('match_level')=='EXACT' and facts.get('cinema_id')
            and facts.get('cinema_name') and not facts.get('cinema_truncated')):
        known.setdefault('cinema',facts['cinema_name'])
    missing=[k for k in ('cinema','movie','date','time') if not known.get(k)]
    if not known.get('ticket_count') and not known.get('seats'):missing.append('ticket_count')
    if saved.get('ambiguous_time'):missing.append('time_period')
    missing.extend(saved.get('cleared_fields') or [])
    if saved.get('position_preference') and not saved.get('seats'):missing.append('seats')
    if not ctx.get('url'):missing.append('image')
    elif ctx.get('unverified_image'):missing.append('image_confirmation')
    elif saved.get('awaiting') in ('cinema_confirmation','cinema_identity','show_verification'):
        missing.append(saved['awaiting'])
    return dict(ok=ok,status=status or ('NEEDS_INFORMATION' if ok else 'BLOCKED'),
        source='local_flow',public_reason=reason if not ok else '',
        reason=reason if not ok else '本轮文字字段已保存；场次、余座或价格尚未因此确认成功。',
        business_observation=deepcopy(observation or {}),
        known_fields=known,missing_fields=list(dict.fromkeys(missing)),field_labels=LABELS,
        demand=dict(demand_id=getattr(tool.conv,'demand_id',''),demand_rev=getattr(tool.conv,'demand_rev',0)),
        next_actions=['ANSWER_FROM_OBSERVATIONS' if not ok else 'ASK_ONLY_MISSING_FIELDS'])
