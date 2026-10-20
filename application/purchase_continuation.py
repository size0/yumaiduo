"""Separate a new purchase from historical money and expose factual progress."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4


def separate(tools):
    ctx = tools.session.image_context
    return bool(ctx.get('purchase_started_at') and ctx.get('purchase_demand_id') == getattr(tools.conv, 'demand_id', '')
                and all(ctx.get(k) == v for k, v in tools.identity.items()))


def relevant_orders(tools):
    """Old purchases do not lock a newly declared purchase; its own orders do."""
    rows=tools._orders()
    if separate(tools):
        rows=[o for o in rows if o.demand_id==getattr(tools.conv,'demand_id','')]
    return rows


def begin(tools, evidence, quantity, quantity_text):
    from .semantic_request import number
    from .ticket_flow import FlowResult
    from domain.conversation import ConvState
    if (evidence != tools.text or getattr(tools, 'semantic_intent', None) != 'revise'
            or type(quantity) is not int or not 1 <= quantity <= 99
            or not quantity_text or quantity_text not in evidence or number(quantity_text) != quantity
            or not all(tools.identity.values())):
        return {'ok': False, 'status': 'INVALID_EVIDENCE', 'reason': '新增购买必须有本轮明确意图及原话张数，不能把询价或修改原单当成新购买。'}
    if getattr(tools, 'new_purchase_started', False):
        return {'ok': True, 'status': 'NEW_PURCHASE', 'demand_id': tools.conv.demand_id}
    previous = tools.session.image_context
    platforms = getattr(tools.service.order_store, 'platforms', None)
    prior_orders = [r['platform_order_id'] for r in platforms.by_identity(tools.identity)] if platforms else []
    archive = list(previous.get('demand_archive') or [])
    archive.append(dict(demand_id=getattr(tools.conv, 'demand_id', ''),
                        demand_rev=getattr(tools.conv, 'demand_rev', 0), quote_id=tools.conv.quote_id,
                        ticket_details=deepcopy(previous.get('ticket_details') or {})))
    conv = tools.conv
    conv.demand_id, conv.demand_rev, conv.demand_inquiry = str(uuid4()), 0, False
    conv.quote = None; conv.quote_id = ''; conv.quote_kind = ''
    conv.recognition = None; conv.supplement_recognition = {}; conv.seats = []
    conv.requested_order_no = ''; conv.state = ConvState.IDLE
    stamp = datetime.now(timezone.utc).isoformat()
    request = dict(ticket_count=quantity, demand_id=conv.demand_id, demand_rev=0)
    tools.session.image_context = dict(tools.identity, url='', created_at=stamp,
        purchase_started_at=stamp, purchase_demand_id=conv.demand_id, demand_id=conv.demand_id, demand_rev=0,
        prior_platform_order_ids=prior_orders,
        ticket_count=quantity, awaiting_image_request=request,
        demand_archive=archive[-20:], quantity_evidence=evidence)
    tools.session.pending_order_card = {}
    tools.platform_facts = {}
    tools.image_snapshot = dict(tools.session.image_context)
    tools.new_purchase_started = True
    tools.flow_result = FlowResult(ok=True, state=conv.state, channel=conv.channel,
        messages=[f'收到，这次另外买{quantity}张，原订单保持不变。请发本次场次截图，我按这次的张数核价。'],
        template_keys=['NEW_PURCHASE'])
    return {'ok': True, 'status': 'NEW_PURCHASE', 'demand_id': conv.demand_id,
            'quantity': quantity, 'reason': '只建立新需求，未修改旧订单、改价、付款或下单。'}


def card_number(tools):
    card = getattr(tools.session, 'pending_order_card', None) or {}
    from .plugin_runtime import _identity
    if not card or _identity(card) != tools.identity:
        return ''
    number = str((card.get('payload') or {}).get('orderId') or '')
    if separate(tools):
        if (card.get('_demand_id') != tools.conv.demand_id
                or number in (tools.session.image_context.get('prior_platform_order_ids') or [])):
            return ''
        platforms = getattr(tools.service.order_store, 'platforms', None)
        row = platforms.get(tools.identity['tenant_id'], tools.identity['shop_id'], number) if platforms else None
        if not created_after(row or {}, tools.session.image_context['purchase_started_at']):
            return ''
    return number


def created_after(row, start):
    try:
        created = datetime.fromisoformat(str(row['platform_created_at']).replace('Z', '+00:00'))
        return created >= datetime.fromisoformat(start)
    except (KeyError, TypeError, ValueError):
        return False


def remember_card(session, event):
    """Preserve first attribution; arrival time never proves a new purchase."""
    card = dict(event)
    if '_demand_id' in card:
        return card
    demand_id = getattr(session.conversation, 'demand_id', '')
    ctx = session.image_context
    if ctx.get('purchase_started_at'):
        detail = event.get('_order_detail') or {}
        if not created_after({'platform_created_at': detail.get('createTime')}, ctx['purchase_started_at']):
            return card
    if demand_id:
        card['_demand_id'] = demand_id
    return card


def candidates(tools, rows):
    """Choose explicit/latest card; separate purchase excludes historical orders."""
    number = getattr(tools.conv, 'requested_order_no', '')
    if number:
        return [r for r in rows if r['platform_order_id'] == number]
    card_no = card_number(tools)
    if card_no:
        chosen = [r for r in rows if r['platform_order_id'] == card_no]
        if chosen:
            return chosen
    if separate(tools):
        prior = set(tools.session.image_context.get('prior_platform_order_ids') or [])
        rows = [r for r in rows if r['platform_order_id'] not in prior
                and created_after(r, tools.session.image_context['purchase_started_at'])]
    active = [r for r in rows if r['platform_status'] in (1, 2, 3)]
    return active or rows


def progress(tools):
    facts = tools.read_platform_order()
    result = dict(facts, ticket_delivery_verified=False)
    if not facts.get('ok'):
        result['guidance'] = '本次订单进度查询暂未取得唯一结果，请提供要核对的订单号，我再核对付款、出票和取票信息；不会让您盲目重复付款。'
        return result
    status = facts.get('platform_status')
    bound = [o for o in tools._orders() if (o.platform_order_id or o.provider_order_id) == facts.get('order_no')]
    if len(bound) == 1 and bound[0].ticket_codes:
        result.update(ticket_information_available=True)
        result['guidance'] = '这笔订单已有取票信息记录，发送回执仍需核对。请查看聊天中的取票二维码；未收到时请店家核对，暂不重复发送。'
    elif status in (3, 4):
        def after_payment(row):
            try:
                return datetime.fromisoformat(row['at']) >= datetime.fromisoformat(str(facts['platform_paid_at']).replace('Z', '+00:00'))
            except (KeyError, TypeError, ValueError):
                return False
        images = [r for r in getattr(tools, 'official_dialogue', {}).get('images', [])
                  if r.get('speaker') == 'seller' and all(r.get(k) == v for k, v in tools.identity.items())
                  and after_payment(r)]
        result['seller_image_observed'] = bool(images)
        result['guidance'] = ('平台显示这笔订单已发货，聊天中有店铺发送的图片，请先查看该图片；'
                              if images else '平台显示这笔订单已发货；') + '本系统尚未核实该单取票码或二维码，需店家核对具体交付信息，不需要重复付款。'
    elif status in (5, 6):
        result['guidance'] = ('这笔订单已关闭。' if status == 6 else '这笔订单当前处于平台状态5，具体含义仍需店家核对。') + '不能仅凭原付款记录安排出票；退款或关闭结果、是否需重拍应先核对平台明细。'
    elif status == 2:
        result['guidance'] = '平台已查到这笔订单的付款记录，取票信息尚未核实。需要店家核对出票进度；目前不能声称已经出票或重复收款。'
    else:
        result['guidance'] = '这笔订单目前尚未核实付款和改价结果，暂不能安排出票；请先核对订单金额和场次。'
    return result
