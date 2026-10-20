"""A transient city lookup failure must not poison future Wanda requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from application.ticket_flow import TicketFlow
from domain.quote_rules import QuoteRuleEngine
from integrations.wanda.bridge import WandaBridgeError


class CityCodeCacheTests(unittest.TestCase):
    def make_flow(self, responses):
        api = Mock()
        api.ch4_city_list.side_effect = responses
        return TicketFlow(api, QuoteRuleEngine([]), tel='13800000000'), api

    def test_timeout_can_recover_on_next_request(self):
        flow, api = self.make_flow([TimeoutError(), [{'name': '包头', 'code': '150200'}]])
        self.assertEqual(flow._liangpiao_city_code('包头'), '')
        self.assertNotIn('包头', flow._city_code_cache)
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')
        self.assertEqual(api.ch4_city_list.call_count, 2)

    def test_empty_response_is_not_cached(self):
        flow, _ = self.make_flow([[], [{'name': '包头', 'code': '150200'}]])
        self.assertEqual(flow._liangpiao_city_code('包头'), '')
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')

    def test_empty_code_is_not_cached(self):
        flow, _ = self.make_flow([[{'name': '包头', 'code': ''}], [{'name': '包头', 'code': '150200'}]])
        self.assertEqual(flow._liangpiao_city_code('包头'), '')
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')

    def test_preexisting_negative_cache_is_discarded(self):
        flow, api = self.make_flow([[{'name': '包头', 'code': '150200'}]])
        flow._city_code_cache = {'包头': ''}
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')
        api.ch4_city_list.assert_called_once_with(keyword='包头')

    def test_successful_code_is_reused(self):
        flow, api = self.make_flow([[{'name': '包头', 'code': '150200'}]])
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')
        self.assertEqual(flow._liangpiao_city_code('包头'), '150200')
        self.assertEqual(api.ch4_city_list.call_count, 1)

    def test_blank_input_does_not_query(self):
        flow, api = self.make_flow([])
        self.assertEqual(flow._liangpiao_city_code('  '), '')
        api.ch4_city_list.assert_not_called()

    def test_wanda_without_bridge_stays_blocked(self):
        flow, api = self.make_flow([])
        result = SimpleNamespace(brand_name='万达影城', cinema_name='万达影城', cinema_id='1', city_name='包头')
        with self.assertRaises(WandaBridgeError) as raised:
            flow._resolve_channel(result)
        self.assertEqual(raised.exception.code, 'WANDA_ROUTE_UNRESOLVED')
        api.ch5_preflight.assert_not_called()

    def test_lookup_failure_does_not_switch_to_liangpiao(self):
        flow, api = self.make_flow([TimeoutError(), [{'name': '包头', 'code': '150200'}]])
        flow.wanda_bridge = Mock()
        route = SimpleNamespace(use_wanda=True)
        flow.wanda_bridge.wanda_lookup.return_value = route
        result = SimpleNamespace(brand_name='万达影城', cinema_name='万达影城', cinema_id='1', city_name='包头')
        with self.assertRaises(WandaBridgeError) as raised:
            flow._resolve_channel(result)
        self.assertEqual(raised.exception.code, 'CITY_CODE_UNAVAILABLE')
        flow.wanda_bridge.wanda_lookup.assert_not_called()
        self.assertIs(flow._resolve_channel(result), route)
        api.ch5_preflight.assert_not_called()
