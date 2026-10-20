"""Seller statements are conversation evidence, never payment authorization."""
from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import json
import re
import sqlite3

GUIDANCE='店家已有新的回复，之前的自动报价已暂停；请结合店家最新回复核对本次价格和处理进度，暂不自动改价或付款。'


def annotate(messages, identity, db_path=None):
    rows=deepcopy(messages)
    try:
        if db_path is None:
            from . import store,plugin_api
            db_path=plugin_api._CACHE.get('path') or store.DATA/'plugin-queue.sqlite3'
        from pathlib import Path
        path=Path(db_path)
        if not path.is_file():raise ValueError('receipts unavailable')
        with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
            records=db.execute('SELECT payload FROM yumaiduo_commands WHERE session_id=? AND status=? ORDER BY rowid DESC LIMIT 501',
                               (identity['chat_id'],'SENT')).fetchall()
        auto=set()
        for (raw,) in records:
            command=json.loads(raw)
            if all(str(command.get(k) or '')==v for k,v in identity.items()) and command.get('sent_message_id'):
                auto.add(command['sent_message_id'])
        complete=len(records)<501
        for row in rows:
            row['origin']=('PLUGIN_AUTOMATED' if row.get('message_id') in auto else
                           'EXTERNAL_SELLER' if complete and row.get('speaker')=='seller' else 'UNKNOWN')
    except (OSError,ValueError,TypeError,sqlite3.Error):
        for row in rows:row['origin']='UNKNOWN'
    return rows


def collect(tools):
    """Pause stale automatic quotes on verified external seller intervention."""
    empty={'active':False,'messages':[]}
    conv=tools.conv;version=dict(demand_id=getattr(conv,'demand_id',''),demand_rev=getattr(conv,'demand_rev',0))
    saved=tools.session.image_context.get('seller_review') or {}
    rows=tools.service.quote_store.by_identity(tools.identity) if tools.service.quote_store else []
    q=rows[0] if rows else None
    if not q or any(getattr(q,k)!=v for k,v in version.items()):
        tools.seller_context=empty
        return empty
    same_saved=(saved.get('quote_id')==q.quote_id and all(saved.get(k)==v for k,v in {**tools.identity,**version}.items()))
    dialogue=getattr(tools,'official_dialogue',{})
    messages=[]
    if dialogue.get('status')=='AVAILABLE' and dialogue.get('identity')==tools.identity:
        for row in dialogue.get('messages',[]):
            try:
                recent=datetime.fromisoformat(row['at'])>q.created_at
            except (KeyError,TypeError,ValueError):continue
            if (recent and row.get('speaker')=='seller' and row.get('origin')=='EXTERNAL_SELLER'
                    and row.get('message_id') and row.get('text')):
                messages.append(deepcopy(row))
    if messages:
        saved=dict(tools.identity,**version,quote_id=q.quote_id,active=True,messages=messages[-20:],guidance=GUIDANCE,
            paused_demand=dict(cinema=q.cinema,movie=q.movie,start_time=q.show,seats=q.selected_seats,ticket_count=q.ticket_count))
        tools.session.image_context['seller_review']=saved
    elif same_saved and dialogue.get('status')=='AVAILABLE' and dialogue.get('identity')==tools.identity:
        # A fresh official snapshot removes recalled/out-of-window references.
        # Keep the conservative pause; removed text cannot support a price.
        saved=deepcopy(saved);saved['messages']=[]
        tools.session.image_context['seller_review']=saved
    elif not same_saved:
        saved=empty
    if saved.get('active'):
        if q.status=='READY':tools.service.quote_store.update_status(q.quote_id,'SUPERSEDED')
        conv.quote=None;conv.quote_kind=''
    tools.seller_context=deepcopy(saved)
    return tools.seller_context


def referenced_messages(tools):
    context=getattr(tools,'seller_context',{})
    if not context.get('active'):return []
    selected=[]
    for claim in getattr(tools,'reply_metadata',{}).get('claims',[]):
        if not isinstance(claim,dict) or claim.get('kind')!='seller_message':continue
        if any(claim.get(k)!=context.get(k) for k in ('demand_id','demand_rev')):continue
        for row in context.get('messages',[]):
            try:
                hours=max(1,min(24,int(tools.cfg.assistant.memory_hours)))
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(row['at'])).total_seconds()
            except (KeyError,TypeError,ValueError):continue
            if 0<=age<=hours*3600 and row['message_id']==claim.get('message_id'):
                selected.append(row)
    return selected


def amounts(tools,text):
    if not re.search('店家|人工|店铺',text):return set()
    sources={fact for row in referenced_messages(tools) for fact in price_literals(row['text'])}
    requested=price_literals(text)
    # Check every occurrence before reducing to values. The same number may
    # have an evidenced unit price and an unevidenced total in one sentence.
    if not all((value,kind) in sources or (kind=='unspecified' and any(v==value for v,_ in sources))
               for value,kind in requested):return set()
    return {value for value,_ in requested}


def price_reference_valid(tools,text):
    return all(value in amounts(tools,text) for value,_ in price_literals(text))


def price_literals(text):
    """Require currency or an explicit per-ticket unit; seat/time digits aren't money."""
    number=r'\d{1,5}(?:\.\d{1,2})?'
    pattern=rf'[¥￥]\s*({number})|(?<![\d.])({number})\s*(?:元|块|[¥￥])|(?<![\d.])({number})\s*(?:一|每|/)\s*张'
    facts=[]
    for match in re.finditer(pattern,text):
        value=next(v for v in match.groups() if v)
        before=text[max(0,match.start()-8):match.start()]
        after=text[match.end():match.end()+6]
        kind=('unit' if match.group(3) or re.match(r'\s*(?:/|一|每)\s*张',after)
              or re.search(r'(?:单价|每张)\s*[:：]?\s*$',before) else
              'total' if re.search(r'(?:合计|总价|总计|一共|共)\s*[:：]?\s*$',before) else 'unspecified')
        facts.append((str(Decimal(value)),kind))
    return facts
