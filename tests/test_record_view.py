import unittest
from unittest.mock import patch
from application.record_view import day, select, buyer_names, _CACHE

class RecordsTests(unittest.TestCase):
    def test_shop_scope_requires_tenant_and_shop(self):
        rows=[dict(tenant_id=t,shop_id=s,created_at='2026-10-16',bucket='unpaid')
              for t,s in [('one','shop'),('two','shop'),('one','other')]]
        self.assertEqual(select(rows,dict(tenant_id='one',shop_id='shop'),'orders'),rows[:1])
        self.assertEqual(select(rows,dict(shop_id='shop'),'orders'),[])
    def test_china_midnight(self):
        self.assertEqual(day('2026-10-15T16:01:00+00:00'),'2026-10-16')
        rows=[dict(created_at='2026-10-15T16:01:00+00:00',buyer_name='红薯麻麻',status='READY')]
        self.assertEqual(select(rows,dict(start='2026-10-16',end='2026-10-16',q='红薯'),'quotes'),rows)
        self.assertEqual(select(rows,dict(start='2026-10-17',end='2026-10-16'),'quotes'),[])
    def test_search_before_pagination(self):
        rows=[dict(created_at='2026-10-16',buyer_name='买家',movie=str(i),status='READY') for i in range(250)]
        self.assertEqual(len(select(rows,dict(q='249'),'quotes')),1)
        self.assertEqual(len(select(rows,dict(status='CONFIRMED'),'quotes')),0)
    def test_names_scope_and_failure(self):
        _CACHE.clear()
        rows=[dict(tenant_id='t',shop_id='s',buyer_id='b'),dict(tenant_id='t',shop_id='other',buyer_id='b')]
        def reply(path,body):
            import json
            return 200,json.dumps(dict(shop_id='s',buyer_id='b',buyer_name='昵称'))
        with patch('integrations.yumaiduo.shop_client.resolve_plugin_secret',return_value='test'), patch('integrations.yumaiduo.shop_client.PluginShopsClient._post',side_effect=reply):
            names=buyer_names(rows, wait=True)
        self.assertEqual(names[('t','s','b')],'昵称')
        self.assertEqual(names[('t','other','b')],'')

class ProjectionTests(unittest.TestCase):
    def test_old_reprice_is_not_current_success(self):
        import json
        from types import SimpleNamespace
        from threading import RLock
        from application.record_view import enrich
        command=dict(order_id='p',tenant_id='t',shop_id='s',buyer_id='b',status='SUCCEEDED',target_amount_fen=100,replacement_quote_id='old')
        queue=SimpleNamespace(lock=RLock(),connection=SimpleNamespace(execute=lambda *args:[(json.dumps(command),)]))
        row=dict(order_id='o',quote_id='new',tenant_id='t',shop_id='s',buyer_id='b',platform_order_id='p',provider='WANDA',provider_order_id='p',amount_yuan=2)
        data=dict(quotes=[],orders=[row])
        with patch('application.record_view.buyer_names',return_value={}),patch('application.plugin_api.queue',return_value=queue):enrich(data)
        self.assertEqual(row['reprice_status'],'STALE')
        command['status']='PENDING'
        with patch('application.record_view.buyer_names',return_value={}),patch('application.plugin_api.queue',return_value=queue):enrich(data)
        self.assertEqual(row['reprice_status'],'PENDING')

    def test_delivery_candidates_survive_filter_and_pagination(self):
        from application import ledger
        from domain.order import Order, OrderStore
        from types import SimpleNamespace
        store=OrderStore();self.addCleanup(store.close)
        for i in range(55):
            store.create(Order(order_id=str(i),quote_id='q'+str(i),idempotency_key='i'+str(i),tenant_id='t',shop_id='s',buyer_id='b',chat_id='c',provider='WANDA',payment_state='CONFIRMED',state='PAID',amount_fen=100,paid_fen=100))
        with patch.object(ledger,'stores',return_value=SimpleNamespace(orders=store,quotes=None)),patch('application.record_view.enrich'):
            data=ledger.orders_payload(filters={'kind':'orders','status':'unpaid'})
            self.assertEqual(data['orders'],[])
            self.assertEqual(len(data['delivery_candidates']),55)
            data=ledger.orders_payload(filters={'kind':'orders','offset':'50'})
            self.assertEqual(len(data['orders']),5)
            self.assertEqual(data['page']['total'],55)
            data=ledger.orders_payload(filters={'kind':'orders','tenant_id':'other','shop_id':'s'})
            self.assertEqual(data['page']['total'],0)
            self.assertEqual(data['buckets']['all'],0)
            self.assertEqual(data['counts']['orders'],0)
            self.assertEqual(len(data['delivery_candidates']),55)

if __name__=='__main__':unittest.main()
