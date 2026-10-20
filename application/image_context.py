"""Keep verified image facts separate from unverified later attachments."""
from copy import deepcopy
import re
from .ticket_flow import FlowResult


def preserve_image_result(session, previous, previous_conv, image_url, result):
    context = session.image_context
    context['recognition_facts'] = deepcopy(getattr(session.conversation, 'supplement_recognition', {}) or {})
    if 'IMAGE_CINEMA_CONTEXT' in result.template_keys:
        # A newly observed cinema is a pending new image, not permission to
        # restore the previous image's quote after supplier verification fails.
        context['verified'] = False
        return result
    if 'POSITION_SELECTION_REQUIRED' in result.template_keys:
        request = deepcopy(previous.get('awaiting_image_request') or previous.get('ticket_details') or {})
        request.update(pending=True, awaiting='seat_selection',
                       demand_id=getattr(session.conversation, 'demand_id', ''),
                       demand_rev=getattr(session.conversation, 'demand_rev', 0))
        context['ticket_details'] = request
        context['verified'] = context['recognition_facts'].get('match_level') == 'EXACT'
        return result
    if result.ok and any(k in result.template_keys for k in ('WANDA_UNIT_PRICE', 'QUOTE_CARD', 'QUOTE_CARD_MULTI', 'SELF_BUY_HINT')):
        context['verified'] = True
        context.update(demand_id=getattr(session.conversation,'demand_id',''),
                       demand_rev=getattr(session.conversation,'demand_rev',0))
        recognition = session.conversation.recognition
        context['summary'] = '｜'.join(str(getattr(recognition, k, '') or '') for k in ('cinema_name', 'movie_name', 'start_time'))
    elif not result.ok and any(k in result.template_keys for k in ('WANDA_QUOTE_FAILED', 'WANDA_MATCH_PENDING')):
        # Recognition succeeded; a missing supplier price is not an incomplete image.
        context['verified'] = False
        context['quote_failure'] = next(k for k in result.template_keys if k in ('WANDA_QUOTE_FAILED', 'WANDA_MATCH_PENDING'))
    elif not result.ok and previous.get('verified') and all(
            previous.get(k) == context.get(k) for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')):
        session.conversation.__dict__.clear()
        session.conversation.__dict__.update(deepcopy(previous_conv))
        session.image_context = deepcopy(previous)
        session.image_context['unverified_image'] = image_url
        session.image_context['unverified_context'] = deepcopy(context)
        result.messages = ['这张补充图暂未识别完整，上一张的购票信息已保留：\n'
                           + previous.get('summary', '') + '\n仍按上一场购买吗？如未换场，请回复“按上一张”和张数；换场请重发完整截图。']
        result.template_keys = ['IMAGE_CONTEXT_CONFIRM']
        result.need_human = False
        result.state = session.conversation.state
    return result


def handle_pending_image(service, session, text, cfg):
    context = session.image_context
    if not context.get('unverified_image'):
        return None
    from .buyer_assistant import BuyerAssistantTools, explicit_ticket_count
    confirmation = re.fullmatch(r'(按上一张|还是上一场|场次没变|同一场)[，,、\s]*(.*)', text.strip())
    remainder = confirmation.group(2).strip() if confirmation else ''
    count = explicit_ticket_count(remainder if confirmation else text)
    if count is not None:
        context['ticket_count'] = count
    confirmed = bool(confirmation and (not remainder or remainder in ('。', '！', '!') or count is not None))
    if confirmed:
        pending = context.pop('unverified_image')
        tool = BuyerAssistantTools(service, session, text, cfg, lambda _q: [])
        answer = tool._resume_quote(ticket_count=context.get('ticket_count'))
        if tool.flow_result is not None:
            if not tool.flow_result.ok:
                context['unverified_image'] = pending
            return tool.flow_result
        context['unverified_image'] = pending
        message = answer.get('reason', '请确认购票信息。')
    else:
        quantity = f'收到，需要{count}张。' if count is not None else ''
        message = quantity + '请确认是否仍是上一场：\n' + context.get('summary', '') + '\n未换场请回复“按上一张”；换场请发送新的完整截图。确认前不会改价。'
    return FlowResult(ok=True, state=session.conversation.state, channel=session.conversation.channel,
                      messages=[message], template_keys=['IMAGE_CONTEXT_CONFIRM'])
