"""Recover a lost cinema only from scoped, anchored historical image evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from .image_sequence import read_image, _remember, normalized


def restore(tools):
    ctx = tools.session.image_context
    if (ctx.get('verified') or ctx.get('cinema_evidence') or not ctx.get('url')
            or any(ctx.get(k) != v for k, v in tools.identity.items())):
        return False
    facts = ctx.get('recognition_facts') or {}
    if (facts.get('cinema_id') or facts.get('price_mismatch') or facts.get('seat_confirm_required')
            or facts.get('seats') or facts.get('match_level') == 'EXACT'):
        return False
    dialogue = getattr(tools, 'official_dialogue', {})
    if dialogue.get('status') != 'AVAILABLE':
        return False
    try:
        target = datetime.fromisoformat(ctx['created_at'])
        started = datetime.fromisoformat(ctx.get('purchase_started_at') or ctx.get('inquiry_started_at') or ctx['created_at'])
        cutoff = datetime.now(timezone.utc).timestamp() - max(1, min(24, tools.cfg.assistant.memory_hours)) * 3600
        rows = [r for r in dialogue.get('images', []) if r.get('speaker') == 'buyer'
                and all(r.get(k) == v for k, v in tools.identity.items()) and r.get('url') != ctx['url']
                and cutoff <= datetime.fromisoformat(r['at']).timestamp() <= target.timestamp()
                and (not (ctx.get('purchase_started_at') or ctx.get('inquiry_started_at'))
                     or datetime.fromisoformat(r['at']) >= started)]
        if (ctx.get('demand_id', '') != getattr(tools.conv, 'demand_id', '')
                or ctx.get('demand_rev', 0) != getattr(tools.conv, 'demand_rev', 0)):
            return False
        for row in sorted(rows, key=lambda r: r['at'], reverse=True)[:2]:
            data = read_image(tools.service, tools.cfg, row['url'], row['at'])
            if (not isinstance(data, dict) or data.get('kind') not in ('cinema', 'schedule')
                    or data.get('cinema_complete') is not True or not data.get('cinema') or not data.get('city')
                    or normalized(data['cinema']) == normalized(data.get('film'))):
                continue
            # Legacy images have no durable demand assignment. Propose their
            # visible cinema; a buyer must confirm before it may enter pricing.
            if not (ctx.get('purchase_started_at') or ctx.get('inquiry_started_at')):
                ctx['cinema_candidate'] = dict(tools.identity, cinema=data['cinema'], city=data['city'],
                    source_url=row['url'], source_message_id=row['message_id'], created_at=row['at'],
                    demand_id=getattr(tools.conv, 'demand_id', ''), demand_rev=getattr(tools.conv, 'demand_rev', 0))
                return False
            saved = _remember(tools.session, tools.identity, data['cinema'], data['city'], row['url'])
            saved.update(created_at=row['at'], source_message_id=row['message_id'], source='official_image_history')
            return True
    except Exception:
        return False
    return False


def confirm(tools, evidence, source_message_id):
    candidate = tools.session.image_context.get('cinema_candidate') or {}
    if getattr(tools, 'semantic_intent', None) != 'revise':
        return {'ok': False, 'status': 'INTENT_REQUIRED', 'reason': '请先调用record_buyer_intent(intent="revise")登记本轮影院确认，再调用confirm_image_cinema；当前尚未确认或报价。'}
    if (evidence != tools.text
            or not source_message_id or candidate.get('source_message_id') != source_message_id
            or any(candidate.get(k) != v for k, v in tools.identity.items())
            or any(candidate.get(k) != getattr(tools.conv, k, default)
                   for k, default in (('demand_id', ''), ('demand_rev', 0)))):
        return {'ok': False, 'status': 'INVALID_EVIDENCE', 'reason': '只能确认本会话当前需求已展示的影院候选。'}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(candidate['created_at'])).total_seconds()
        if not 0 <= age <= min(24, max(1, tools.cfg.assistant.memory_hours)) * 3600:
            return {'ok': False, 'status': 'STALE_REFERENCE', 'reason': '影院候选已过期，请重新核对完整影院。'}
    except (KeyError, TypeError, ValueError):
        return {'ok': False, 'status': 'STALE_REFERENCE', 'reason': '影院候选时间未核实。'}
    _remember(tools.session, tools.identity, candidate['cinema'], candidate['city'], candidate['source_url'])
    tools.session.image_context.pop('cinema_candidate', None)
    tools.image_snapshot = dict(tools.session.image_context)
    return tools._resume_quote(ticket_count=tools.session.image_context.get('ticket_count'))
