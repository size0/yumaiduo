"""Bind buyer text to an image before any quote or order operation."""
from copy import deepcopy
from datetime import datetime, timezone
import re


def preimage_city(request,context,conv,max_age_hours):
    """Only a fresh, same-demand pre-image literal may guide recognition."""
    from .request_state import scoped,KEYS
    if context.get('url') or not scoped(conv,context) or context.get('unverified_image'):
        return ''
    if any(k in request and request[k]!=getattr(conv,k,'' if k=='demand_id' else 0 if k=='demand_rev' else None) for k in KEYS):
        return ''
    if (request.get('demand_id')!=getattr(conv,'demand_id','')
            or request.get('demand_rev')!=getattr(conv,'demand_rev',0)
            or request.get('source_image')):
        return ''
    city=request.get('city')
    if not isinstance(city,str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,16}',city):return ''
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(context['created_at'])).total_seconds()
        return city if 0<=age<=max(1,min(24,max_age_hours))*3600 else ''
    except (KeyError,TypeError,ValueError):return ''


def scoped_request(request, context, conv, identity, max_age_hours):
    """Migrate legacy child versions only from an explicit, current parent scope."""
    request = deepcopy(request)
    if all(key in request for key in ('demand_id', 'demand_rev')):
        return request
    if (not request.get('pending') or not context.get('url')
            or request.get('source_image') != context['url']
            or context.get('unverified_image')
            or any(context.get(k) != v for k, v in identity.items())
            or any(k not in context or context[k] != getattr(conv, k, default)
                   for k, default in (('demand_id', ''), ('demand_rev', 0)))
            or any(k in request and request[k] != context[k] for k in ('demand_id', 'demand_rev'))):
        return request
    try:
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(context['created_at'])).total_seconds()
        if not 0 <= age <= min(24, max(1, max_age_hours))*3600:
            return request
    except (KeyError, TypeError, ValueError):
        return request
    request.update(demand_id=context['demand_id'], demand_rev=context['demand_rev'])
    return request


def reconcile(conv, result, request, quote_store, out, *, verified_cinema_id=None):
    """Return a blocked result on ambiguity; otherwise use verified text seats.

    The agent extracts literal text. This gate binds it to the recognized show;
    Wanda's existing seat-map/price checks still decide availability and cost.
    """
    if not request:
        return None
    # Text/image consistency is supplier-independent. Only the routing layer may
    # select a provider using verified IDs; a cinema name is not route authority.
    from .ticket_details import CHINA
    from domain.conversation import Event
    from dataclasses import asdict
    normalize = lambda value: re.sub(r'[^\w\u4e00-\u9fff]', '', str(value or '')).casefold()
    try:
        stamp = datetime.fromisoformat(str(result.start_time).replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp.astimezone(CHINA)
        provided = any(request.get(key) for key in ('movie', 'date', 'time', 'cinema', 'city', 'seats'))
        same = (result.match_level == 'EXACT' and not result.cinema_truncated and provided
                and (not request.get('movie') or normalize(request['movie']) == normalize(result.movie_name))
                and (not request.get('date') or request['date'] == stamp.date().isoformat())
                and (not request.get('time') or request['time'] == stamp.strftime('%H:%M'))
                and (not request.get('city') or normalize(request['city']) == normalize(result.city_name))
                and not request.get('ambiguous_time'))
        if request.get('cinema'):
            cinema_verified=(verified_cinema_id is not None and result.cinema_id is not None
                             and str(verified_cinema_id)==str(result.cinema_id))
            same = same and (cinema_verified or normalize(request['cinema']) in normalize(result.cinema_name))
    except (TypeError, ValueError):
        same = False
    if request.get('seats') and request.get('ticket_count') and len(request['seats']) != request['ticket_count']:
        same = False
    count = request.get('ticket_count')
    version_matches = all(request.get(key, getattr(conv, key, default)) == getattr(conv, key, default)
                          for key, default in (('demand_id', ''), ('demand_rev', 0)))
    quantity_only = (type(count) is int and 1 <= count <= 99 and not any(request.get(key)
        for key in ('cinema', 'city', 'movie', 'date', 'time', 'seats', 'position_preference', 'ambiguous_time')))
    version_explicit = all(key in request for key in ('demand_id', 'demand_rev'))
    complete_show = all(request.get(key) for key in ('movie', 'date', 'time'))
    if not complete_show and not version_explicit:
        same = False
    if quantity_only and version_explicit and version_matches and result.match_level == 'EXACT' and not result.cinema_truncated:
        # Text is a quantity supplement, not a second complete show description.
        same = True
    same = same and version_matches
    quantity_conflict = bool(not request.get('seats') and type(count) is int
                             and result.seats and len(result.seats) != count)
    if quantity_conflict:
        same = False
    if request.get('position_preference') and not request.get('seats'):
        ident = {k: str(getattr(conv, k, '') or '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
        if quote_store is not None and all(ident.values()):
            quote_store.supersede_for_identity(ident, demand_id=getattr(conv, 'demand_id', '') or None)
        conv.quote = None; conv.quote_id = ''; conv.quote_kind = ''
        conv.supplement_recognition = asdict(result)
        out.ok = False
        out.state = conv.apply(Event.RECOGNIZE_FAIL).state
        out.template_keys = ['POSITION_SELECTION_REQUIRED']
        out.messages = ['已保留位置偏好：'+request['position_preference']+
                        '。这张图还不能直接作为该位置的报价，请确认目标具体几排几座；已有张数不变，余座和价格仍需核对。']
        return out
    if same and not complete_show and request.get('seats'):
        def labels(rows):
            return [s if isinstance(s,str) else
                    (s.get('seatNo') or s.get('seatName')) if isinstance(s,dict) else None for s in rows]
        supplied,observed=labels(request['seats']),labels(result.seats or [])
        if (supplied and observed and all(isinstance(s,str) and s for s in supplied+observed)
                and len(set(supplied))==len(supplied)
                and sorted(supplied)==sorted(observed)):
            result.expected_seat_count=len(supplied)
            conv.buyer_ticket_count=len(supplied)
            out.notes.append('文字座号与当前图片完全一致，仅确认张数；保留价格及座位核验标记')
            return None
    if same and complete_show and request.get('seats'):
        result.seats = deepcopy(request['seats'])
        result.expected_seat_count = len(result.seats)
        # Original screenshot seat prices/confirmation belong to other seats.
        result.seat_matched = True
        result.price_all_fen = None
        result.price_mismatch = False
        result.seat_confirm_required = False
        result.raw_results = {**(result.raw_results or {}), 'buyer_text_details': True}
        out.notes.append('文字指定座位已绑定同一影片日期场次，交万达座位图重新核价')
        return None
    if same and not request.get('seats') and type(count) is int and 1 <= count <= 99:
        # Keep screenshot seats when they agree with the supplied quantity.
        # Matching quantity is not proof that OCR prices or seat status are right.
        result.expected_seat_count = count
        conv.buyer_ticket_count = count
        out.notes.append('文字张数已补充到当前截图；保留识别座号及原有价格、座位核验标记，继续后续校验')
        return None
    if same and not request.get('seats') and count is None:
        # Text supplements the image; its missing quantity is not a conflict.
        # Preserve all OCR seat/count/price warnings for the original flow.
        out.notes.append('文字补充字段与截图一致，沿用截图事实继续核验座位和价格')
        return None
    ident = {k: str(getattr(conv, k, '') or '') for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
    if quote_store is not None and all(ident.values()):
        quote_store.supersede_for_identity(ident, demand_id=getattr(conv, 'demand_id', '') or None)
    conv.quote = None; conv.quote_id = ''; conv.quote_kind = ''
    conv.supplement_recognition = asdict(result)
    out.ok = False
    out.state = conv.apply(Event.RECOGNIZE_FAIL).state
    out.template_keys = ['TEXT_IMAGE_CONFIRM']
    out.messages = ['文字购票需求与这张截图还未核对一致。请确认影片、日期、开场时间和目标排座；确认前不按截图中的其他座位报价。']
    if quantity_conflict:
        out.messages = [f'您说要{count}张，但这张截图识别到{len(result.seats)}个座位，请确认要几张、哪几个位置。']
    if not version_matches:
        out.messages = ['之前的购票需求已经变化，不能把旧需求套到这张截图；请确认本次要几张及目标位置。']
    return out
