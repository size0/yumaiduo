"""Buyer-safe explanations for business outcomes; never expose raw API errors."""

MESSAGES = {
    'REQUEST_FIELDS_UNSUPPORTED': '本轮声明的购票字段缺少原话佐证，尚未修改报价；需要按实际要求重新核对。',
    'REQUEST_REVALIDATION_REQUIRED': '先前图片或需求已超过核验时效，需要重新核对当前排片；旧报价不能用于付款。',
    'REQUEST_PLAN_REQUIRED': '本轮购票需求还未完整核对，需要先确认本次要调整的内容；尚未重新报价。',
    'REQUEST_FIELDS_INCOMPLETE': '本轮购票信息还未合并完整，需要先核对补充的位置或场次；尚未重新报价，请勿按旧价付款。',
    'PAID_ORDER_LOCKED': '这笔订单已付款或正在处理，不能自动修改购票需求，需要人工核对付款和出票进度。',
    'ORDER_AMBIGUOUS': '查到的订单还不能唯一确定，请确认要处理的是哪一笔，或发对应订单卡片；请勿重复付款或下单。',
    'NO_BOUND_ORDER': '当前会话尚未关联到平台订单。若已经拍下，请发对应订单卡片供核对；这不表示您没有拍下。',
    'QUERY_UNKNOWN': '平台订单暂时未能查清，无法确认当前金额或改价结果。请先不要重复付款，可请店家核对。',
    'HISTORICAL_EVIDENCE_UNAVAILABLE': '本次没有取得可核对的订单证据，无法确认金额或改价结果；需要对应订单卡片或店家核对。',
    'NEEDS_QUOTE': '本次场次尚未取得有效报价，需要重新核价；原订单未修改，请先不要按旧报价付款。',
    'IMAGE_CONFIRMATION_REQUIRED': '张数已经记下，但最新图片的场次还未核实。请确认沿用上一场还是换场，确认前不会改价。',
    'STALE_REFERENCE': '订单或报价版本已经变化，本次改价尚未完成，需要重新核对当前报价；请先不要付款。',
    'INVALID_FIELD_EVIDENCE': '这次补充的信息还未核对通过，请确认要修改的具体内容；原订单未修改。',
    'INVALID_EVIDENCE': '这次补充的信息还未核对通过，请确认本次需要的张数或具体内容；原订单未修改。',
    'ORDER_STATE_LOCKED': '当前订单进度需要先核对，暂不能自动替换购票信息，请店家核对原订单。',
}


def explain(name, result):
    # Only locally authored public reasons or a known business status may be sent.
    # reason/guidance can contain exception strings from adapters, so never echo them.
    status = str(result.get('status') or '')
    if status == 'NO_BOUND_ORDER' and result.get('purchase_ready') is True:
        return ('本次报价有效，确认购票信息后可以拍下；如果已经拍下，请勿重复下单，'
                '可发订单卡片核对。收到对应订单并核验前，尚不能确认改价。')
    if result.get('source')=='local_flow' and result.get('public_reason'):
        return result['public_reason']
    if status == 'HISTORICAL_EVIDENCE_UNAVAILABLE':
        if name == 'read_ticket_progress':
            return '本次没有取得可核对的订单及出票进度记录，暂不能确认是否已出票；需要店家核对对应订单。'
        if name == 'read_quote':
            return '本次没有取得可核对的报价记录，不能确认当前售价；需要核对本次场次及实际价格。'
        if name not in {'read_order', 'read_platform_order', 'read_refund_status', 'request_latest_quote_reprice', 'get_purchase_guidance'}:
            return '本次购票信息所需的查询记录缺失，尚不能确认核价结果；请核对尚缺的场次或位置信息。'
    reason_code = str(result.get('reason_code') or '')
    if status == 'BLOCKED' and reason_code in MESSAGES:
        return MESSAGES[reason_code]
    if status in MESSAGES:
        return MESSAGES[status]
    if name == 'request_latest_quote_reprice':
        return '本次改价申请尚未确认完成，需要核对订单和当前报价；请先不要付款或重复下单。'
    if name in {'read_order', 'read_platform_order', 'read_refund_status'}:
        return '这次订单查询没有取得可核实的结果，需要对应订单卡片或店家核对；请勿重复付款。'
    if name == 'read_quote':
        return '这次报价暂未查询成功，不能确认当前价格，需要重新核价。'
    return '这次购票信息还未核对通过，暂不能确认已更新或已重新报价；请确认需要补充的具体信息。'


def pending_request(tools):
    """Only explicitly versioned, same-identity text belongs to this demand."""
    ctx = tools.session.image_context
    if (ctx.get('unverified_image') or
            any(str(ctx.get(k, '')) != v for k, v in tools.identity.items())):
        return {}
    saved = ctx.get('ticket_details') or ctx.get('awaiting_image_request') or {}
    if (saved.get('demand_id') != getattr(tools.conv, 'demand_id', '') or
            type(saved.get('demand_rev')) is not int or
            saved['demand_rev'] != getattr(tools.conv, 'demand_rev', 0)):
        return {}
    return saved


def already_saved_fields(tools, args):
    """A rejected redundant input need not invalidate authoritative saved facts."""
    from .ticket_details import parse_date, parse_time, parse_seats
    saved = pending_request(tools)
    if not saved:
        return set()
    matched = set()
    for key in ('cinema', 'movie', 'date', 'time', 'seats', 'position_preference'):
        value = args.get(key)
        if not value:
            continue
        try:
            if key == 'date':
                value = parse_date(value)
            elif key == 'time':
                value, ambiguous = parse_time(value)
                if ambiguous or saved.get('ambiguous_time'):
                    continue
            elif key == 'seats':
                value = parse_seats(value)
        except (TypeError, ValueError):
            continue
        if saved.get(key) == value:
            matched.add(key)
    if args.get('quantity') == saved.get('ticket_count') and type(args.get('quantity')) is int:
        matched.add('ticket_count')
    return matched
