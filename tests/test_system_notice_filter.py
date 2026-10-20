"""Platform notifications cannot start an AI reply loop."""
import unittest
from unittest.mock import patch

from application import plugin_runtime


class NoticeFilterTests(unittest.TestCase):
    def event(self, message_type, content='营销小助手答复了你'):
        return {'event': 'im.message.received', 'tenantId': '107', 'eventId': 'test',
                'payload': {'chatId': 'c', 'accountUnb': '2313315754',
                            'peerUnb': '2222077351043', 'messageType': message_type,
                            'content': content}}

    def test_system_types_never_enter_ai_even_with_unseen_wording(self):
        with patch.object(plugin_runtime, '_from_buyer_message') as buyer, patch.object(
                plugin_runtime, '_from_order_card') as card:
            for kind in (14, '14', 28, '28', 999, '999'):
                for text in ('营销小助手答复了你', '平台新的自动通知内容', ''):
                    with self.subTest(kind=kind, text=text):
                        self.assertEqual(plugin_runtime.events_to_commands(self.event(kind, text)), [])
            buyer.assert_not_called()
            card.assert_not_called()

    def test_real_buyer_words_and_images_are_not_filtered_by_text(self):
        with patch.object(plugin_runtime, '_from_buyer_message', return_value=[]) as buyer:
            for kind in (1, 2, None):
                plugin_runtime.events_to_commands(self.event(kind))
            self.assertEqual(buyer.call_count, 3)

    def test_order_card_still_enters_its_business_handler(self):
        e = self.event(26, '订单卡片')
        e['payload']['orderId'] = 'platform'
        with patch.object(plugin_runtime, '_from_order_card', return_value=[]) as card, patch.object(
                plugin_runtime, '_from_buyer_message') as buyer:
            plugin_runtime.events_to_commands(e)
            card.assert_called_once()
            buyer.assert_not_called()

    def test_shipment_notice_syncs_before_filter_without_customer_message(self):
        e = self.event(14, '你已发货')
        with patch('application.order_status_sync.sync_delivery', return_value='已同步') as sync, patch.object(
                plugin_runtime, '_from_buyer_message') as buyer:
            commands = plugin_runtime.events_to_commands(e)
        sync.assert_called_once()
        buyer.assert_not_called()
        self.assertTrue(all(not c.text and c.extra.get('internal_only') for c in commands))

    def test_close_notice_syncs_before_filter(self):
        e = self.event(28, '未付款，买家关闭订单')
        e['payload']['orderId'] = 'platform'
        with patch('application.order_status_sync.sync_closed', return_value='已同步') as sync, patch.object(
                plugin_runtime, '_from_buyer_message') as buyer:
            commands = plugin_runtime.events_to_commands(e)
        sync.assert_called_once()
        buyer.assert_not_called()
        self.assertTrue(all(not c.text and c.extra.get('internal_only') for c in commands))
