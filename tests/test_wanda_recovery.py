"""Wanda recovery must verify facts, never select unrelated supplier candidates."""
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from application.wanda_recovery import recover, eligible, RecoveryError
from application.wanda_showtime import WandaShow
from application.ticket_flow import FlowResult
from domain.conversation import Conversation, ConvState
from domain.quote import QuoteStore
from tests._check_channel_route import flow, result, MAP_NONE


class WandaRecoveryTests(unittest.TestCase):
    def test_unbranded_cinema_is_eligible_by_readonly_bridge(self):
        raw = dict(city='厦门', cinema='厦门鹭港广场店', film='生化危机：爆发夜',
                   hall='1号CINITY厅', showtime='2026-10-17 18:25:00')
        r = result(match_level='NONE', raw_results=raw)
        self.assertTrue(eligible(r, Mock(match_facts=Mock())))
        self.assertFalse(eligible(r, None))
        r.match_level = 'EXACT'
        self.assertFalse(eligible(r, Mock()))

    def test_truncation_flag_does_not_override_exact_authoritative_cinema(self):
        self.raw['cinema'] = '厦门鹭港广场店'
        self.raw['city'] = '厦门'
        self.raw['cinemaTruncated'] = True
        self.payload['data']['city'] = dict(id='87', name='厦门')
        self.payload['data']['cinema'] = dict(id=6892, name='厦门鹭港广场店', address='厦门市湖里区')
        recovered = self.recover()
        self.assertEqual(recovered.wanda_verified_route.wanda_cinema_id, '6892')
        self.bridge.match_facts.assert_called_once()

    def test_actual_truncated_or_wrong_city_name_cannot_be_accepted(self):
        self.raw['cinemaTruncated'] = True
        self.raw['cinema'] = '太仓万达'
        with self.assertRaises(RecoveryError):
            self.recover()
        self.raw['cinema'] = '太仓万达广场店'
        self.raw['city'] = '厦门'
        with self.assertRaises(RecoveryError):
            self.recover()

    def test_unresolved_channel_does_not_resurrect_cleared_notice(self):
        f, _, _ = flow(MAP_NONE)
        conv = Conversation(chat_id='c')
        out = FlowResult(channel=None, template_keys=['RECOGNIZING_TIP'], messages=['请确认完整影院名。'])
        with patch.object(f, '_recognize_screenshot', return_value=out), patch(
                'application.ticket_flow.tpl.find', return_value={'channel_text': {'wanda': ''}}), patch(
                'application.ticket_flow.tpl.render_messages') as render:
            actual = f.on_screenshot(conv, 'image')
        render.assert_not_called()
        self.assertEqual(actual.messages, ['请确认完整影院名。'])

    def test_unbranded_exact_recovery_reaches_quote_and_respects_empty_wanda_notice(self):
        f, api, _ = flow(MAP_NONE)
        f.quote_store = QuoteStore()
        self.addCleanup(f.quote_store.close)
        recognized = result(match_level='NONE', raw_results=dict(self.raw, cinema='厦门鹭港广场店'))
        verified = result(expected_seat_count=None)
        verified.cinema_id = verified.show_id = verified.movie_id = None
        from integrations.wanda.bridge import CinemaRoute
        verified.wanda_verified_route = CinemaRoute(mapped=True, wanda_city_id='87', wanda_cinema_id='6892', wanda_cinema_name='厦门鹭港广场店')
        verified.wanda_verified_show_id = '103155648'
        api.ch7_recognize = Mock(return_value=recognized)
        conv = Conversation(chat_id='c', tenant_id='t', shop_id='s', buyer_id='b')
        import templates
        find = templates.find
        def scoped_find(key):
            row = find(key)
            return dict(row, channel_text={'wanda': ''}) if key == 'RECOGNIZING_TIP' else row
        with patch('application.wanda_recovery.recover', return_value=verified) as recovery, patch(
                'application.ticket_flow._second_quote_wait_seconds', return_value=0), patch(
                'domain.quote.models._now', return_value=datetime(2026,10,13,tzinfo=timezone.utc)), patch(
                'application.ticket_flow.tpl.find', side_effect=scoped_find):
            out = f.on_screenshot(conv, 'image')
        recovery.assert_called_once()
        self.assertIn('WANDA_UNIT_PRICE', out.template_keys, out)
        self.assertNotIn('系统正在识别', ''.join(out.messages))
        self.assertFalse(api.preflight_calls)
        self.assertEqual(conv.channel, 'wanda')

    def setUp(self):
        self.raw = dict(city='太仓', cinema='太仓万达广场店', film='生化危机：爆发夜',
                        hall='IMAX激光厅 (儿童需购票)', showtime='2026-10-16 19:30:00', seat=[])
        self.payload = dict(success=True, data=dict(city=dict(id='279', name='苏州'),
            cinema=dict(id=369, name='太仓万达广场店', address='江苏省太仓市上海东路188号'),
            showtime=dict(id=103313535), showtime_match=dict(candidate_count=1)))
        self.show = WandaShow(showtime_id='103313535', date='20261016',
            realtime_ms=int(datetime.fromisoformat('2026-10-16T19:30:00+08:00').timestamp()*1000),
            film_name=self.raw['film'], hall_name='IMAX激光厅（儿童需购票）')
        self.bridge = Mock()
        self.bridge.match_facts.return_value = self.payload

    def recover(self):
        # pick_show has its own tested legacy local-time behaviour; recovery must
        # independently check the exact China-time instant even if selection errs.
        with patch('application.wanda_recovery.fetch_shows', return_value=[self.show]), patch(
                'application.wanda_recovery.pick_show', return_value=self.show):
            return recover(self.bridge, result(match_level='CANDIDATE', raw_results=self.raw))

    def test_taicang_administrative_city_and_hall_punctuation(self):
        r = self.recover()
        self.assertEqual(r.wanda_verified_route.wanda_cinema_id, '369')
        self.assertEqual(r.city_name, '太仓')
        self.assertIsNone(r.cinema_id)
        self.assertIsNone(r.show_id)
        self.assertEqual(r.seats, [])
        self.assertEqual(r.candidates, {})

    def test_display_screen_suffix_accepts_same_unique_hall(self):
        self.raw['hall']='激光IMAX厅-福建省银幕最大IMAX_Cola银幕'
        self.show.hall_name='激光IMAX厅-福建省银幕最大IMAX_Cola'
        self.assertEqual(self.recover().hall_name,self.show.hall_name)

    def test_screen_suffix_does_not_hide_a_different_hall_number(self):
        self.raw['hall']='8号IMAX厅银幕'
        self.show.hall_name='9号IMAX厅'
        with self.assertRaises(RecoveryError):self.recover()

    def test_mapped_nonbranded_recovery_failure_does_not_resurrect_old_candidates(self):
        from integrations.wanda.bridge import CinemaRoute
        f,_,_=flow(MAP_NONE)
        f.wanda_bridge=Mock()
        f.wanda_bridge.wanda_lookup.return_value=CinemaRoute(mapped=True,wanda_city_id='87',wanda_cinema_id='6862')
        r=result(match_level='CANDIDATE',cinema_id=10025,raw_results=dict(self.raw,cinema='厦门寰映影城海上世界店'))
        r.final_results={'cityCode':'350200'}
        conv=Conversation(chat_id='c')
        with patch('application.wanda_recovery.recover',side_effect=RecoveryError('场次未核实')):
            out=f._after_recognition(conv,r,FlowResult())
        self.assertIn('WANDA_MATCH_PENDING',out.template_keys)
        self.assertNotIn('DISAMBIG',out.template_keys)

    def test_rejects_wrong_cinema_city_hall_film_time_and_ambiguity(self):
        for kind in ('cinema', 'city', 'hall', 'film', 'time', 'ambiguous', 'seat'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'cinema': self.payload['data']['cinema']['name'] = '泉州万达广场店'
                if kind == 'city': self.payload['data']['cinema']['address'] = '苏州市工业园区'
                if kind == 'hall': self.show.hall_name = '1号厅'
                if kind == 'film': self.show.film_name = '其他影片'
                if kind == 'time': self.show.realtime_ms += 60000
                if kind == 'ambiguous': self.payload['data']['showtime_match']['candidate_count'] = 2
                if kind == 'seat': self.raw['seat'] = [{}]
                with self.assertRaises(RecoveryError): self.recover()

    def test_failure_does_not_emit_foreign_candidates_or_keep_quote(self):
        f, api, _ = flow(MAP_NONE)
        f.wanda_bridge.match_facts = Mock(side_effect=TimeoutError())
        conv = Conversation(chat_id='c', state=ConvState.RECOGNIZING)
        conv.quote_id = 'old'
        r = result(match_level='CANDIDATE', raw_results=self.raw, candidates={'cinema': [{'name':'泉州'}]})
        out = f._after_recognition(conv, r, FlowResult())
        self.assertFalse(out.ok)
        self.assertIn('WANDA_MATCH_PENDING', out.template_keys)
        self.assertEqual(conv.state, ConvState.IDLE)
        self.assertEqual(conv.quote_id, '')
        self.assertNotIn('泉州', ''.join(out.messages))
        self.assertFalse(api.preflight_calls)

    def test_recovered_unknown_quantity_continues_wanda_unit_quote(self):
        f, api, _ = flow(MAP_NONE)
        f.quote_store = QuoteStore()
        self.addCleanup(f.quote_store.close)
        r = result(expected_seat_count=None)
        r.cinema_id = r.show_id = r.movie_id = None
        from integrations.wanda.bridge import CinemaRoute
        r.wanda_verified_route = CinemaRoute(mapped=True, wanda_city_id='80', wanda_cinema_id='379', wanda_cinema_name=r.cinema_name)
        r.wanda_verified_show_id = '103155648'
        conv = Conversation(chat_id='c', tenant_id='t', shop_id='s', buyer_id='b', state=ConvState.RECOGNIZING)
        with patch('application.wanda_recovery.recover', return_value=r), patch(
                'application.ticket_flow._second_quote_wait_seconds', return_value=0), patch(
                'domain.quote.models._now', return_value=datetime(2026,10,13,tzinfo=timezone.utc)):
            out = f._after_recognition(conv, result(match_level='CANDIDATE', raw_results=self.raw), FlowResult())
        self.assertIn('WANDA_UNIT_PRICE', out.template_keys, out)
        self.assertFalse(api.preflight_calls)
        self.assertFalse(api.city_calls)
        self.assertEqual(conv.channel, 'wanda')
        self.assertEqual(f.quote_store.get(conv.quote_id).ticket_count, 0)

    def test_changed_verified_show_never_reaches_cost(self):
        f, api, _ = flow(MAP_NONE)
        r = result()
        from integrations.wanda.bridge import CinemaRoute
        r.wanda_verified_route = CinemaRoute(mapped=True, wanda_city_id='80', wanda_cinema_id='379')
        r.wanda_verified_show_id = 'different-original-id'
        with patch('application.ticket_flow.resolve_cost') as cost:
            out = f._build_quote(Conversation(chat_id='c'), r, FlowResult())
        self.assertFalse(out.ok)
        cost.assert_not_called()
        self.assertFalse(api.preflight_calls)
