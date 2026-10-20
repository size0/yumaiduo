import unittest
from copy import deepcopy

from tests._check_wanda_cost import client, SEATS
from application.wanda_cost import resolve_cost, WandaCostError


class SalesFallbackTests(unittest.TestCase):
    def cost(self, body=None, seats=None):
        return resolve_cost(client(seats=body or SEATS), city_id='80', cinema_id='1',
                            phone='123', showtime_resolver=lambda _: 'show',
                            seat_names=seats, seat_count=1)

    def test_selected_seat_without_activity_uses_sales_price(self):
        c = self.cost(seats=['3排3座'])
        self.assertEqual(c.unit_cost_fen, 5290)
        self.assertEqual(c.source, 'WANDA_SALES_PRICE')

    def test_member_price_still_has_priority(self):
        c = self.cost(seats=['6排9座'])
        self.assertEqual(c.unit_cost_fen, 5026)
        self.assertEqual(c.source, 'WANDA_MEMBER_PRICE')

    def test_present_but_broken_activity_does_not_fall_back(self):
        for act in ({}, {'price': 0}, {'price': -1}):
            body = deepcopy(SEATS)
            body['data']['realtimeSeats']['area'][2]['wPlusActivity'] = act
            with self.assertRaises(WandaCostError):
                self.cost(body, ['3排3座'])

    def test_missing_or_invalid_sales_price_cannot_quote_zero(self):
        for price in (None, 0, -1):
            body = deepcopy(SEATS)
            body['data']['realtimeSeats']['area'][2]['areaPrice']['salesPrice'] = price
            with self.assertRaises(WandaCostError):
                self.cost(body, ['3排3座'])

    def test_single_normal_area_can_quote_without_selected_seats(self):
        body = deepcopy(SEATS)
        body['data']['realtimeSeats']['area'] = [body['data']['realtimeSeats']['area'][2]]
        self.assertEqual(self.cost(body).unit_cost_fen, 5290)

    def test_unselected_uses_member_area_sales_not_cheaper_normal_areas(self):
        body = deepcopy(SEATS)
        for a in body['data']['realtimeSeats']['area']:
            a['wPlusActivity'] = None
        cost = self.cost(body)
        self.assertEqual(cost.unit_cost_fen, 5790)
        self.assertEqual(cost.source, "WANDA_SALES_PRICE")

    def test_selected_mixed_member_and_sales_areas_cannot_use_first_price(self):
        with self.assertRaises(WandaCostError):
            self.cost(seats=['6排9座', '3排3座'])

    def test_unavailable_selected_seat_can_quote_same_area_for_reselection(self):
        body = deepcopy(SEATS)
        for area in body['data']['realtimeSeats']['area']:
            for seat in area.get('seat', []):
                if seat.get('name') == '6排9座':
                    seat['status'] = 2
        cost = resolve_cost(client(seats=body), city_id='80', cinema_id='1',
                            phone='123', showtime_resolver=lambda _: 'show',
                            seat_names=['6排9座'], seat_count=1,
                            require_available=False,
                            allow_unavailable_area_quote=True)
        self.assertEqual(cost.unit_cost_fen, 5026)
        self.assertEqual(cost.area_name, 'W+专享')
