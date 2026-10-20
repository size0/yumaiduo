import unittest
import templates as tpl

class ShowTimeDisplayTests(unittest.TestCase):
    def test_formats_both_render_paths_without_mutating_facts(self):
        for value in ('2026-10-17T15:10:00+08:00', '2026-10-17T07:10:00Z', '2026-10-17 15:10:00'):
            facts={'show_time':value}
            for render in (tpl.render, tpl.render_messages):
                text=str(render('WANDA_UNIT_PRICE',facts,channel='wanda'))
                self.assertIn('2026-10-17 15:10',text)
                self.assertNotIn('+08:00',text)
            self.assertEqual(facts['show_time'],value)
    def test_unknown_time_is_preserved(self):
        self.assertEqual(tpl._display_values({'show_time':'待确认'})['show_time'],'待确认')
    def test_catalog_contains_editable_unit_price_and_conditions(self):
        row=next(r for g in tpl.catalog() for r in g['items'] if r['key']=='WANDA_UNIT_PRICE')
        self.assertIn('张数已确认则发送整单报价卡',row['trigger'])
        self.assertIn('movie',[v['key'] for v in row['variables']])
        self.assertFalse(row['locked'])
    def test_blank_channel_still_silent(self):
        row=tpl.find('WANDA_UNIT_PRICE')
        old=dict(row.get('channel_text') or {})
        try:
            row['channel_text']={'wanda':''}
            self.assertEqual(tpl.render_messages('WANDA_UNIT_PRICE',{'show_time':'2026-10-17T15:10:00+08:00'},channel='wanda'),[])
        finally:
            row['channel_text']=old
