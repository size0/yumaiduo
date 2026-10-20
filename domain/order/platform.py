"""Platform purchase facts, independent of quote-bound fulfillment orders."""
import json
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL

KEYS = ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')


class PlatformOrderStore:
    def __init__(self, owner):
        self.owner = owner
        with owner.lock:
            owner.connection.execute('''CREATE TABLE IF NOT EXISTS platform_orders (
                tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, platform_order_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(tenant_id, shop_id, platform_order_id))''')
            owner.connection.commit()

    def get(self, tenant, shop, number):
        with self.owner.lock:
            row = self.owner.connection.execute('SELECT payload FROM platform_orders WHERE tenant_id=? AND shop_id=? AND platform_order_id=?',
                                                (tenant, shop, number)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self.owner.lock:
            rows = self.owner.connection.execute('SELECT payload FROM platform_orders').fetchall()
        return [json.loads(row[0]) for row in rows]

    def by_identity(self, identity):
        return [row for row in self.all() if all(row.get(key) == identity.get(key) for key in KEYS)]

    def discover(self, identity, number, event_id='', *, source='UNKNOWN'):
        if not all(isinstance(identity.get(k), str) and identity[k] for k in KEYS) or not number.isdigit():
            raise ValueError('PLATFORM_ORDER_IDENTITY_REQUIRED')
        with self.owner.lock:
            old = self.get(identity['tenant_id'], identity['shop_id'], number)
            if old:
                if any(old[k] != identity[k] for k in KEYS):
                    raise ValueError('PLATFORM_ORDER_IDENTITY_CONFLICT')
                return old
            stamp = datetime.now(timezone.utc).isoformat()
            row = dict(identity, order_id='platform:'+str(uuid5(NAMESPACE_URL, json.dumps([
                identity['tenant_id'], identity['shop_id'], number]))), platform_order_id=number,
                record_kind='PLATFORM_ORDER', platform_status=None, amount_fen=None, paid_at=None,
                sync_state='PENDING', sync_error='', first_seen_at=stamp, checked_at=None,
                identity_source=source,
                last_event_id=event_id, next_sync_at=0)
            self.owner.connection.execute('INSERT INTO platform_orders VALUES (?,?,?,?)',
                (identity['tenant_id'], identity['shop_id'], number, json.dumps(row, ensure_ascii=False)))
            self.owner.connection.commit()
            return row

    def put(self, row, *, expected=None):
        with self.owner.lock:
            old = self.get(row['tenant_id'], row['shop_id'], row['platform_order_id'])
            if not old or any(row[k] != old[k] for k in (*KEYS, 'order_id')):
                raise ValueError('PLATFORM_ORDER_IDENTITY_CONFLICT')
            if expected is not None and old != expected:
                raise ValueError('PLATFORM_RECORD_CHANGED')
            written = self.owner.connection.execute('UPDATE platform_orders SET payload=? WHERE tenant_id=? AND shop_id=? AND platform_order_id=? AND payload=?',
                (json.dumps(row, ensure_ascii=False), row['tenant_id'], row['shop_id'], row['platform_order_id'],
                 json.dumps(old, ensure_ascii=False)))
            if written.rowcount != 1:
                self.owner.connection.rollback()
                raise ValueError('PLATFORM_RECORD_CHANGED')
            self.owner.connection.commit()

