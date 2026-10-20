"""Platform shipment is not pending issuance or proof of QR delivery."""
import unittest

from application.ledger import _order_row
from domain.order import Order


class OrderDisplayTests(unittest.TestCase):
    def order(self, **kwargs):
        return Order(order_id='o', quote_id='q', idempotency_key='i', provider='WANDA',
                     tenant_id='t', shop_id='s', buyer_id='b', chat_id='c',
                     provider_order_id='platform', state='PAID', payment_state='CONFIRMED',
                     amount_fen=6600, paid_fen=6600, **kwargs)

    def test_shipped_without_qr_is_attention_not_pending_issuance(self):
        order = self.order(shipped=True)
        before = order.to_dict()
        row = _order_row(order)
        self.assertEqual(row['bucket'], 'attention')
        self.assertIn('已发货', row['state_label'])
        self.assertIn('待核对', row['state_label'])
        self.assertTrue(row['needs_attention'])
        self.assertEqual(order.to_dict(), before)
        self.assertEqual(row['ticket_codes'], [])
        self.assertFalse(row['can_deliver'])

    def test_completed_platform_order_has_explicit_label_without_forging_delivery(self):
        order = self.order(shipped=True, payload={'platform_delivery_sync': {'order_id': 'platform', 'status': 4}})
        row = _order_row(order)
        self.assertIn('平台已完成', row['state_label'])
        self.assertEqual(row['bucket'], 'attention')
        self.assertEqual(row['state'], 'PAID')
        self.assertFalse(row['delivered_at'])

    def test_unshipped_paid_order_stays_pending(self):
        row = _order_row(self.order())
        self.assertEqual(row['bucket'], 'awaiting_ticket')
        self.assertEqual(row['state_label'], '待出票')

    def test_delivered_record_is_not_demoted(self):
        order = self.order(shipped=True)
        order.state = 'DELIVERED'
        row = _order_row(order)
        self.assertEqual(row['bucket'], 'delivered')

    def test_unrelated_platform_reference_does_not_claim_completion(self):
        row = _order_row(self.order(shipped=True, payload={'platform_delivery_sync': {'order_id': 'other', 'status': 4}}))
        self.assertNotIn('平台已完成', row['state_label'])
