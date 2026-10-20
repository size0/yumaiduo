"""Durable manual-issuance association with the 8801 buyer workbench."""
from contextlib import contextmanager
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import re
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import APIRouter, HTTPException, Request
from . import store, app_credentials, cinema_identity_store

router=APIRouter(prefix="/api/ticket-agent-handoff",tags=["ticket-agent-handoff"])
_worker=None


@contextmanager
def database():
    db=sqlite3.connect(str(store.DATA_DIR / 'ticket_agent_handoff.sqlite3'),timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS bindings(
      wanda_id TEXT PRIMARY KEY, agent_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
      status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS creation_claims(
      agent_id TEXT PRIMARY KEY, wanda_id TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)""")
    try:
        with db:
            yield db
    finally:
        db.close()


def credential():
    key=os.getenv('TICKET_AGENT_APP_KEY','')
    for entry in app_credentials._read():
        if entry.get('app_key')==key and not entry.get('disabled'):
            return key,entry['app_secret']
    raise ValueError('HANDOFF_CREDENTIAL_NOT_CONFIGURED')


async def agent_call(body):
    key,secret=credential();raw=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
    ts=str(int(time.time()));nonce=secrets.token_hex(16)
    signature=hmac.new(secret.encode(),key.encode()+ts.encode()+nonce.encode()+raw,hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
        response=await client.post('http://127.0.0.1:8801/api/v1/ticket-system',content=raw,
            headers={'Content-Type':'application/json','X-App-Key':key,'X-Timestamp':ts,'X-Nonce':nonce,'X-Sign':signature})
    data=response.json()
    if response.status_code!=200 or not data.get('ok'):
        code=str(data.get('error') or 'HANDOFF_UNAVAILABLE')
        raise ValueError(code if re.fullmatch('[A-Z_]{1,80}',code) else 'HANDOFF_UNAVAILABLE')
    return data


def clean(value):
    return re.sub(r'[\s（）()·：:、,，-]','',str(value or '')).lower()


def moment(value):
    text=str(value or '')
    if re.fullmatch(r'[0-9]{10,13}',text):return int(text)/(1000 if len(text)>10 else 1)
    dt=datetime.fromisoformat(text.replace('Z','+00:00'))
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.timestamp()


def validate_source(source, target):
    if not source or not source.get('phone'):raise ValueError('SOURCE_ORDER_NOT_FOUND')
    if source.get('status') in ('cancelled','refunded','failed'):raise ValueError('SOURCE_ORDER_CLOSED')
    identity = target.get('provider_identity') or {}
    cinema_id = str(identity.get('wanda_cinema_id') or '')
    if not cinema_id and identity.get('liangpiao_cinema_id'):
        mapped = cinema_identity_store.lookup(identity.get('liangpiao_city_id'), identity['liangpiao_cinema_id'])
        if not mapped.get('mapped'):raise ValueError('CINEMA_IDENTITY_UNRESOLVED')
        cinema_id = str(mapped['wandaCinemaId'])
    cinema_matches = (str(source.get('cinema_id') or '') == cinema_id if cinema_id
                      else bool(clean(target.get('cinema'))) and clean(source.get('cinema_name')) == clean(target.get('cinema')))
    if not cinema_matches or clean(source.get('movie_name'))!=clean(target.get('movie')):
        raise ValueError('CINEMA_OR_MOVIE_MISMATCH')
    if abs(moment(source.get('show_time'))-moment(target.get('show')))>=60:raise ValueError('SHOWTIME_MISMATCH')
    quantity=source.get('quantity') or len(source.get('seats') or [])
    if int(quantity)!=int(target.get('ticket_count') or 0) or int(quantity)<=0:raise ValueError('TICKET_COUNT_MISMATCH')
    if target.get('seats'):
        actual=source.get('seat_info') or source.get('seat_name') or '、'.join(source.get('seat_names') or [])
        def seat_set(value):
            text=clean(value).replace('列','座')
            return set(re.findall(r'([0-9a-z]+)排([0-9a-z]+)座',text))
        wanted=seat_set('、'.join(str(x) for x in target['seats']))
        if len(wanted)!=len(target['seats']) or wanted!=seat_set(actual):raise ValueError('SEATS_MISMATCH')


def operator(request):
    op=str(request.session.get('operator_id') or '')
    if not op:raise HTTPException(401,'OPERATOR_REQUIRED')
    return op


async def prepare_creation(agent_id, source):
    """Resolve trusted buyer facts before any supplier write."""
    choices = (await agent_call({'action':'list'}))['orders']
    target = next((x for x in choices if x['order_id'] == agent_id), None)
    if not target:raise HTTPException(409, 'BUYER_ORDER_NOT_AVAILABLE')
    if target.get('binding'):raise HTTPException(409, 'BUYER_ORDER_ALREADY_BOUND')
    identity=target.get('provider_identity') or {}
    if not (identity.get('wanda_cinema_id') or identity.get('liangpiao_cinema_id')):
        raise HTTPException(409,'买家订单缺少可核对的影院身份，请先核实影院映射')
    try:validate_source(source, target)
    except (ValueError, TypeError):raise HTTPException(409, '购票信息与买家订单不一致，请核对影院、场次、张数及座位') from None
    return target


def claim_creation(agent_id):
    # A lost supplier response must never allow a second create for this buyer order.
    with database() as db:
        if db.execute('SELECT 1 FROM bindings WHERE agent_id=?', (agent_id,)).fetchone():
            raise HTTPException(409, 'BUYER_ORDER_ALREADY_BOUND')
        try:db.execute('INSERT INTO creation_claims(agent_id,updated) VALUES(?,?)', (agent_id,time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该买家订单已有创建记录，请到订单管理核查原单，不要重复创建') from None


def persist_binding(wanda_id, source, target, op):
    validate_source(source, target)
    binding=dict(binding_id=secrets.token_hex(16),wanda_order_id=wanda_id,phone=source['phone'],quote_id=target['quote_id'])
    payload={'target':target,'binding':binding,'operator':op}
    with database() as db:
        prior=db.execute('SELECT * FROM bindings WHERE wanda_id=? OR agent_id=?',(wanda_id,target['order_id'])).fetchone()
        if prior:
            if prior['wanda_id'] != wanda_id or prior['agent_id'] != target['order_id']:raise ValueError('BINDING_CONFLICT')
            return prior['status']
        db.execute('INSERT INTO bindings(wanda_id,agent_id,payload,status,updated) VALUES(?,?,?,?,?)',
            (wanda_id,target['order_id'],json.dumps(payload,ensure_ascii=False),'BIND_PENDING',time.time()))
        db.execute('UPDATE creation_claims SET wanda_id=?,updated=? WHERE agent_id=?',(wanda_id,time.time(),target['order_id']))
    return 'BIND_PENDING'


@router.get('/orders')
async def pending_orders(request:Request):
    operator(request)
    try:return await agent_call({'action':'list'})
    except Exception:raise HTTPException(503,'买家订单暂时无法查询，请稍后刷新') from None


@router.get('/binding/{wanda_id}')
async def binding_status(wanda_id:str,request:Request):
    operator(request)
    with database() as db:row=db.execute('SELECT * FROM bindings WHERE wanda_id=?',(wanda_id,)).fetchone()
    if not row:return {'binding':None}
    payload=json.loads(row['payload']);delivery=None
    if row['status']=='ACCEPTED':
        try:delivery=await agent_call({'action':'status','order_id':row['agent_id']})
        except Exception:pass
    return {'binding':{'order_id':row['agent_id'],'wanda_order_id':wanda_id,'status':row['status'],'error':row['error'],'target':payload['target'],'delivery':delivery}}


@router.get('/workbench/{agent_id}')
async def workbench_order(agent_id:str, request:Request):
    operator(request)
    target=next((o for o in (await agent_call({'action':'list'}))['orders'] if o['order_id']==agent_id),None)
    if not target:raise HTTPException(409,'该买家订单已不在待出票列表，请刷新')
    with database() as db:
        row=db.execute('SELECT * FROM bindings WHERE agent_id=?',(agent_id,)).fetchone()
        claim=db.execute('SELECT * FROM creation_claims WHERE agent_id=?',(agent_id,)).fetchone()
    wanda_id=row['wanda_id'] if row else (claim['wanda_id'] if claim else '')
    if not wanda_id and (claim or target.get('binding')):
        raise HTTPException(409,'该订单已有创建或关联记录，请核查原单，不要重复出票')
    source=store.get_order_by_id(wanda_id) if wanda_id else None
    if wanda_id and not source:raise HTTPException(409,'关联的万达订单暂时无法读取，请核查原单')
    return {'target':target,'order':source}


@router.post('/bind')
async def bind_order(body:dict,request:Request):
    op=operator(request)
    wanda_id=str(body.get('wanda_order_id') or '');agent_id=str(body.get('order_id') or '')
    try:
        source=store.get_order_by_id(wanda_id)
        with database() as db:prior=db.execute('SELECT * FROM bindings WHERE wanda_id=?',(wanda_id,)).fetchone()
        if prior:
            if prior['agent_id']!=agent_id:raise ValueError('BINDING_CONFLICT')
            return {'ok':True,'status':prior['status']}
        choices=(await agent_call({'action':'list'}))['orders']
        target=next((x for x in choices if x['order_id']==agent_id),None)
        if not target:raise ValueError('BUYER_ORDER_NOT_AVAILABLE')
        return {'ok':True,'status':persist_binding(wanda_id,source,target,op)}
    except (ValueError,TypeError,sqlite3.IntegrityError) as exc:
        message=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z_]{1,80}',str(exc)) else 'BINDING_CONFLICT'
        raise HTTPException(409,message) from None


def command(payload,action):
    t=payload['target']
    return dict(action=action,order_id=t['order_id'],binding=payload['binding'],
                **{k:t[k] for k in ('tenant_id','shop_id','buyer_id','chat_id','quote_id')})


async def process(row):
    payload=json.loads(row['payload']);source=store.get_order_by_id(row['wanda_id'])
    validate_source(source,payload['target'])
    if source['phone']!=payload['binding']['phone']:raise ValueError('SOURCE_ACCOUNT_CHANGED')
    if row['status']=='BIND_PENDING':
        await agent_call(command(payload,'bind'))
        with database() as db:db.execute("UPDATE bindings SET status='WAITING_TICKET',error='',updated=? WHERE wanda_id=?",(time.time(),row['wanda_id']))
        return
    image_data=payload.get('image_data')
    if not image_data:
        # Read only: wait for supplier pickup proof, then reuse the ticket image renderer.
        from .routes.order import _ticket_code_uncached, render_order_ticket_image
        result=await _ticket_code_uncached(row['wanda_id'],payload['binding']['phone'],False)
        pickup=result.get('pickup_codes') or []
        if not any(isinstance(x,dict) and (x.get('code') or x.get('qr')) for x in pickup):return
        source=dict(store.get_order_by_id(row['wanda_id']))
        unique=[];seen=set()
        for item in pickup:
            identity=str(item.get('code') or item.get('qr') or '') if isinstance(item,dict) else ''
            if identity and identity not in seen:
                seen.add(identity);unique.append(item)
        if len(unique)>20:raise ValueError('TOO_MANY_QR_IMAGES')
        images=[]
        for item in unique:
            source['_ticket_cache']=dict(result,pickup_codes=[item])
            images.append(await render_order_ticket_image(source,payload['binding']['phone']))
        png=images[0]
        if len(images)>1:
            from PIL import Image
            import io
            frames=[Image.open(io.BytesIO(data)).convert('RGB') for data in images]
            width=max(frame.width for frame in frames);height=sum(frame.height for frame in frames)
            if width>20000 or height>20000:raise ValueError('QR_IMAGE_TOO_LARGE')
            joined=Image.new('RGB',(width,height),'white');offset=0
            for frame in frames:
                joined.paste(frame,(0,offset));offset+=frame.height
            output=io.BytesIO();joined.save(output,format='PNG');png=output.getvalue()
        if not png or len(png)>5*1024*1024:raise ValueError('INVALID_QR_IMAGE')
        image_data=base64.b64encode(png).decode()
        payload['image_data']=image_data
        payload.pop('ticket_codes',None)
        with database() as db:db.execute("UPDATE bindings SET payload=?,status='RETURN_PENDING',updated=? WHERE wanda_id=?",(json.dumps(payload,ensure_ascii=False),time.time(),row['wanda_id']))
    await agent_call(dict(command(payload,'deliver'),image_data=image_data))
    with database() as db:db.execute("UPDATE bindings SET status='ACCEPTED',error='',updated=? WHERE wanda_id=?",(time.time(),row['wanda_id']))


async def run_worker():
    # One process holds this lock throughout its lifetime, including hot restarts.
    import fcntl
    lock=open(store.DATA_DIR/'ticket_agent_handoff.lock','a')
    try:
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(1)
        while True:
            with database() as db:jobs=db.execute("SELECT * FROM bindings WHERE status IN ('BIND_PENDING','WAITING_TICKET','RETURN_PENDING') ORDER BY updated LIMIT 20").fetchall()
            for row in jobs:
                try:await process(row)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    code=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[A-Z_]{1,80}',str(exc)) else 'QUERY_OR_RETURN_UNCONFIRMED'
                    conflicts=('MISMATCH','CONFLICT','CHANGED','CLOSED','OUTSIDE','NOT_DELIVERABLE','ALREADY_BOUND')
                    status='ATTENTION' if any(x in code for x in conflicts) else row['status']
                    with database() as db:db.execute('UPDATE bindings SET status=?,error=?,updated=? WHERE wanda_id=?',(status,code,time.time(),row['wanda_id']))
                finally:
                    with database() as db:db.execute('UPDATE bindings SET updated=? WHERE wanda_id=?',(time.time(),row['wanda_id']))
            await asyncio.sleep(10)
    finally:lock.close()


def start():
    global _worker
    if os.getenv('TICKET_AGENT_HANDOFF_ENABLED')=='true' and (_worker is None or _worker.done()):
        _worker=asyncio.create_task(run_worker())


async def stop():
    if _worker:
        _worker.cancel()
        try:await _worker
        except asyncio.CancelledError:pass
