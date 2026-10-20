"""Buyer-grounded multi-turn demand; only supplier facts may enter pricing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

CHINA = timezone(timedelta(hours=8))


def now():
    return datetime.now(CHINA)


def extract(text, pending=False):
    """Recognize explicit compact messages; general language uses the agent tool."""
    result = {}
    if pending and text.strip(' 。！!') in ('上午','早上','下午','晚上'):
        return {'time':text.strip(' 。！!')}
    time = re.search(r'(?<!\d)(?:上午|早上|下午|晚上)?\s*\d{1,2}[:：]\d{2}(?!\d)', text)
    seats = re.search(r'\d{1,2}排\s*\d{1,2}(?:[，,、]\s*\d{1,2})*(?:座|列)?', text)
    date = re.search(r'今天|明天|后天|\d{4}-\d{1,2}-\d{1,2}|(?<!\d)\d{1,2}[-/]\d{1,2}(?!\d)|\d{1,2}月\d{1,2}[日号]?', text)
    if not (time or seats or (pending and date)):
        return {}
    # Alternatives/negation are left to the agent; never pick one automatically.
    if re.search(r'不要|不是|不买|不选|别选|别买|或者|还是|能不能|有没有|吗(?:$|[。！!])|是否|能否|可以|可否|多少钱|什么价|价格|贵不贵|便宜|[?？]', text):
        return {}
    for key, match in [('time',time),('seats',seats),('date',date)]:
        if match:
            result[key] = match.group().strip()
    if time and seats:
        movie = text[time.end():seats.start()].strip(' ，,《》')
        if movie and not re.search(r'今天|明天|多少钱|要|买|\d', movie):
            result['movie'] = movie
    return result


def parse_date(value):
    if value in ('今天','明天','后天'):
        return (now().date()+timedelta(days={'今天':0,'明天':1,'后天':2}[value])).isoformat()
    short=re.fullmatch(r'(\d{1,2})(?:[-/]|月)(\d{1,2})(?:日|号)?',value)
    try:
        if short:
            return datetime(now().year,int(short.group(1)),int(short.group(2))).date().isoformat()
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ValueError('日期没有识别清楚，请回复“今天”“明天”或完整日期，例如2026-10-16。') from None


def cinema_confirmation(session, text):
    """A short yes only answers the current, named cinema question."""
    if text.strip(' 。！!') not in ('是','是的','对','对的','没错','就是这家'):
        return ''
    ctx=session.image_context
    demand=ctx.get('ticket_details') or {}
    facts=ctx.get('recognition_facts') or {}
    name=facts.get('cinema_name') or ''
    if not demand.get('pending') or not name or not facts.get('cinema_id'):
        return ''
    if demand.get('awaiting') not in (None,'cinema_confirmation'):
        return ''
    # Also recognize the explicit pre-release question after one failed handoff.
    for row in reversed(session.history[-4:]):
        if row.get('role')!='assistant':continue
        message=row.get('content','')
        if message.startswith('截图影院名称被截断，请确认是否为：'+name):
            return name
        if not ('转人工' in message or '呼叫人工' in message):
            return ''
    return ''


def parse_time(value, *, evidence=None):
    # Normalize a model-selected literal time, never extract arbitrary numbers
    # from a sentence (seat numbers and ticket counts are separate fields).
    literal = value
    if re.fullmatch(r'\d{4}', value):
        value = value[:2] + ':' + value[2:]
    if evidence and not re.match(r'^(上午|早上|下午|晚上)', literal):
        # Preserve only a period immediately attached to the exact submitted
        # literal. A period elsewhere in the sentence is not time evidence.
        periods = set()
        for occurrence in re.finditer(re.escape(literal), evidence):
            prefix = re.search(r'(上午|早上|下午|晚上)\s*$', evidence[:occurrence.start()])
            if prefix:
                periods.add(prefix.group(1))
        if len(periods) > 1:
            raise ValueError('这条消息包含不同时间段，请确认要哪一个开场时间。')
        if periods:
            value = periods.pop() + value
    from .semantic_request import number
    words = re.fullmatch(r'(上午|早上|下午|晚上)?\s*([零〇一二两三四五六七八九十\d]{1,3})[点时](?:([零〇一二两三四五六七八九十\d]{1,3})分?|([半]))?', value)
    if words:
        def component(raw):
            if not raw or raw in ('零', '〇'): return 0
            if raw.isdigit(): return int(raw)
            return number(raw)
        hour = component(words.group(2))
        minute = 30 if words.group(4) else component(words.group(3))
        if hour is None or minute is None:
            raise ValueError('开场时间格式不正确，请核对。')
        value = f'{words.group(1) or ""}{hour}:{minute:02d}'
    m = re.fullmatch(r'(上午|早上|下午|晚上)?\s*(\d{1,2})[:：.](\d{2})', value)
    if not m:
        raise ValueError('请提供明确的开场时间，例如晚上21:15。')
    period,h,minute = m.group(1),int(m.group(2)),int(m.group(3))
    if h == 12 and period in ('晚上', '上午', '早上'):
        raise ValueError('请确认是中午12点，还是凌晨0点；如果是次日凌晨，也请说明具体观影日期。')
    if h>23 or minute>59 or (period in ('上午','早上') and h>12):
        raise ValueError('开场时间格式不正确，请核对。')
    if period in ('下午','晚上') and h<12:h+=12
    if period in ('上午','早上') and h==12:h=0
    return f'{h:02d}:{minute:02d}', not period and 1<=h<=12


def parse_seats(value):
    from .semantic_request import number
    numeral = r'[0-9一二两三四五六七八九十]{1,3}'
    # Explicit row/seat pairs may each repeat their row. Keep the literal
    # evidence intact; only convert its formatting for the supplier interface.
    parts = re.split(r'(?<=[座列])(?:\s*(?:[，,、；;]|以及|和)\s*|\s+)(?=第?' + numeral + r'排)', value.strip())
    if len(parts) > 1:
        seats = [seat for part in parts for seat in parse_seats(part)]
        if len({(s['rowNo'], s['colNo']) for s in seats}) != len(seats):
            raise ValueError('座位号重复或不正确，请核对。')
        return seats
    m=re.fullmatch(r'第?('+numeral+r')排\s*(?:的\s*)?(.+)', value.strip())
    if not m:raise ValueError('请按“7排10、11、12座”补充具体位置。')
    # Tokenize the original literal, requiring a comma or 座/列 between numbers.
    tail = m.group(2)
    token = re.compile(r'('+numeral+r')(?:[座列]\s*(?:[，,、]\s*)?|[，,、]\s*|$)')
    cols=[]
    while tail:
        part = token.match(tail)
        if not part:raise ValueError('座位未识别清楚，请说明几排几座，或在选座图上标出位置。')
        cols.append(number(part.group(1)))
        tail=tail[part.end():]
    row=number(m.group(1))
    if row is None or row<1 or not cols or any(c is None or c<1 for c in cols) or len(set(cols))!=len(cols):
        raise ValueError('座位号重复或不正确，请核对。')
    return [{'rowNo':row,'colNo':c,'seatName':f'{row}排{c}座','seatNo':f'{row}排{c}座'} for c in cols]


def respond(tool, message, ok=True, *, status=None, observation=None):
    if getattr(tool,'semantic_mode',False):
        from .request_observation import project
        tool.flow_result=None
        return project(tool,ok,status,message,observation)
    from .ticket_flow import FlowResult
    tool.flow_result=FlowResult(ok=ok,state=tool.conv.state,channel=tool.conv.channel,
                               messages=[message],template_keys=['TICKET_DETAILS'])
    result = {'ok':ok,'reason':message,'demand':tool.session.image_context.get('ticket_details',{})}
    if status:
        result.update(status=status, reason_code=status)
    return result


def supplement(tool, evidence, movie='', date='', time='', seats='', cinema='', ticket_count=None, position_preference='', city=''):
    from domain.conversation import ConvState, Event
    from integrations.liangpiao.api import RecognitionResult, MATCH_EXACT
    from .ticket_flow import FlowResult
    if evidence != tool.text:
        return {'ok':False,'reason':'必须提交本轮买家完整原话。'}
    if city and tool.session.image_context.get('url'):
        return {'ok':False,'status':'INVALID_FIELD_EVIDENCE',
                'reason':'已有图片时请用城市补充工具重新核验，不能仅更新文字城市后沿用旧图报价。'}
    if not getattr(tool, 'semantic_mode', False) and re.search(r'不要|不是|不买|不选|别选|别买|或者|还是|能不能|有没有|吗(?:$|[。！!])|是否|能否|可以|可否|多少钱|什么价|价格|贵不贵|便宜|[?？]',evidence):
        return {'ok':False,'reason':'这条是询问、否定或存在备选，不能当作变更确认。请先理解并澄清，不修改需求或报价。'}
    fields={k:v.strip() for k,v in dict(movie=movie,date=date,time=time,seats=seats,cinema=cinema,position_preference=position_preference,city=city).items() if v}
    if seats and position_preference:
        return {'ok': False, 'status': 'INVALID_FIELD_EVIDENCE', 'reason': '具体座号和区域偏好请分开确认，不能同时当作已确定座位。'}
    confirmed_name=cinema_confirmation(tool.session,evidence)
    if (getattr(tool, 'semantic_mode', False) and
            (tool.session.image_context.get('ticket_details') or {}).get('awaiting') == 'cinema_confirmation'):
        confirmed_name=(tool.session.image_context.get('recognition_facts') or {}).get('cinema_name')
    invalid = [k for k,v in fields.items() if v not in evidence
               and not (k=='cinema' and v==confirmed_name)
               and (getattr(tool,'referenced_fields',{}).get(k) or {}).get('value') != v]
    if (not fields and ticket_count is None) or invalid:
        return {'ok':False,'status':'INVALID_FIELD_EVIDENCE',
                'reason':'补充字段必须逐字来自本轮买家或已核验消息引用，不能猜测或改写。',
                'invalid_fields':invalid,
                'next_actions':['保留本轮真实字段；历史字段先read_buyer_context取得有效引用并用source_refs提交，未确认字段留空或追问。']}
    signature = (movie, date, time, seats, cinema, ticket_count, position_preference)
    seen = getattr(tool, 'supplement_seen', set())
    if (signature in seen or (tool.city_attempted and not getattr(tool, 'semantic_mode', False))):
        return {'ok':False,'reason':'本轮已处理报价，请勿重复调用。'}
    from .quote_confirmation import unchanged_details
    unchanged = unchanged_details(tool,movie=movie,date=date,time=time,seats=seats,
                                  cinema=cinema,ticket_count=ticket_count,position_preference=position_preference)
    if unchanged is not None:
        return unchanged
    seen.add(signature)
    tool.supplement_seen = seen
    ctx=tool.session.image_context
    if not ctx.get('url') and getattr(tool, 'semantic_mode', False) and tool.conv.channel in (None, '', 'wanda'):
        from .buyer_assistant import CLOSED_STATES
        try:
            from .purchase_continuation import relevant_orders
            active_orders = relevant_orders(tool)
        except Exception:
            return {'ok': False, 'status': 'QUERY_UNKNOWN'}
        if (not getattr(tool.conv, 'demand_inquiry', False) and
                any(o.state not in CLOSED_STATES and (o.payment_state != 'UNPAID' or
                    (o.payload or {}).get('quote_revision_pending') or o.state == 'REPRICE_PENDING')
                    for o in active_orders)):
            return {'ok': False, 'status': 'PAID_ORDER_LOCKED'}
        try:
            same_identity = all(ctx.get(k) == v for k, v in tool.identity.items())
            pending = deepcopy(ctx.get('awaiting_image_request') or {}) if same_identity else {}
            version = {'demand_id': getattr(tool.conv, 'demand_id', ''),
                       'demand_rev': getattr(tool.conv, 'demand_rev', 0)}
            if pending and any(pending.get(k, v) != v for k, v in version.items()):
                pending = {}
            pending.update(version)
            if seats: pending['seats'] = parse_seats(seats)
            if position_preference:
                pending['position_preference'] = position_preference
                pending.pop('seats', None)
            if movie: pending['movie'] = movie
            if date: pending['date'] = parse_date(date)
            if time: pending['time'], pending['ambiguous_time'] = parse_time(time, evidence=evidence)
            if cinema: pending['cinema'] = cinema
            if city:pending['city']=city
            if ticket_count is not None:
                if type(ticket_count) is not int or not 1 <= ticket_count <= 99:
                    raise ValueError('张数不正确，请核对。')
                if pending.get('seats') and len(pending['seats']) != ticket_count:
                    raise ValueError('张数与已指定座位不一致，请确认保留哪些位置。')
                pending['ticket_count'] = ticket_count
            if pending.get('seats') and pending.get('ticket_count') and len(pending['seats']) != pending['ticket_count']:
                raise ValueError('张数与已指定座位不一致，请确认需要几张、保留哪些位置。')
        except ValueError as exc:
            return respond(tool, str(exc), False)
        pending['evidence'] = evidence
        provenance = pending.setdefault('field_sources', {})
        for key in fields:
            provenance.pop(key, None)
        provenance.update(getattr(tool, 'referenced_fields', {}))
        tool.session.image_context = {**(ctx if same_identity else {}), **tool.identity,
                                      'awaiting_image_request': pending}
        tool.session.image_context.setdefault('created_at',datetime.now(timezone.utc).isoformat())
        missing = [label for key, label in [('cinema', '影院名称'), ('movie', '影片名'),
                                           ('date', '观影日期'), ('time', '开场时间')]
                   if not pending.get(key)]
        if not pending.get('seats') and not pending.get('ticket_count'):
            missing.append('张数')
        if pending.get('ambiguous_time'):
            missing.append('开场时间是上午还是晚上')
        question = ('还需要确认' + '、'.join(missing) + '；') if missing else ''
        return respond(tool, '已记下文字购票需求。' + question + '请发对应影院场次截图，以便核对影院和实际价格。')
    if not ctx.get('url') or any(ctx.get(k)!=v for k,v in tool.identity.items()):
        return respond(tool,'请先发送当前影院的截图，再补充购票信息。',False)
    if ctx.get('unverified_image'):
        # Explicit new details belong to the latest image, never the old quote.
        ctx=deepcopy(ctx.get('unverified_context') or {**tool.identity,'url':ctx['unverified_image']})
    facts=ctx.get('recognition_facts') or {}
    from .wanda_position import available as wanda_image_available
    can_verify_image=wanda_image_available(tool,facts)
    if tool.conv.channel!='wanda' and '万达' not in str(facts.get('cinema_name','')) and not can_verify_image:
        return respond(tool,'这项多轮补充目前用于万达，请发送完整选座截图继续核价。',False)
    if not tool.cfg.recognize_quote.auto_recognize:
        return respond(tool,'自动识别报价已关闭，请店家核对。',False)
    allowed={ConvState.IDLE,ConvState.RECOGNIZING,ConvState.QUOTED,ConvState.QT_FAIL,ConvState.REWORK,
             ConvState.PICK_CINEMA,ConvState.PICK_MOVIE,ConvState.PICK_SHOW,ConvState.AWAIT_PAY,ConvState.AWAIT_REPRICE,
             ConvState.SELF_BUY}
    if tool.conv.state not in allowed:
        return respond(tool,'当前订单进度需要先核对，暂不替换购票信息。',False,status='ORDER_STATE_LOCKED')
    try:
        from .purchase_continuation import relevant_orders
        orders=relevant_orders(tool)
    except Exception:
        return respond(tool,'订单状态暂未查清，先不要付款，待核对后继续报价。',False,status='QUERY_UNKNOWN')
    from .buyer_assistant import CLOSED_STATES
    if not getattr(tool.conv, 'demand_inquiry', False) and any(o.state not in CLOSED_STATES and (o.payment_state!='UNPAID' or
           (o.payload or {}).get('quote_revision_pending') or o.state=='REPRICE_PENDING') for o in orders):
        return respond(tool,'原订单已付款或有操作待确认，需先核对原订单，本次不替换报价。',False,status='PAID_ORDER_LOCKED')
    if (seats and can_verify_image and not any((movie,date,time,cinema,position_preference))
            and not (ctx.get('ticket_details') or {}).get('cleared_fields')):
        from .wanda_position import supplement as supplement_wanda_position
        return supplement_wanda_position(tool,seats,ticket_count)
    demand=deepcopy(ctx.get('ticket_details') or {})
    # Only inherit the current, verified exact image. A new inquiry or an
    # unverified attachment must never silently borrow the old show.
    if ((not demand or demand.get('awaiting') == 'correction')
            and ctx.get('verified') and facts.get('match_level') == MATCH_EXACT
            and not getattr(tool.conv, 'demand_inquiry', False)):
        try:
            stamp = datetime.fromisoformat(str(facts.get('start_time') or '').replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp.astimezone(CHINA)
            for key, value in dict(movie=facts.get('movie_name'), date=stamp.date().isoformat(),
                                   time=stamp.strftime('%H:%M'), ambiguous_time=False).items():
                if key not in (demand.get('cleared_fields') or []):
                    demand.setdefault(key, value)
            if demand.get('awaiting') != 'correction':
                demand.setdefault('seats', deepcopy(facts.get('seats') or []))
        except ValueError:
            pass
    try:
        if cinema:
            demand['cinema']=cinema
            if cinema==facts.get('cinema_name'):
                demand['cinema_confirmed']=cinema
        if movie:demand['movie']=movie
        if date:demand['date']=parse_date(date)
        if time:
            value=time+demand.get('time','') if time in ('上午','早上','下午','晚上') else time
            demand['time'],demand['ambiguous_time']=parse_time(value, evidence=evidence)
        if seats:
            demand['seats']=parse_seats(seats)
            demand['ticket_count']=len(demand['seats'])
            demand.pop('position_preference', None)
        if position_preference:
            demand['ticket_count'] = (demand.get('ticket_count') or ctx.get('ticket_count')
                                      or len(demand.get('seats') or []) or None)
            demand['position_preference'] = position_preference
            demand.pop('seats', None)
        if ticket_count is not None:
            if type(ticket_count) is not int or not 1 <= ticket_count <= 99:
                raise ValueError('张数不正确，请核对。')
            if demand.get('seats') and len(demand['seats']) != ticket_count:
                raise ValueError('张数与已指定座位不一致，请确认保留哪些位置。')
            demand['ticket_count']=ticket_count
    except ValueError as exc:
        return respond(tool,str(exc),False)
    cleared=set(demand.get('cleared_fields') or [])-set(fields)
    if ticket_count is not None:cleared.discard('ticket_count')
    if seats or position_preference:cleared.difference_update(('seats','position_preference'))
    demand['cleared_fields']=sorted(cleared)
    tool.city_attempted=True
    demand['pending']=True
    provenance = demand.setdefault('field_sources', {})
    for key in fields:
        provenance.pop(key, None)
    provenance.update(getattr(tool, 'referenced_fields', {}))
    tool.session.image_context=ctx
    demand.update(demand_id=getattr(tool.conv, 'demand_id', ''),
                  demand_rev=getattr(tool.conv, 'demand_rev', 0))
    ctx['ticket_details']=demand
    demand.setdefault('source_image', ctx.get('url'))
    if seats:
        ctx.pop('awaiting_image_request', None)
    # A changed demand cannot be fulfilled or repriced with a previous quote.
    if tool.service.quote_store:
        tool.service.quote_store.supersede_for_identity(
            tool.identity, demand_id=getattr(tool.conv, 'demand_id', '') or None)
    tool.conv.quote=None;tool.conv.quote_id='';tool.conv.quote_kind=''
    tool.conv.quote_comparison={}
    requested_cinema=demand.get('cinema')
    saved_match=demand.get('cinema_match') or {}
    matched_before=(saved_match.get('input')==requested_cinema and
                    str((saved_match.get('cinema') or {}).get('cinemaId'))==str(facts.get('cinema_id')))
    if requested_cinema and requested_cinema!=facts.get('cinema_name') and not matched_before:
        from .cinema_matching import match_cinema
        matched=match_cinema(tool.service.flow.api,requested_cinema,
            city_code=(facts.get('final_results') or {}).get('cityCode') or '',
            city_name=facts.get('city_name') or '')
        if matched['status']!='MATCHED':
            demand['awaiting']='cinema_identity'
            return respond(tool,matched['reason'],True,observation=matched)
        selected_cinema=matched['cinema']
        different=str(selected_cinema['cinemaId'])!=str(facts.get('cinema_id'))
        facts=deepcopy(facts)
        if different:
            # A new branch cannot inherit a seat/show from the previous branch.
            for key in ('show_id','movie_id','movie_name','start_time','hall_name','seats','prices','raw_results',
                        'price_all_fen','price_mismatch','seat_matched','candidates'):
                facts.pop(key,None)
            for key,explicit in (('movie',movie),('date',date),('time',time),('seats',seats)):
                if not explicit:
                    demand.pop(key,None)
            ctx['verified']=False
        facts.update(cinema_id=selected_cinema['cinemaId'],cinema_name=selected_cinema['name'],
            cinema_address=selected_cinema['address'],city_name=selected_cinema['cityName'],
            cinema_truncated=False,cinema_hit_nums=1,match_level=MATCH_EXACT,
            final_results={'cityCode':selected_cinema['cityCode']})
        ctx['recognition_facts']=facts
        demand['cinema_confirmed']=selected_cinema['name']
        demand['cinema_match']={**matched,'input':requested_cinema}
    if demand.get('position_preference') and not demand.get('seats'):
        demand['awaiting']='seat_selection'
        count = demand.get('ticket_count')
        quantity = f'，仍按{count}张保留' if count else '，请告诉我需要几张'
        return respond(tool, '已记下位置偏好：'+demand['position_preference']+quantity+
                       '。原位置报价不再用于新位置；请确认目标具体几排几座，再核对余座和价格。')
    missing=[label for key,label in [('movie','影片名'),('date','观影日期'),('time','开场时间')] if not demand.get(key)]
    if not demand.get('seats') and not demand.get('ticket_count'):
        missing.append('张数或具体座位')
    if missing:
        demand['awaiting']='details'
        return respond(tool,'已记下这次补充，请再告诉我'+ '、'.join(missing)+'。核对完成后重新报价，请先不要付款。')
    if cleared:
        from .request_observation import LABELS
        return respond(tool,'已保留补充信息，还需重新确认'+ '、'.join(LABELS.get(k,k) for k in sorted(cleared))+'；不沿用已撤回内容。')
    if demand.get('ambiguous_time'):
        demand['awaiting']='time_period'
        return respond(tool,f'已记下 {demand["date"]}《{demand["movie"]}》，共{len(demand.get("seats") or []) or demand.get("ticket_count")}张。{demand["time"]}是上午还是晚上？请提供明确的开场时间；散场时间不能用于购票。')
    if not facts.get('cinema_id') or (facts.get('match_level') != MATCH_EXACT and int(facts.get('cinema_hit_nums') or 0)>1):
        demand['awaiting']='cinema_identity'
        return respond(tool,'购票信息已保留，但影院还未唯一确认，请补充城市和完整影院名称或完整截图。',
                       observation=dict(code='CINEMA_NOT_UNIQUE',cinema_name=facts.get('cinema_name'),
                                        city_name=facts.get('city_name'),match_count=facts.get('cinema_hit_nums')))
    if facts.get('cinema_truncated') and demand.get('cinema_confirmed')!=facts.get('cinema_name'):
        demand['awaiting']='cinema_confirmation'
        return respond(tool,'截图影院名称被截断，请确认是否为：'+str(facts.get('cinema_name') or '')+'。若是，回复“是”即可；若不是，请发完整影院截图。',
                       observation=dict(code='TRUNCATED_CINEMA_CONFIRMATION',cinema_name=facts.get('cinema_name'),city_name=facts.get('city_name')))
    demand['awaiting']='show_verification'
    try:
        api=tool.service.flow.api
        # Supplier uses business dates (08:00 boundary), buyer supplies calendar date.
        day=datetime.strptime(demand['date'],'%Y-%m-%d').date()
        business_day=day-timedelta(days=1) if int(demand['time'][:2])<8 else day
        shows=api.ch4_show_list(facts['cinema_id'],business_day.isoformat())
        def start(row):
            dt=datetime.fromisoformat(str(row.get('startTime') or '').replace('Z','+00:00'))
            return dt.replace(tzinfo=CHINA) if dt.tzinfo is None else dt.astimezone(CHINA)
        matches=[]
        from .cinema_matching import title_key
        for row in shows:
            if title_key(row.get('movieName'))!=title_key(demand['movie']):continue
            try:dt=start(row)
            except ValueError:continue
            if dt.date()==day and dt.strftime('%H:%M')==demand['time']:matches.append(row)
        if len(matches)!=1:
            return respond(tool,'已查询该影院排片，'+('没有找到' if not matches else '找到多个')+f' {demand["date"]} {demand["time"]}《{demand["movie"]}》的唯一场次，请核对影片和开场时间，或发送对应场次截图。',
                           observation=dict(code='SHOW_NOT_UNIQUE',match_count=len(matches),
                               requested_show={k:demand[k] for k in ('movie','date','time')},
                               candidates=[{k:r.get(k) for k in ('showId','startTime','hallName','movieName')} for r in matches]))
        selected=matches[0]
        if not selected.get('showId'):raise ValueError('场次编号缺失')
        detail=api.ch4_show_detail(str(selected['showId']))
        if (str(detail.get('cinemaId'))!=str(facts['cinema_id']) or title_key(detail.get('movieName'))!=title_key(demand['movie'])
            or start(detail).date()!=day or start(detail).strftime('%H:%M')!=demand['time']
            or start(detail)<=now() or str(detail.get('showId'))!=str(selected['showId'])):
            return respond(tool,'场次详情与本次需求不一致或已经开场，请重新核对场次。',False)
        verified=deepcopy(demand.get('seats') or [])
        count=len(verified) or demand.get('ticket_count')
        result=RecognitionResult(match_level=MATCH_EXACT,cinema_id=facts['cinema_id'],
            cinema_name=facts.get('cinema_name'),cinema_address=facts.get('cinema_address'),
            cinema_truncated=bool(facts.get('cinema_truncated')),
            city_name=facts.get('city_name'),brand_name=facts.get('brand_name') or '万达影城',
            movie_id=detail.get('movieId'),movie_name=detail['movieName'],show_id=str(selected['showId']),
            start_time=start(detail).isoformat(),hall_name=detail.get('hallName'),
            seats=verified,seat_matched=bool(verified),expected_seat_count=count,
            raw_results={'buyer_text_details':True},final_results={'cityCode':(facts.get('final_results') or {}).get('cityCode')})
        if not result.movie_id or not result.hall_name:raise ValueError('影片或影厅身份缺失')
        decision=tool.conv.apply(Event.BUYER_IMAGE)
        if decision.needs_manual:return respond(tool,'当前进度需要店家核对，暂不继续报价。',False)
        tool.conv.buyer_ticket_count=count
        tool.conv.cinema_confirmed=demand.get('cinema_confirmed')==facts.get('cinema_name')
        out=FlowResult(channel='wanda')
        tool.flow_result=tool.service.flow._after_recognition(tool.conv,result,out)
        if tool.flow_result.ok and tool.conv.quote_id:
            demand.update(demand_id=getattr(tool.conv, 'demand_id', ''),
                          demand_rev=getattr(tool.conv, 'demand_rev', 0))
            demand['pending']=False
            ctx.update(demand_id=demand['demand_id'],demand_rev=demand['demand_rev'])
            ctx['ticket_count']=count
            if verified and ctx.get('verified') and tool.flow_result.messages:
                tool.flow_result.messages[0] = ('本次核价座位：' + '、'.join(s['seatNo'] for s in verified)
                                               + '。\n' + tool.flow_result.messages[0])
        return {'ok':tool.flow_result.ok,'reason':'已根据真实场次和座位执行报价；未执行改价、下单或付款。'}
    except Exception as exc:
        return respond(tool,'排片或座位暂未核实成功，请稍后再试或请店家核对；不会沿用旧报价。',False)
