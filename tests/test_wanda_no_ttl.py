import unittest
from datetime import datetime,timedelta,timezone
from domain.quote import QuoteRecord
from domain.conversation import Conversation,ConvState

class WandaNoTTLTests(unittest.TestCase):
    def record(self,provider='WANDA',**kw):
        args=dict(quote_id='q',tenant_id='t',shop_id='s',buyer_id='b',chat_id='c',provider=provider,request_type='EXACT_SEATS',selected_seats=['1排1座'],ticket_count=1,total_sell_price_fen=1000)
        args.update(kw);return QuoteRecord(**args)
    def test_wanda_without_expiry_is_ready(self):
        self.assertTrue(self.record().is_order_ready())
    def test_legacy_wanda_deadline_is_ignored(self):
        q=self.record(expires_at=datetime.now(timezone.utc)-timedelta(days=1))
        self.assertTrue(q.is_usable());self.assertFalse(q.expire_if_needed());self.assertEqual(q.status,'READY')
    def test_liangpiao_still_expires(self):
        q=self.record('LIANGPIAO',expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        self.assertFalse(q.is_order_ready());self.assertTrue(q.expire_if_needed())
        self.assertFalse(self.record('LIANGPIAO').is_order_ready())
    def test_cancelled_superseded_not_revived_by_policy(self):
        for state in ('CANCELLED','SUPERSEDED','EXPIRED'):
            self.assertFalse(self.record(status=state).is_order_ready())
    def test_wanda_conversation_has_no_deadline(self):
        c=Conversation(chat_id='c',channel='wanda',state=ConvState.QUOTED)
        c._touch_quote();self.assertIsNone(c.quote_expires_at)
        c.quote_expires_at=datetime.now()-timedelta(days=1)
        self.assertFalse(c.quote_expired());self.assertIsNone(c.seconds_left())
    def test_liangpiao_conversation_still_expires(self):
        c=Conversation(chat_id='c',channel='liangpiao',state=ConvState.QUOTED)
        c._touch_quote();self.assertIsNotNone(c.quote_expires_at)
        c.quote_expires_at=datetime.now()-timedelta(days=1)
        self.assertTrue(c.quote_expired())


    def test_migration_restores_only_latest_time_expired_wanda(self):
        import importlib.util
        from pathlib import Path
        from domain.quote import QuoteStore
        spec=importlib.util.spec_from_file_location('wanda_migrate',Path(__file__).resolve().parents[1]/'release-wanda-no-ttl-20261016/migrate.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        store=QuoteStore();self.addCleanup(store.close)
        now=datetime.now(timezone.utc)
        for qid,provider,buyer,status,created in [('old','WANDA','b','EXPIRED',now-timedelta(hours=2)),('latest','WANDA','b','EXPIRED',now-timedelta(hours=1)),('lp','LIANGPIAO','lp','EXPIRED',now),('cancel','WANDA','cancel','CANCELLED',now),('super','WANDA','super','SUPERSEDED',now)]:
            q=self.record(provider,quote_id=qid,buyer_id=buyer,status=status,created_at=created,expires_at=now-timedelta(minutes=1),show=(now+timedelta(days=1)).strftime('%Y-%m-%d %H:%M'))
            store.create(q)
        changed=module.migrate(store.connection)
        self.assertEqual([r['quote_id'] for r in changed if r['restored']],['latest'])
        self.assertTrue(store.get('latest').is_order_ready())
        self.assertFalse(store.get('old').is_order_ready())
        self.assertIsNotNone(store.get('lp').expires_at)
        self.assertEqual(module.migrate(store.connection),[])

    def test_no_ttl_does_not_allow_already_started_show(self):
        q=self.record(show='2020-01-01 12:00')
        self.assertFalse(q.is_usable());self.assertFalse(q.is_order_ready())
