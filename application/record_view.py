"""Read-only operating record enrichment and China-time filtering."""
import json
import time
from datetime import datetime, timezone, timedelta
from threading import RLock
from concurrent.futures import ThreadPoolExecutor

_CACHE = {}
_LOCK = RLock()
_POOL = ThreadPoolExecutor(max_workers=4)
_PENDING = {}
CHINA = timezone(timedelta(hours=8))

def buyer_names(rows, *, wait=False):
    from integrations.yumaiduo.shop_client import PluginShopsClient, resolve_plugin_secret
    identities = set((r['tenant_id'], r['shop_id'], r['buyer_id']) for r in rows)
    def read(key):
        with _LOCK:
            cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return key, cached[1]
        name = cached[1] if cached else ''
        if all(key):
            try:
                client = PluginShopsClient(secret=resolve_plugin_secret(), timeout=3)
                status, raw = client._post('/internal/buyers/read', dict(zip(('tenant_id','shop_id','buyer_id'),key)))
                data = json.loads(raw)
                if status == 200 and data.get('buyer_id') == key[2] and data.get('shop_id') == key[1]:
                    name = str(data.get('buyer_name') or name)
            except (ValueError, OSError, RuntimeError):
                pass
        with _LOCK:
            _CACHE[key] = (time.monotonic(), name)
        return key, name
    if wait:
        return dict(_POOL.map(read, identities))
    with _LOCK:
        for key, future in list(_PENDING.items()):
            if future.done():
                try: future.result()
                except Exception: _CACHE[key] = (time.monotonic(), '')
                del _PENDING[key]
        for key in identities:
            cached = _CACHE.get(key)
            if key not in _PENDING and (not cached or time.monotonic()-cached[0]>=300) and len(_PENDING)<32:
                _PENDING[key] = _POOL.submit(read,key)
        return {key: _CACHE.get(key,(0,''))[1] for key in identities}

def enrich(out):
    names = buyer_names(out['quotes'] + out['orders'])
    out['profiles_pending'] = bool(_PENDING)
    quotes = {r['quote_id']:r for r in out['quotes']}
    from .plugin_api import queue
    q = queue()
    with q.lock:
        commands = [json.loads(r[0]) for r in q.connection.execute(
            "SELECT payload FROM yumaiduo_commands WHERE type='CHANGE_ORDER_PRICE' ORDER BY created_at DESC")]
    for row in out['quotes'] + out['orders']:
        row['buyer_name'] = names.get((row['tenant_id'],row['shop_id'],row['buyer_id']), '')
    for row in out['orders']:
        quote = quotes.get(row['quote_id'])
        row['quote_bound'] = bool(quote)
        row['margin_yuan'] = quote['margin_total_yuan'] if quote else None
        row['quote_total_yuan'] = quote['total_sell_yuan'] if quote else None
        platform = row['platform_order_id'] or (row['provider_order_id'] if row['provider']=='WANDA' else '')
        row['platform_order_id'] = platform
        row['supplier_order_id'] = row['provider_order_id'] if row['provider']=='LIANGPIAO' else ''
        match = next((c for c in commands if c.get('order_id')==platform and all(str(c.get(k,''))==str(row[k]) for k in ('tenant_id','shop_id','buyer_id'))), None)
        row['reprice_status'] = match.get('status','') if match else ''
        refusal_labels = {'CANNOT_MODIFY_FEE': '平台不允许修改这笔订单价格，需店家核对',
                          'ORDER_ALREADY_PAID': '平台提示订单已付款，不能改价',
                          'ORDER_CLOSED': '平台提示订单已关闭，不能改价'}
        row['reprice_failure_reason'] = (refusal_labels.get(match.get('failure_reason'), '')
                                         if match and match.get('status') == 'FAILED' else '')
        if match and match.get('status') == 'SUCCEEDED' and (int(match.get('target_amount_fen') or 0) != round(row['amount_yuan']*100)
                      or (match.get('replacement_quote_id') and match['replacement_quote_id'] != row['quote_id'])):
            row['reprice_status'] = 'STALE'
        row['reprice_target_yuan'] = (match.get('target_amount_fen') or 0)/100 if match else None
        if row.get('manual_settlement'):
            row['historical_reprice_status'] = row['reprice_status']
            row['historical_reprice_target_yuan'] = row['reprice_target_yuan']
            row['reprice_status'] = 'MANUAL_CONFIRMED'
            row['reprice_target_yuan'] = row['manual_settlement']['amount_fen'] / 100

def day(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=CHINA)
        return dt.astimezone(CHINA).date().isoformat()
    except (TypeError,ValueError):
        return ''

def select(rows, params, kind):
    start,end,query = (params.get(k,'').strip() for k in ('start','end','q'))
    if start and end and start>end: return []
    state=params.get('status','all')
    today=datetime.now(CHINA).date().isoformat()
    result=[]
    fields=('buyer_name','buyer_id','shop_id','quote_id','order_id','platform_order_id','supplier_order_id','cinema','movie','provider')
    shop = params.get('shop_id', '').strip()
    tenant = params.get('tenant_id', '').strip()
    for r in rows:
        if shop and (str(r.get('shop_id', '')) != shop or str(r.get('tenant_id', '')) != tenant):
            continue
        date=day(r.get('created_at'))
        if (start and date<start) or (end and date>end): continue
        if query and query.casefold() not in ' '.join(str(r.get(k) or '') for k in fields).casefold(): continue
        if state!='all':
            if kind=='orders' and r.get('bucket')!=state: continue
            if kind=='quotes':
                if state=='TODAY' and date!=today: continue
                if state=='LOSS' and not (r.get('is_loss') and r.get('status')=='READY'): continue
                if state not in ('TODAY','LOSS') and r.get('status')!=state: continue
        result.append(r)
    return result
