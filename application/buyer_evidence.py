"""Bounded references to buyer messages; never a payment or order authority."""
import hashlib
import json
from datetime import datetime

FIELDS = ('cinema', 'movie', 'date', 'time', 'seats', 'position_preference')


def sources(tool):
    from .ticket_details import now, CHINA
    current = now()
    ctx = tool.session.image_context
    anchors = []
    for value in (ctx.get('created_at'), ctx.get('inquiry_started_at')):
        if value:
            try:
                stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                anchors.append(stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp)
            except ValueError:
                return []
    rows = []
    history = getattr(tool, 'official_buyer_history', tool.session.history)
    for index, row in enumerate(history[-12:]):
        if row.get('role') != 'user':
            continue
        text = str(row.get('content') or '')
        at = row.get('created_at')
        eligible = False
        try:
            stamp = datetime.fromisoformat(str(at).replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp
            eligible = (0 <= (current-stamp).total_seconds() <= 1800
                        and stamp.astimezone(CHINA).date() == current.date()
                        and all(stamp >= anchor for anchor in anchors))
        except (ValueError, TypeError):
            pass
        ref = hashlib.sha256(json.dumps([tool.identity, index, text, at,
                    getattr(tool.conv, 'demand_id', ''), getattr(tool.conv, 'demand_rev', 0)], sort_keys=True).encode()).hexdigest()[:20]
        rows.append({'message_ref': ref, 'text': text, 'at': at, 'can_reference': eligible})
    return rows


def resolve(tool, fields, refs):
    """Only literal fields from recent same-demand buyer messages may be reused."""
    if not refs:
        return {}
    if (not isinstance(refs, dict) or set(refs)-set(FIELDS)
            or getattr(tool, 'semantic_intent', None) not in ('revise', 'inquire')):
        raise ValueError('历史引用仅用于当前购票资料补充，不能授权购买或改价。')
    ctx = tool.session.image_context
    if ctx and any(ctx.get(k) != v for k, v in tool.identity.items()):
        raise ValueError('历史信息与当前会话身份尚未核对。')
    available = {r['message_ref']: r for r in sources(tool) if r['can_reference']}
    verified = {}
    for field, ref in refs.items():
        value = fields.get(field, '')
        source = available.get(ref)
        if not isinstance(value, str) or not value.strip() or not source or value.strip() not in source['text']:
            raise ValueError(f'{field}的消息引用或逐字佐证无效；请在同一次调用填写{field}的历史原文和source_refs.{field}的有效message_ref，不能只填其中一项。可用read_buyer_context核对来源。')
        verified[field] = {'value': value.strip(), 'message_ref': ref, 'at': source['at']}
    return verified
