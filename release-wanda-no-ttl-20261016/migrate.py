"""One-time migration: remove Wanda TTL; restore only latest future-show time-expired quote."""
import json,sqlite3
from datetime import datetime,timezone,timedelta

def migrate(connection):
    now=datetime.now(timezone.utc);changed=[]
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        rows=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM quote_records')]
        latest={}
        for row in rows:
            key=tuple(row.get(k) for k in ('tenant_id','shop_id','buyer_id','chat_id'))
            if key not in latest or row.get('created_at','')>latest[key].get('created_at',''):
                latest[key]=row
        for row in rows:
            if row.get('provider')!='WANDA':continue
            old_exp=row.get('expires_at');restored=False
            key=tuple(row.get(k) for k in ('tenant_id','shop_id','buyer_id','chat_id'))
            if row.get('status')=='EXPIRED' and latest.get(key) is row and old_exp:
                try:
                    expiry=datetime.fromisoformat(old_exp)
                    if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=timezone.utc)
                    show=datetime.fromisoformat(row.get('show') or '')
                    if show.tzinfo is None:show=show.replace(tzinfo=timezone(timedelta(hours=8)))
                    restored=expiry<=now<show
                except (ValueError,TypeError):pass
            if old_exp is None and not restored:continue
            row['expires_at']=None
            if restored:row['status']='READY'
            connection.execute('UPDATE quote_records SET payload=? WHERE quote_id=?',
                (json.dumps(row,ensure_ascii=False),row['quote_id']))
            changed.append({'quote_id':row['quote_id'],'restored':restored})
    return changed
