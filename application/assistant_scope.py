"""AI text reply stage gate; transaction workflows keep their own switches."""
from domain.conversation import ConvState

def stage_for(conv, order_store=None):
    state = conv.state
    stage = 'intake'
    if state in (ConvState.AWAIT_REPRICE, ConvState.AWAIT_PAY): stage = 'quote'
    if state in (ConvState.AWAIT_TICKET, ConvState.QT_FAIL): stage = 'order'
    if state in (ConvState.DELIVERED, ConvState.REFUNDING, ConvState.REFUNDED): stage = 'after_sale'
    identity = {k:str(getattr(conv,k,'') or '') for k in ('tenant_id','shop_id','buyer_id','chat_id')}
    if order_store is not None and all(identity.values()):
        from domain.order.models import TERMINAL_STATES
        orders = order_store.by_identity(identity)
        ranks = {'intake':0,'quote':1,'order':2,'after_sale':3}
        for order in orders:
            if any(str(getattr(order,k,''))!=v for k,v in identity.items()): continue
            same_quote = bool(getattr(conv,'quote_id','')) and order.quote_id==conv.quote_id
            if (order.is_terminal or order.state in TERMINAL_STATES or order.state=='CLOSED') and not same_quote: continue
            fact = 'intake'
            if order.shipped or order.state in ('DELIVERED','REFUNDED','REFUNDING'):
                fact = 'after_sale'
            elif order.payment_state == 'CONFIRMED': fact = 'order'
            elif order.platform_order_id or (order.provider=='WANDA' and order.provider_order_id): fact = 'quote'
            if ranks[fact]>ranks[stage]: stage=fact

    return stage

def allowed(assistant, conv, order_store=None):
    if not assistant.agent_enabled: return False
    if not assistant.stage_filter_enabled: return True
    return stage_for(conv, order_store) in assistant.allowed_stages
