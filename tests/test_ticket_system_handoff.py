import base64
from application import wanda_qr
from integrations.yumaiduo import shop_client
QR=base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode()
IMG={"imageUrl":"https://img.alicdn.com/qr.png","width":100,"height":100}
import unittest, tempfile, os, json, hashlib, hmac, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from domain.order import Order,OrderStore
from application import ticket_system_handoff as h

class HandoffTests(unittest.TestCase):
 def setUp(self):
  self.s=OrderStore();self.addCleanup(self.s.close)
  self.ident=dict(tenant_id='107',shop_id='s',buyer_id='b',chat_id='c')
  self.o=Order(order_id='local',quote_id='q',idempotency_key='i',provider='WANDA',provider_order_id='platform',payment_state='CONFIRMED',amount_fen=100,paid_fen=100,state='AWAITING_TICKET',**self.ident)
  self.s.create(self.o)
  self.binding=dict(binding_id='binding',wanda_order_id='wanda1',phone='account',quote_id='q')
  self.body=dict(order_id='local',quote_id='q',binding=self.binding,**self.ident)
  self.env=patch.dict(os.environ,{'TICKET_HANDOFF_ENABLED':'true','TICKET_HANDOFF_TENANT_ID':'107','TICKET_HANDOFF_SHOP_IDS':'s'});self.env.start();self.addCleanup(self.env.stop)
  self.store=patch.object(h.ledger,'stores',return_value=SimpleNamespace(orders=self.s));self.store.start();self.addCleanup(self.store.stop)
  self.platform=patch.object(h,'platform_order',return_value={'orderStatus':2,'payment':'100'});self.platform.start();self.addCleanup(self.platform.stop)
  for p in [patch.object(wanda_qr,'platform_order',return_value={'orderStatus':2,'payment':'100'}),patch.object(shop_client,'upload_ticket_image',return_value={'ok':True,'image':IMG})]:
   p.start();self.addCleanup(p.stop)
 def call(self,action,**kw):return h.handle(dict(self.body,action=action,**kw))
 def test_bind_deliver_replay_preserves_platform_id(self):
  self.call('bind');self.call('deliver',image_data=QR)
  self.assertTrue(self.call('deliver',image_data=QR)['duplicate'])
  o=self.s.get('local');self.assertEqual(o.provider_order_id,'platform');self.assertEqual(o.state,'TICKETED')
  with self.assertRaisesRegex(ValueError,'TICKET_CONFLICT'):self.call('deliver',image_data=base64.b64encode(b'\x89PNG\r\n\x1a\nother').decode())
 def test_unpaid_rejected(self):
  o=self.s.get('local');o.payment_state='UNPAID';self.s.put(o)
  with self.assertRaisesRegex(ValueError,'ORDER_NOT_DELIVERABLE'):self.call('bind')
 def test_wrong_buyer_rejected(self):
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.call('bind',buyer_id='other')
 def test_changed_quote_rejected(self):
  self.call('bind');o=self.s.get('local');o.quote_id='new';self.s.put(o)
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.call('deliver',image_data=QR)
 def test_same_wanda_order_cannot_bind_two_buyers(self):
  self.call('bind');o=Order(order_id='two',quote_id='q2',idempotency_key='i2',provider='WANDA',provider_order_id='p2',payment_state='CONFIRMED',amount_fen=100,paid_fen=100,state='AWAITING_TICKET',**self.ident);self.s.create(o)
  with self.assertRaisesRegex(ValueError,'ALREADY_BOUND'):self.call('bind',order_id='two',quote_id='q2',binding=dict(self.binding,quote_id='q2'))
 def test_platform_closed_rejected(self):
  with patch.object(h,'platform_order',return_value={'orderStatus':5,'payment':'100'}):
   with self.assertRaisesRegex(ValueError,'PAYMENT'):self.call('bind')
 def test_signature_nonce_replay_and_bad_signature(self):
  with tempfile.TemporaryDirectory() as d,patch.object(h.ledger,'orders_file',return_value=Path(d)/'orders.db'),patch.dict(os.environ,{'TICKET_APP_KEY':'key','TICKET_APP_SECRET':'secret'}):
   raw=b'{}';ts=str(int(time.time()));nonce='n'*32
   sign=hmac.new(b'secret',b'key'+ts.encode()+nonce.encode()+raw,hashlib.sha256).hexdigest()
   headers={'X-App-Key':'key','X-Timestamp':ts,'X-Nonce':nonce,'X-Sign':sign}
   h.authenticate(headers,raw)
   with self.assertRaisesRegex(ValueError,'NONCE_REUSED'):h.authenticate(headers,raw)
   with self.assertRaisesRegex(ValueError,'AUTH_INVALID'):h.authenticate(dict(headers,**{'X-Nonce':'a'*32}),raw)
