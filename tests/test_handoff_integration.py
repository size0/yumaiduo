import base64
from application import wanda_qr
from integrations.yumaiduo import shop_client
QR=base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode()
IMG={"imageUrl":"https://img.alicdn.com/qr.png","width":100,"height":100}
import os,sys,tempfile,unittest,asyncio,json
from pathlib import Path
from types import SimpleNamespace,ModuleType
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures' / 'ticket_backend'))
with tempfile.TemporaryDirectory(prefix="ticket-backend-import-") as import_data:
 with patch.dict(os.environ, {"TICKET_DATA_DIR": import_data}):
  from app import ticket_agent_handoff as back
from application import ticket_system_handoff as receiver
from domain.order import Order,OrderStore

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.s=OrderStore();self.addCleanup(self.s.close)
  self.o=Order(order_id='local',quote_id='q',idempotency_key='i',provider='WANDA',provider_order_id='platform',payment_state='CONFIRMED',amount_fen=100,paid_fen=100,state='AWAITING_TICKET',tenant_id='107',shop_id='s',buyer_id='b',chat_id='c',cinema='万达影城',movie='电影',show='2099-10-16 20:15',ticket_count=2)
  self.s.create(self.o)
  self.source=dict(id='wanda1',phone='account',cinema_name='万达影城',movie_name='电影',show_time='2099-10-16 20:15',quantity=2,status='created')
  patches=[patch.dict(os.environ,{'TICKET_HANDOFF_ENABLED':'true','TICKET_HANDOFF_TENANT_ID':'107','TICKET_HANDOFF_SHOP_IDS':'s'}),patch.object(receiver.ledger,'stores',return_value=SimpleNamespace(orders=self.s)),patch.object(receiver,'platform_order',return_value={'orderStatus':2,'payment':'100'}),patch.object(back.store,'DATA_DIR',Path(self.temp.name)),patch.object(back.store,'get_order_by_id',return_value=self.source)]
  patches += [patch.object(wanda_qr,'platform_order',return_value={'orderStatus':2,'payment':'100'}),patch.object(shop_client,'upload_ticket_image',return_value={'ok':True,'image':IMG})]
  for p in patches:p.start();self.addCleanup(p.stop)
  async def call(body):return receiver.handle(body)
  p=patch.object(back,'agent_call',side_effect=call);p.start();self.addCleanup(p.stop)
  self.request=SimpleNamespace(session={'operator_id':'tester'})
 def row(self):
  with back.database() as db:return db.execute('SELECT * FROM bindings').fetchone()
 async def test_full_bind_and_code_return_with_unknown_response_replay(self):
  await back.bind_order({'wanda_order_id':'wanda1','order_id':'local'},self.request)
  await back.process(self.row());self.assertEqual(self.row()['status'],'WAITING_TICKET')
  module=ModuleType('app.routes.order')
  async def codes(*args):return {'pickup_codes':[{'code':'CODE'}]}
  module._ticket_code_uncached=codes
  async def render(*args):return base64.b64decode(QR)
  module.render_order_ticket_image=render
  async def uncertain(body):
   result=receiver.handle(body)
   if body['action']=='deliver':raise TimeoutError('response lost')
   return result
  with patch.dict(sys.modules,{'app.routes.order':module}),patch.object(back,'agent_call',side_effect=uncertain):
   with self.assertRaises(TimeoutError):await back.process(self.row())
  self.assertEqual(self.row()['status'],'RETURN_PENDING')
  self.assertEqual(self.s.get('local').ticket_codes,[])
  self.assertEqual(self.s.get('local').payload['wanda_ticket_image'],IMG)
  await back.process(self.row())
  self.assertEqual(self.row()['status'],'ACCEPTED')
  self.assertEqual(self.s.get('local').provider_order_id,'platform')
  self.assertFalse(self.s.get('local').shipped)
 async def test_mismatched_show_cannot_bind(self):
  self.source['show_time']='2099-10-17 20:15'
  from fastapi import HTTPException
  with self.assertRaises(HTTPException):await back.bind_order({'wanda_order_id':'wanda1','order_id':'local'},self.request)
  self.assertIsNone(self.row())
 async def test_row_one_does_not_match_row_eleven(self):
  target=receiver.projection(self.o);target['seats']=['1排1座']
  self.source['seat_info']='11排1座'
  with self.assertRaisesRegex(ValueError,'SEATS_MISMATCH'):back.validate_source(self.source,target)
 async def test_different_source_account_blocks_return(self):
  await back.bind_order({'wanda_order_id':'wanda1','order_id':'local'},self.request)
  self.source['phone']='changed'
  with self.assertRaisesRegex(ValueError,'ACCOUNT_CHANGED'):await back.process(self.row())

 async def test_worker_waits_for_lock_and_rotates_more_than_twenty(self):
  from unittest.mock import Mock, AsyncMock
  with back.database() as db:
   for i in range(21):db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?)',(str(i),str(i),'{}','WAITING_TICKET','',i))
  fake=ModuleType('fcntl');fake.LOCK_EX=1;fake.LOCK_NB=2
  fake.flock=Mock(side_effect=[BlockingIOError(),None])
  sleeps=[];seen=[]
  async def sleep(n):
   sleeps.append(n)
   if len(sleeps)>=3:raise asyncio.CancelledError()
  async def process(row):seen.append(row['wanda_id'])
  with patch.dict(sys.modules,{'fcntl':fake}),patch.object(back,'process',side_effect=process),patch.object(back.asyncio,'sleep',side_effect=sleep):
   with self.assertRaises(asyncio.CancelledError):await back.run_worker()
  self.assertEqual(fake.flock.call_count,2)
  self.assertIn('20',seen)

 async def test_multiple_qr_images_returned_together(self):
  import io,base64
  from PIL import Image
  await back.bind_order({'wanda_order_id':'wanda1','order_id':'local'},self.request)
  await back.process(self.row())
  module=ModuleType('app.routes.order');seen=[]
  async def codes(*args):return {'pickup_codes':[{'code':'A'},{'code':'B'}]}
  async def render(source,*args):
   seen.append(source['_ticket_cache']['pickup_codes'][0]['code'])
   out=io.BytesIO();Image.new('RGB',(10,10),'black').save(out,format='PNG');return out.getvalue()
  module._ticket_code_uncached=codes;module.render_order_ticket_image=render
  with patch.dict(sys.modules,{'app.routes.order':module}):await back.process(self.row())
  self.assertEqual(seen,['A','B']);self.assertEqual(self.row()['status'],'ACCEPTED')
  payload=json.loads(self.row()['payload'])
  self.assertEqual(Image.open(io.BytesIO(base64.b64decode(payload['image_data']))).size,(10,20))
