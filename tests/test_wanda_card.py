import unittest
from types import SimpleNamespace
from unittest.mock import patch
import templates as tpl
from application.ticket_flow import FlowResult, TicketFlow
from tests._check_channel_route import result

class WandaCardTests(unittest.TestCase):
    def emit(self, channel):
        flow=TicketFlow.__new__(TicketFlow)
        return flow._emit_quote_card(result(cinema_hit_nums=3),[{}, {}, {}],SimpleNamespace(unit_price_fen=3670,total_price_fen=11010,seat_count=3),FlowResult(channel=channel))

    def test_wanda_uses_custom_card_without_multi_references(self):
        card=tpl.find('QUOTE_CARD')
        with patch.dict(card,channel_text={'wanda':'自定义报价 {total_price}'}):
            out=self.emit('wanda')
        self.assertEqual(out.template_keys,['QUOTE_CARD'])
        self.assertEqual(out.messages,['自定义报价 110.10'])

    def test_wanda_blank_card_remains_silent(self):
        with patch.dict(tpl.find('QUOTE_CARD'),channel_text={'wanda':''}):
            self.assertEqual(self.emit('wanda').messages,[])

    def test_liangpiao_retains_existing_multi_template(self):
        self.assertEqual(self.emit('liangpiao').template_keys,['QUOTE_CARD_MULTI'])
