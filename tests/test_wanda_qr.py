import unittest
from unittest.mock import patch
from tests.test_ticket_system_handoff import HandoffTests,QR,IMG
from application import wanda_qr,plugin_authorize
from integrations.yumaiduo.commands import Command

class ManualQRTests(HandoffTests):
 def test_manual_optional_fields_and_actual_receipts(self):
  r=wanda_qr.receive('local',QR,tenant='107',shops={'s'})
  self.assertTrue(r['ok']);o=self.s.get('local')
  self.assertEqual(o.provider_order_id,'platform');self.assertFalse(o.shipped)
  self.assertEqual(o.ticket_codes,[]);self.assertEqual(o.payload['wanda_qr_receipt']['operator'],'')
  self.assertTrue(o.can_deliver()[0])
  command=Command(command_id='cmd',type='TICKET_DELIVERY_NOTICE',event_id='platform',image=IMG,**self.ident,
                  extra={'local_order_id':'local','delivery_provider':'WANDA'})
  command=Command.from_dict(command.to_dict())
  with patch.object(plugin_authorize,'_load',return_value=(command,None)),patch('application.store.shop_send_enabled',return_value=True):
   grant=plugin_authorize.authorize_ticket_delivery('cmd',detail={'orderId':'platform','accountUnb':'s','orderStatus':2,'payment':'100'},session={'peerUnb':'b'})
   self.assertTrue(grant['allowed']);self.assertEqual(grant['image'],IMG);self.assertEqual(grant['text'],'')
   bad=plugin_authorize.authorize_ticket_delivery('cmd',detail={'orderId':'platform','accountUnb':'s','orderStatus':2,'payment':'99'},session={'peerUnb':'b'})
   self.assertFalse(bad['allowed'])
 def test_invalid_image_and_unpaid_no_record(self):
  with self.assertRaisesRegex(ValueError,'INVALID_QR_IMAGE'):wanda_qr.receive('local','12345',tenant='107',shops={'s'})
  o=self.s.get('local');o.payment_state='UNPAID';self.s.put(o)
  with self.assertRaisesRegex(ValueError,'NOT_DELIVERABLE'):wanda_qr.receive('local',QR,tenant='107',shops={'s'})
  self.assertNotIn('wanda_ticket_image',self.s.get('local').payload)
 def test_upstream_id_does_not_replace_platform_id(self):
  wanda_qr.receive('local',QR,tenant='107',shops={'s'},upstream_id='supplier')
  self.assertEqual(self.s.get('local').provider_order_id,'platform')
  self.assertEqual(self.s.get('local').payload['wanda_qr_receipt']['upstream_order_id'],'supplier')
