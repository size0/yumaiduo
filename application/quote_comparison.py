"""Non-tradable comparison observations, isolated from executable quote records."""
from copy import deepcopy
from datetime import datetime, timezone
import re


def money_literals(text):
    """Compare price expressions, including common currency-free ticket units."""
    from .seller_context import price_literals
    values=[value for value,_ in price_literals(text)]
    values.extend(re.findall(r'(?:单价|每张|售价|报价|总价|合计|总计|自购(?:价)?|一共)\s*[:：]?\s*(\d+(?:\.\d+)?)',text))
    return values


def remember(conv,result,quote,*,provider,cost_total_fen,cost_source,buyer_total_fen,estimated=False,
             comparison_source='BUYER_SCREENSHOT'):
    conv.quote_comparison=dict(
        status='SELF_BUY_RECOMMENDED',tradable=False,quote_kind='COMPARISON_ONLY',
        **{k:str(getattr(conv,k,'') or '') for k in ('tenant_id','shop_id','buyer_id','chat_id','demand_id')},
        demand_rev=getattr(conv,'demand_rev',0),observed_at=datetime.now(timezone.utc).isoformat(),
        provider=provider,cinema=result.cinema_name,movie=result.movie_name,start_time=result.start_time,
        seat_count=quote.seat_count,seats=[s.get('seatNo') or s.get('seatName') for s in result.seats],
        cost_total_fen=cost_total_fen,cost_source=cost_source,cost_is_estimate=bool(estimated),
        sell_unit_fen=quote.unit_price_fen,sell_total_fen=quote.total_price_fen,
        comparison_source=comparison_source,comparison_total_fen=buyer_total_fen,
        buyer_total_fen=buyer_total_fen if comparison_source=='BUYER_SCREENSHOT' else None,
        pricing_rule_id=quote.pricing_rule_id,pricing_rule_revision=quote.pricing_rule_revision,
        rule_name=quote.rule_name,
        reason='本次规则售价不低于'+('截图自购总价' if comparison_source=='BUYER_SCREENSHOT' else '接口市场参考总价')+
               '；未生成可用于改价或下单的报价。')


def current(conv):
    value=getattr(conv,'quote_comparison',{})
    if (not isinstance(value,dict) or value.get('status')!='SELF_BUY_RECOMMENDED'
            or any(value.get(k)!=str(getattr(conv,k,'') or '') for k in
                   ('tenant_id','shop_id','buyer_id','chat_id','demand_id'))
            or value.get('demand_rev')!=getattr(conv,'demand_rev',0)):
        return None
    return deepcopy(value)


def lookup(tools):
    """Never restore a previous image's price into a pending or changed request."""
    ctx=tools.session.image_context
    if (ctx.get('unverified_image') or (ctx.get('ticket_details') or {}).get('pending')
            or any(ctx.get(k)!=v for k,v in tools.identity.items())):
        return None
    result=current(tools.conv)
    if result:
        return result
    if getattr(tools.conv,'quote_comparison',{}):
        return None
    # Old sessions did not persist comparison amounts; keep their known status
    # without reconstructing historical prices from a fresh supplier request.
    from domain.conversation import ConvState
    facts=ctx.get('recognition_facts') or {}
    if (tools.conv.state==ConvState.SELF_BUY and facts.get('match_level')=='EXACT'
            and ctx.get('demand_id','')==getattr(tools.conv,'demand_id','')
            and ctx.get('demand_rev',0)==getattr(tools.conv,'demand_rev',0)):
        return dict(status='SELF_BUY_RECOMMENDED',tradable=False,quote_kind='COMPARISON_ONLY',
            historical_amounts_unavailable=True,provider=tools.conv.channel or '',
            cinema=facts.get('cinema_name'),movie=facts.get('movie_name'),start_time=facts.get('start_time'),
            reason='已有截图和建议自购结果，但该历史记录未保存完整比价金额，需要店家重新核价。')
    return None


def explanation(tools):
    # Only finish a price lookup, never turn another buyer question into pricing.
    if not any(name=='read_quote' and result.get('status')=='SELF_BUY_RECOMMENDED'
               for name,result in getattr(tools,'tool_results',[])):
        return ''
    comparison=lookup(tools)
    if not comparison:
        return ''
    return render(comparison)


def render(comparison):
    """Typed money labels are rendered by code, never inferred from a bare amount."""
    if comparison.get('historical_amounts_unavailable'):
        return '影院、场次和座位已识别，上次比价后建议自购。但当时的完整报价未保存，需要店家重新核价，不用重复发送同一张截图。'
    label='截图自购合计' if comparison.get('comparison_source','BUYER_SCREENSHOT')=='BUYER_SCREENSHOT' else '接口市场参考合计'
    total=comparison.get('comparison_total_fen',comparison.get('buyer_total_fen'))
    return (f'上次核价共{comparison["seat_count"]}张，按本次规则为'
            f'{comparison["sell_unit_fen"]/100:.2f}元/张，合计{comparison["sell_total_fen"]/100:.2f}元；'
            f'{label}{total/100:.2f}元，所以建议您自购。'
            '这是上次比价结果，未生成可用于改价或下单的报价；如需继续购买，须重新核价。')
