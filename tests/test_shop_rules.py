import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import time
from unittest.mock import patch
from application import store
from application.service import _shop_rule_engine
from domain.quote_rules import QuoteRule

class ShopRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(store, 'RULES_FILE', Path(self.tmp.name)/'rules.json')
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        store.save_rules([QuoteRule(name='公共', markup_yuan=1, limit_yuan=5)])

    def test_roundtrip_and_scope(self):
        self.assertEqual(store.rules_payload('107','A')['scope'],'shared')
        store.save_rules([QuoteRule(name='本店', brands=['万达影城'], markup_yuan=3, limit_yuan=5)],'107','A')
        self.assertEqual(store.load_rules('107','A')[0].brands,['万达影城'])
        self.assertEqual(store.load_rules('107','B')[0].name,'公共')
        self.assertEqual(store.load_rules('108','A')[0].name,'公共')
        self.assertEqual(store.rules_payload('107','A')['scope'],'shop')

    def test_empty_and_corrupt_do_not_restore_defaults(self):
        store.save_rules([], '107','A')
        self.assertEqual(store.load_rules('107','A'),[])
        store.rules_path('107','A').write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError): store.load_rules('107','A')

    def test_identity_path(self):
        with self.assertRaises(ValueError): store.rules_path('', 'A')
        self.assertEqual(store.rules_path('../x','../s').parent,store.RULES_FILE.parent/'shop_rules')

    def test_runtime_prices_use_shop_identity_and_refresh(self):
        def price(shop):
            engine=_shop_rule_engine(SimpleNamespace(tenant_id='107',shop_id=shop))
            return engine.quote(seat_count=1,now=time(12),estimate_total_fen=1000).total_price_fen
        a=price('A')
        store.save_rules([QuoteRule(name='A',markup_yuan=3,limit_yuan=5)],'107','A')
        self.assertEqual(price('A')-a,200)
        self.assertEqual(price('B'),a)

if __name__=='__main__': unittest.main()

