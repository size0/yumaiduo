import unittest,tempfile,json,threading
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import settings,templates
from application import shop_scope as sc,store,plugin_runtime

class ShopPushTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  p=patch.object(sc,'ROOT',Path(self.tmp.name)/'shops');p.start();self.addCleanup(p.stop)
  p=patch.object(settings,'CONFIG_FILE',Path(self.tmp.name)/'settings.json');p.start();self.addCleanup(p.stop)
  p=patch.object(store,'CONFIG_FILE',settings.CONFIG_FILE);p.start();self.addCleanup(p.stop)
  settings.save(settings.Settings())
 def test_assistant_snapshot_isolated_from_features_and_other_shop(self):
  with sc.scope('107','A','assistant'):
   cfg=settings.load();cfg.assistant.persona='A客服';store.save_config(cfg)
   self.assertEqual(settings.load().assistant.persona,'A客服')
   sc.push('assistant','107','B')
  with sc.scope('107','B'):
   self.assertEqual(store.load_config().assistant.persona,'A客服')
   self.assertNotIn('features',sc.read())
   self.assertNotIn('model_api_key',json.dumps(sc.read()))
  with sc.scope('107','A','assistant'):
   cfg=settings.load();cfg.assistant.persona='更新';store.save_config(cfg)
  with sc.scope('107','B'):self.assertEqual(settings.load().assistant.persona,'A客服')
  with sc.scope('107','C'):self.assertNotEqual(settings.load().assistant.persona,'A客服')
 def test_features_snapshot_preserves_assistant(self):
  with sc.scope('107','B','assistant'):
   c=settings.load();c.assistant.persona='B';store.save_config(c)
  with sc.scope('107','A','settings'):
   c=settings.load();c.order_ship.auto_ship=False;store.save_config(c)
   sc.push('features','107','B')
  with sc.scope('107','B'):
   self.assertFalse(settings.load().order_ship.auto_ship);self.assertEqual(settings.load().assistant.persona,'B')
 def test_blank_template_and_channels_survive_push_without_global_mutation(self):
  original=templates.text_for('QUOTE_CARD','wanda')
  with sc.scope('107','A'):
   sc.save_template('QUOTE_CARD','','wanda')
   sc.save_template('QUOTE_CARD','良票定制','liangpiao')
   sc.push('templates','107','B')
  self.assertEqual(templates.text_for('QUOTE_CARD','wanda'),original)
  with sc.scope('107','B'):
   self.assertEqual(templates.render_messages('QUOTE_CARD',channel='wanda'),[])
   self.assertEqual(templates.text_for('QUOTE_CARD','liangpiao'),'良票定制')
   row=next(r for g in templates.catalog() for r in g['items'] if r['key']=='QUOTE_CARD')
   self.assertEqual(row['channel_text']['wanda'],'')
 def test_event_entry_and_threads_do_not_leak_scope(self):
  for shop in ['A','B']:
   with sc.scope('107',shop):sc.update({'assistant':{'persona':shop}})
  def run(shop):
   with sc.scope('107',shop):return settings.load().assistant.persona
  with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(list(pool.map(run,['A','B'])),['A','B'])
  with patch.object(plugin_runtime,'_scoped_events_to_commands',side_effect=lambda event:[settings.load().assistant.persona]):
   self.assertEqual(plugin_runtime.events_to_commands({'tenantId':'107','shopId':'A'}),['A'])
  self.assertEqual(sc.CURRENT.get(),('',''))
 def test_http_scope_and_restore(self):
  class Handler:
   path='/ta/api/assistant?tenant_id=107&shop_id=A'
   @sc.http_scope
   def get(self):return sc.CURRENT.get(),sc.SECTION.get()
  self.assertEqual(Handler().get(),(('107','A'),'assistant'));self.assertEqual(sc.CURRENT.get(),('',''))
 def test_image_asset_isolated(self):
  from application import ticket_image
  with sc.scope('107','A'):ticket_image.save({'data':'test','name':'A'});sc.push('features','107','B')
  with sc.scope('107','B'):self.assertEqual(ticket_image.load()['name'],'A')
