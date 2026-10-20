import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from settings import Assistant, Settings
from application.config_patch import validate_patch
from application.manual_control import close_statuses
from application.service import Session, TicketService


class ConfigAuditTests(unittest.TestCase):
    def test_bad_patch_types(self):
        for value in (None, [], 'bad'):
            self.assertTrue(validate_patch(Settings(), value))
        for patch_value in ({'agent_enabled': 'banana'}, {'persona': None},
                            {'memory_messages': 1.5}, {'model_options': ['x']},
                            {'takeover': 5}, {'takeover_seconds': 5}):
            self.assertTrue(validate_patch(Assistant(), patch_value, section='assistant'))

    def test_legacy_form_values_and_invalid_enums(self):
        self.assertFalse(validate_patch(Settings(), {'message_delay': {'enabled': '1', 'min_ms': '100', 'max_ms': 200}}))
        for value in ('NaN', '1.5', None, True):
            self.assertTrue(validate_patch(Settings(), {'message_delay': {'min_ms': value}}))
        self.assertTrue(validate_patch(Settings(), {'ticket_release': {'mode': 'unknown'}}))
        self.assertTrue(validate_patch(Settings(), {'night_recognize': {'start': '25:00'}}))

    def test_assistant_validation_never_crashes(self):
        for field, value in [('persona', None), ('experience', 5), ('business_scope', []), ('business_scope', ['不限制', '影票代订']), ('memory_messages', 5.5)]:
            a = Assistant(); setattr(a, field, value)
            self.assertTrue(a.validate())

    def test_static_prompt_contains_scope_and_experience(self):
        a = Assistant(experience='本店经验测试')
        prompt = a.build_prompt()
        self.assertIn('【业务范围】', prompt)
        self.assertIn('本店经验测试', prompt)
        self.assertIn('工具', prompt)

    def test_history_filters_age_not_order_facts(self):
        now = datetime.now(timezone.utc)
        row = lambda text, stamp: {'role': 'user', 'content': text, 'created_at': stamp}
        s = SimpleNamespace(history=[row('old', (now-timedelta(hours=25)).isoformat()),
                                     row('new', now.isoformat()), row('legacy', ''),
                                     row('naive', now.replace(tzinfo=None).isoformat())],
                            conversation=object())
        fact = s.conversation
        self.assertEqual(TicketService._history(None, s, Settings()), [{'role': 'user', 'content': 'new'}])
        self.assertIs(s.conversation, fact)

    def test_remember_timestamps(self):
        s = SimpleNamespace(history=[])
        Session.remember(s, 'hello', 'reply')
        self.assertTrue(all(datetime.fromisoformat(r['created_at']).tzinfo for r in s.history))

    def test_delay_range_and_idempotence(self):
        from application.plugin_api import _apply_message_delay
        cfg = Settings(); cfg.message_delay.enabled = True
        cfg.message_delay.min_ms = 100; cfg.message_delay.max_ms = 900
        cmd = SimpleNamespace(type='SEND_MESSAGE', not_before='')
        with patch('settings.load', return_value=cfg), patch('random.randint', return_value=900) as rand:
            _apply_message_delay(cmd)
            first = cmd.not_before
            _apply_message_delay(cmd)
            rand.assert_called_once_with(100, 900)
            self.assertEqual(cmd.not_before, first)

    def test_empty_close_status_preserved(self):
        cfg = Settings(); cfg.manual.close_on_status = []
        self.assertEqual(close_statuses(cfg), [])

    def test_blank_reminders_and_zero_delay(self):
        from application.reminders import r_movie_remind, r_urge_order, r_urge_receive, r_urge_review
        now = datetime.now(timezone.utc)
        cfg = Settings()
        order = {'state': 'TICKETED', 'shipped': True, 'delivered_at': now.isoformat(),
                 'created_at': now.isoformat(), 'show': now.strftime('%Y-%m-%d %H:%M')}
        for name, fn in [('urge_receive', r_urge_receive), ('urge_review', r_urge_review), ('urge_order', r_urge_order)]:
            order['state'] = 'AWAITING_PAYMENT' if name == 'urge_order' else 'TICKETED'
            section = getattr(cfg, name); section.delay_minutes = 0; section.message = '配置文案'
            self.assertEqual(fn(now, order, cfg)[0], '配置文案')
            section.message = ''
            self.assertIsNone(fn(now, order, cfg))
        cfg.movie_remind.message = ''; cfg.movie_remind.advance_minutes = 0
        self.assertIsNone(r_movie_remind(now, order, cfg))

    def test_blank_defaults_do_not_enqueue_reminders(self):
        from application.plugin_api import run_reminders
        cfg = Settings()
        for name in ('movie_remind', 'urge_receive', 'urge_order', 'urge_review'):
            getattr(cfg, name).message = ''
        # Shop overrides require looking up order identities even when shared
        # defaults are blank. Assert the user-visible contract: no send command.
        with (patch('settings.load', return_value=cfg),
              patch('application.ledger.orders_payload', return_value={'orders': []}),
              patch('application.plugin_api.queue') as queue):
            self.assertEqual(run_reminders(force=True), 0)
            queue.return_value.enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
