import unittest
from types import SimpleNamespace

from domain.quote import QuoteRecord, QuoteStore
from application.demand import bind_quote, version_matches


class DemandVersionTests(unittest.TestCase):
    def setUp(self):
        self.store = QuoteStore(':memory:')
        self.ident = dict(tenant_id='t', shop_id='s', buyer_id='b', chat_id='c')
        self.conv = SimpleNamespace(**self.ident)

    def tearDown(self):
        self.store.close()

    def quote(self, key, count):
        return QuoteRecord(quote_id=key, provider='WANDA', cinema='影院', movie='影片',
                           show='2099-01-01 21:15', ticket_count=count,
                           metadata={'quote_kind': 'ORDER_TOTAL' if count else 'UNIT_PRICE'}, **self.ident)

    def test_quantity_changes_revision_and_quote_kind(self):
        a = self.quote('unit', 0); bind_quote(self.conv, a, self.store); self.store.create(a)
        b = self.quote('three', 3); bind_quote(self.conv, b, self.store); self.store.create(b)
        self.assertEqual(a.demand_id, b.demand_id)
        self.assertEqual((a.demand_rev, b.demand_rev), (1, 2))
        self.assertEqual(self.store.get('unit').ticket_count, 0)
        self.assertEqual(self.store.get('unit').status, 'SUPERSEDED')
        self.assertFalse(version_matches(b, a.demand_id, a.demand_rev))

    def test_new_demand_preserves_other_demand_quote(self):
        a = self.quote('first', 3); bind_quote(self.conv, a, self.store); self.store.create(a)
        self.conv.demand_id = ''; self.conv.demand_rev = 0
        b = self.quote('other', 4); bind_quote(self.conv, b, self.store); self.store.create(b)
        self.assertNotEqual(a.demand_id, b.demand_id)
        self.assertEqual(self.store.get('first').status, 'READY')

    def test_same_facts_reprice_does_not_change_demand_revision(self):
        a = self.quote('a', 3); bind_quote(self.conv, a, self.store); self.store.create(a)
        b = self.quote('b', 3); b.unit_sell_price_fen = 5000
        bind_quote(self.conv, b, self.store)
        self.assertEqual(a.demand_rev, b.demand_rev)

    def test_identity_isolation(self):
        a = self.quote('a', 3); bind_quote(self.conv, a, self.store); self.store.create(a)
        b = self.quote('b', 4); b.buyer_id = 'other'
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            bind_quote(self.conv, b, self.store)

    def test_legacy_deserialization_remains_readable(self):
        q = QuoteRecord.from_dict(self.quote('legacy', 3).to_dict())
        self.assertEqual(q.demand_rev, 0)
        self.assertFalse(version_matches(q, '', 0))
