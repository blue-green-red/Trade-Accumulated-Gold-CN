import json
import unittest
import test_risk_accounts as risk
from monitor.controls import set_enabled
from monitor.web import dashboard,history_page


class ModelVisibilityTests(unittest.TestCase):
    def setUp(self):self.f=risk.RiskAccountTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()

    def test_disabled_hidden_everywhere_but_control_and_restored_without_deleting_data(self):
        f=self.f;c=f.m.store.conn
        counts=[c.execute('select count(*) from '+t).fetchone()[0] for t in ('ai_portfolios','ai_portfolio_fills')]
        set_enabled(c,'claude',False)
        d=dashboard(f.settings)
        self.assertEqual(len(d['autonomous']['accounts']),6)
        for field in ('leaderboard','fills','equity','comparison','ai','scorecard','notifications','strategy_names','series_order','autonomous'):
            self.assertNotIn('claude',json.dumps(d[field],ensure_ascii=False).lower(),field)
        self.assertEqual(next(x for x in d['model_controls'] if x['key']=='claude'),{'key':'claude','name':'Claude','enabled':False})
        page=history_page(f.settings,{'kind':['fills'],'limit':['2']})
        self.assertEqual(len(page['items']),2)
        self.assertTrue(all(r['account'].startswith('codex:') for r in page['items']))
        self.assertEqual(history_page(f.settings,{'kind':['fills'],'model':['claude']})['items'],[])
        self.assertEqual(counts,[c.execute('select count(*) from '+t).fetchone()[0] for t in ('ai_portfolios','ai_portfolio_fills')])
        set_enabled(c,'claude',True)
        restored=dashboard(f.settings)
        self.assertEqual(len(restored['autonomous']['accounts']),12)
        self.assertTrue(any(x['key']=='claude:low_full' for x in restored['autonomous']['accounts']))

    def test_both_disabled_empty_ai_surfaces_keep_rules_and_switches(self):
        f=self.f;c=f.m.store.conn
        for m in ('claude','codex'):set_enabled(c,m,False)
        d=dashboard(f.settings)
        self.assertEqual(d['autonomous']['accounts'],[])
        self.assertEqual(d['comparison']['models'],[])
        self.assertEqual(len(d['model_controls']),2)
        self.assertTrue(all(':' not in a['key'] for a in d['leaderboard']))
        self.assertEqual(d['scorecard']['views'],0)
