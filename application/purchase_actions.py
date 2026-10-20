"""Buyer-scoped platform checks and requests through the existing execution queue."""
import json
from types import SimpleNamespace

from integrations.yumaiduo.shop_client import PluginShopsClient, resolve_plugin_secret
from application.quote_reprice import build_quote_reprice, _replaceable


def purchase_state_blocked(conv):
    """A missing local order cannot reset an in-progress purchase."""
    from domain.conversation import ConvState
    return conv.state in {ConvState.AWAIT_PAY, ConvState.AWAIT_REPRICE,
                          ConvState.SELF_BUY, ConvState.AWAIT_TICKET}


def platform_order(order, identity):
    if not all(identity.values()) or any(str(getattr(order, k)) != v for k, v in identity.items()):
        raise ValueError('ORDER_IDENTITY_MISMATCH')
    client = PluginShopsClient(secret=resolve_plugin_secret())
    status, body = client._post('/internal/orders/read', {
        'tenant_id': identity['tenant_id'], 'order_id': order.provider_order_id})
    if status != 200:
        raise ValueError('PLATFORM_QUERY_UNAVAILABLE')
    detail = json.loads(body)
    if (str(detail.get('orderId')) != order.provider_order_id
            or str(detail.get('accountUnb')) != identity['shop_id']):
        raise ValueError('PLATFORM_ORDER_IDENTITY_MISMATCH')
    return detail


def inspect(tools):
    # Demand state constrains transaction permission, never hides read-only
    # platform facts needed to explain payment or delivery progress.
    result=_inspect_facts(tools)
    from .request_state import project
    state=project(tools.conv,tools.session.image_context,tools.cfg.assistant.memory_hours)
    if state['blocked']:
        result=dict(result,payment_blocked=True,purchase_ready=False,quote_valid=False,matches_latest_quote=False,
            demand_status=state['status'])
        if not result.get('ok'):
            result['status']=state['status']
        elif result.get('status')=='MATCHED':
            result['status']='NEEDS_REVIEW'
        result['guidance']=result.get('guidance','')+' 本次需求尚未核实，不能据旧报价付款或改价。'
    return result


def _inspect_facts(tools):
    from domain.order.models import TERMINAL_STATES
    orders = [o for o in tools._orders() if o.state not in TERMINAL_STATES and o.state != 'CLOSED']
    from .purchase_continuation import separate
    if separate(tools):
        orders = [o for o in orders if o.demand_id == getattr(tools.conv, 'demand_id', '')]
    number = getattr(tools.conv, 'requested_order_no', '')
    from .purchase_continuation import card_number
    number = number or card_number(tools)
    if number:
        orders = [o for o in orders if (o.platform_order_id or o.provider_order_id) == number]
    if not orders:
        platforms = getattr(tools.service.order_store, 'platforms', None)
        candidates = platforms.by_identity(tools.identity) if platforms else []
        from .purchase_continuation import candidates as select_candidates, separate
        candidates = select_candidates(tools, candidates)
        if len(candidates) > 1:
            return {'ok': False, 'status': 'ORDER_AMBIGUOUS', 'payment_blocked': True,
                    'guidance': '已记录多笔平台订单，需要先确认对应订单；请勿重复下单或付款。'}
        if len(candidates) == 1:
            from .platform_orders import refresh_record
            checked = refresh_record(tools.service.order_store, candidates[0])
            row = platforms.get(candidates[0]['tenant_id'], candidates[0]['shop_id'], candidates[0]['platform_order_id'])
            if not checked['ok']:
                return {'ok': False, 'status': 'QUERY_UNKNOWN', 'guidance': '已记录平台订单，本次实时查询暂未取得结果；请勿重复下单或付款。'}
            status = row['platform_status']
            return {'ok': True, 'status': 'PLATFORM_ORDER_UNBOUND', 'order_no': row['platform_order_id'],
                'platform_status': status, 'platform_amount_yuan': f"{(row.get('amount_fen') or 0)/100:.2f}",
                'platform_paid_at': row.get('paid_at'), 'platform_created_at': row.get('platform_created_at'),
                'matches_latest_quote': False, 'quote_valid': False, 'payment_blocked': True,
                'guidance': ('这笔订单已关闭，原付款记录不代表仍待出票；请核对平台退款明细及是否另行购买。' if status == 6 else
                    '平台当前状态5的具体含义尚待核实，请店家核对退款或关闭明细，不能仅凭原付款记录安排出票。' if status == 5 else
                    '平台订单已发货，请先核对原订单交付，不要重复下单或付款。' if status in (3, 4) else
                    '已查到平台付款记录，购票信息尚未完成关联，需要店家核对；无需再次下单或付款。' if row.get('paid_at') else
                    '已查到平台订单，购票信息与成交金额尚未核对，请先不要付款或重复下单。')}
        if separate(tools):
            return {'ok': True, 'status': 'NEW_PURCHASE_PENDING', 'payment_blocked': True,
                    'guidance': '本次是另外购买，已保存新需求，但本次场次、金额或新订单还未核实。可以继续核价；之前订单的状态不影响这次询价。'}
        quotes = tools.service.quote_store.by_identity(tools.identity) if tools.service.quote_store else []
        latest = quotes[0] if quotes else None
        from .assistant_reply import _is_current_order
        ctx = tools.session.image_context or {}
        ready = bool(latest and latest.is_order_ready() and _is_current_order(tools, {
            'quote_id': latest.quote_id, 'demand_id': latest.demand_id, 'demand_rev': latest.demand_rev})
            and not ctx.get('unverified_image') and not (ctx.get('ticket_details') or {}).get('pending')
            and not purchase_state_blocked(tools.conv)
            and not getattr(tools, 'seller_context', {}).get('active'))
        return {'ok': False, 'status': 'NO_BOUND_ORDER', 'purchase_ready': ready,
                'next_tool': 'get_purchase_guidance' if ready else None,
                'guidance': ('本次整单报价有效，但尚未取得对应平台订单。请结合前文区分确认购买和查询订单，使用get_purchase_guidance确认下单步骤；不能认定买家没有拍下。'
                             if ready else '当前会话尚未关联到平台订单。若已经拍下，请提供订单卡片供核对；不能据此认定没有拍下。')}
    if len(orders) != 1 or not orders[0].provider_order_id:
        return {'ok': False, 'status': 'ORDER_AMBIGUOUS', 'guidance': '订单未能唯一确定，请先确认要处理的订单，不要重复拍下或付款。'}
    o = orders[0]
    try:
        d = platform_order(o, tools.identity)
        amount = int(str(d.get('payment')))
        if amount < 0:
            raise ValueError('invalid amount')
    except Exception:
        return {'ok': False, 'status': 'QUERY_UNKNOWN', 'guidance': '平台订单暂未查清，请先不要付款；可以稍后查询或请店家核对。'}
    qs = tools.service.quote_store.by_identity(tools.identity) if tools.service.quote_store else []
    q = qs[0] if qs else None
    from .demand import reference
    matched = bool(q and q.is_order_ready() and q.quote_id == o.quote_id
                   and q.provider == o.provider and q.total_sell_price_fen == o.amount_fen == amount
                   and _replaceable(o) and not o.payload.get('quote_revision_pending') and o.state != 'REPRICE_PENDING')
    unpaid = (type(d.get('orderStatus')) is int and d['orderStatus'] == 1
              and not d.get('payTime') and d.get('grouponPending') is False)
    result = {'ok': True, 'source': '平台实时查询', 'status': 'MATCHED' if matched and unpaid else 'NEEDS_REVIEW',
              'order_no': o.provider_order_id, 'platform_status': d.get('orderStatus'),
              'platform_amount_yuan': f'{amount / 100:.2f}', 'unpaid': unpaid,
              'quote': ({'cinema': q.cinema, 'movie': q.movie, 'show': q.show, 'ticket_count': q.ticket_count, 'provider': q.provider} if q else None),
              'matches_latest_quote': matched, 'payment_blocked': not (matched and unpaid), 'quote_valid': bool(q and q.is_order_ready()),
              'latest_quote_yuan': f'{q.total_sell_price_fen / 100:.2f}' if q and q.is_order_ready() else None}
    result['quote_reference'] = reference(q) if q else None
    if not unpaid:
        result['guidance'] = '平台订单不是已核实的待付款状态，请核对付款或出票进度，不要再改价或重复付款。'
    elif not _replaceable(o) or o.payload.get('quote_revision_pending'):
        result['guidance'] = '订单已有处理中的操作或出票记录，先核对原操作结果，不重复改价。'
    elif matched:
        result['guidance'] = f'已查询平台，原订单金额为 {amount / 100:.2f} 元，与本次购票报价一致；请核对影院、场次和张数后再付款。'
    elif not result['quote_valid']:
        result['guidance'] = '当前报价已失效或信息不完整，需要重新核价；请先不要付款。'
    else:
        result['action'] = 'REQUEST_REPRICE'
        result['guidance'] = f'平台原订单为 {amount / 100:.2f} 元，尚未核实更新为最新购票报价。可请求按最新报价改价，确认前请不要付款。'
    return result


def request_reprice(tools, *, order_no=None, quote_id=None, demand_id=None, demand_rev=None):
    if getattr(tools, 'purchase_request_result', None) is not None:
        return tools.purchase_request_result
    if not tools.cfg.fish_reprice.auto_reprice:
        return {'ok': False, 'guidance': '自动改价未开启，请店家处理。'}
    facts = inspect(tools)
    tools.platform_facts = facts
    if not facts.get('ok') or not facts.get('unpaid') or not facts.get('quote_valid'):
        return facts
    if facts.get('matches_latest_quote'):
        return facts
    qs = tools.service.quote_store.by_identity(tools.identity)
    q = qs[0]
    supplied = any(v is not None for v in (order_no, quote_id, demand_id, demand_rev))
    if q.demand_id or supplied:
        if (order_no != facts.get('order_no') or quote_id != q.quote_id
                or demand_id != q.demand_id or type(demand_rev) is not int or demand_rev != q.demand_rev):
            return {'ok': False, 'status': 'STALE_REFERENCE',
                    'guidance': '订单或报价版本已变化，请重新查询平台订单并使用查询返回的报价引用。'}
    stores = SimpleNamespace(orders=tools.service.order_store, quotes=tools.service.quote_store)
    commands = build_quote_reprice(stores, tools.identity, qs[0].quote_id, allow_wanda_switch=True,
                                  repair_bound=True, expected_order_no=facts['order_no'])
    if not commands:
        return {'ok': False, 'guidance': '当前订单不满足自动改价条件或已有待确认操作，请店家核对，先不要付款。'}
    # A command is only proposed here. The normal plugin gate checks platform state
    # again, reserves the revision, changes price, and verifies readback before settlement.
    from application.ticket_flow import FlowResult
    tools.flow_result = FlowResult(ok=True, state=tools.conv.state, channel=tools.conv.channel,
        messages=[], pending_commands=commands, template_keys=['AI_REPRICE_REQUEST'])
    tools.purchase_request_result = {'ok': True, 'status': 'REQUESTED',
        'guidance': '已提交按最新报价改价的请求，尚未确认成功；请先不要付款，等待核验结果。'}
    return tools.purchase_request_result
