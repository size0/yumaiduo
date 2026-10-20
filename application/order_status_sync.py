"""System notices trigger an authoritative read; notification text is not order truth."""
from datetime import datetime, timezone
from .purchase_actions import platform_order


def is_delivery_notice(event):
    p=event.get('payload') or {}
    return (event.get('event') in ('order.shipped','order.finished') or
            (str(p.get('messageType')) in ('14','28') and
             str(p.get('content') or '').strip() in ('你已发货','卖家已发货','买家已确认收货，交易成功')))


def sync_delivery(event, orders):
    """Refresh platform shipment truth only; never invent or resend ticket evidence."""
    from .plugin_runtime import _identity
    ident=_identity(event);p=event.get('payload') or {}
    if orders is None or not ident['tenant_id'] or not ident['shop_id']:return '缺少订单身份，未更新'
    number=str(p.get('orderId') or p.get('order_id') or '')
    if number:
        order=orders.find_by_provider_order(number)
        if not order or order.tenant_id!=ident['tenant_id'] or order.shop_id!=ident['shop_id']:
            return '未找到同店铺关联订单，未更新'
        if ident['buyer_id'] and order.buyer_id!=ident['buyer_id']:return '买家身份不一致，未更新'
    else:
        if not all(ident.values()):return '缺少会话身份，未更新'
        candidates=[o for o in orders.by_identity(ident) if o.provider_order_id and not o.is_terminal]
        if len(candidates)!=1:return '无法唯一确定发货订单，未更新'
        order=candidates[0];number=order.provider_order_id
    identity={k:str(getattr(order,k)) for k in ('tenant_id','shop_id','buyer_id','chat_id')}
    detail=platform_order(order,identity)
    if (str(detail.get('orderId'))!=number or str(detail.get('accountUnb'))!=order.shop_id
            or type(detail.get('orderStatus')) is not int or detail['orderStatus'] not in (3,4)
            or int(detail.get('payment',-1))!=order.amount_fen):
        return '平台未核实为已发货，未更新'
    with orders.lock:
        current=orders.get(order.order_id)
        if not current or current.to_dict()!=order.to_dict():return '订单已变化，未覆盖'
        current.shipped=True
        current.payload=dict(current.payload or {})
        current.payload['platform_delivery_sync']={'order_id':number,'status':detail['orderStatus'],
            'checked_at':datetime.now(timezone.utc).isoformat(),'event_id':str(event.get('id') or '')}
        orders.put(current)
    return '平台发货状态已同步；未补发二维码或发货命令'


def is_close_notice(event):
    p = event.get('payload') or {}
    return (str(p.get('messageType')) in ('14','28')
            and '关闭' in str(p.get('content') or '')
            and bool(p.get('orderId') or p.get('order_id')))


def sync_closed(event, orders):
    p = event.get('payload') or {}
    number = str(p.get('orderId') or p.get('order_id') or '')
    tenant = str(event.get('tenantId') or '')
    shop = str(p.get('accountUnb') or '')
    if not all((number, tenant, shop)) or orders is None:
        return '缺少订单身份，未更新'
    order = orders.find_by_provider_order(number)
    if not order or order.tenant_id != tenant or order.shop_id != shop:
        return '未找到同店铺关联订单，未更新'
    ident = {k:str(getattr(order,k)) for k in ('tenant_id','shop_id','buyer_id','chat_id')}
    detail = platform_order(order, ident)
    if (str(detail.get('orderId')) != number or str(detail.get('accountUnb')) != shop
            or type(detail.get('orderStatus')) is not int or detail['orderStatus'] != 6
            or detail.get('payTime')):
        return '平台未核实为未付款关闭，未更新'
    with orders.lock:
        current = orders.get(order.order_id)
        if (current is None or current.provider_order_id != number
                or current.payment_state != 'UNPAID' or current.paid_fen
                or current.shipped or current.ticket_codes
                or current.state not in ('CREATED','AWAITING_PAYMENT','REPRICE_PENDING','CANCELLED')):
            return '本地订单已有付款或履约状态，未覆盖'
        if current.state == 'CANCELLED':
            return '订单已同步关闭，无需重复处理'
        current.state = 'CANCELLED'
        current.payload = dict(current.payload or {})
        current.payload['platform_close_sync'] = {
            'order_id':number, 'status':6,
            'checked_at':datetime.now(timezone.utc).isoformat(),
            'event_id':str(event.get('id') or event.get('eventId') or '')}
        orders.put(current)
    return '平台未付款关闭已同步至本地订单'
