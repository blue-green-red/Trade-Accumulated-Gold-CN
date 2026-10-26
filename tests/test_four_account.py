import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import test_autonomous as fixtures
from monitor.config import Settings
from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, FourAccountResult
from monitor.autonomous import AutonomousPortfolio, FourPortfolioDecisions
from monitor.web import dashboard


class FourAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=replace(Settings(),ai_trading_mode='four_account',db_path=Path(self.tmp.name)/'test.db',
            ai_analysts=('claude','codex'),ai_schedule_bj=(),ai_inflight_max_move_pct=100,clawx_api_key='',
            anthropic_api_key='',codex_cli='missing-codex',claude_cli='missing-claude')
        self.m=Monitor(self.settings,True)
        for model in self.settings.ai_analysts:self.m.store.set_state('ai_status:'+model,{'available':True})
        self.snap=fixtures.snapshot();self.m.portfolios.on_tick(self.snap)
        self.a=AIAnalyst(self.settings,self.m.store,'codex','codex')

    def tearDown(self):
        self.m.store.conn.close();self.tmp.cleanup()

    def context(self):
        return {'snapshot_at':self.snap['ts'].isoformat(),'xauusd':4000,
                'trading_accounts':self.m.portfolios.input('codex',self.snap['execution_quote'])}

    def value(self):
        d=fixtures.response().model_dump();d['account_decisions']={k:v for k,v in d['account_decisions'].items() if not k.startswith('long_')}
        return FourAccountResult.model_validate(d)

    def test_new_install_four_accounts_schema_and_single_round(self):
        self.assertEqual(self.m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolios').fetchone()[0],8)
        self.assertEqual(len(self.context()['trading_accounts']['accounts']),4)
        self.assertNotIn('long_full',self.a.result_type(self.context()).model_json_schema()['$defs']['FourPortfolioDecisions']['properties'])
        self.assertNotIn('long_full/long_empty',self.a.trading_system(self.context()))
        policy=self.a.trading_system(self.context())
        self.assertIn('入场与复查条件必须能由已提供且有效的字段',policy)
        self.assertIn('京东有效报价用于实际入场',policy)
        self.assertIn('数据不足不自动等于可以买或必须不买',policy)
        self.a._run_codex=Mock(return_value=('notes',[],self.value()))
        result=self.a.analyze(self.context(),'test')
        self.assertEqual(len(result['account_decisions']),4)
        self.assertEqual(result['_trading_version'],'autonomous-four-v1')
        self.m.portfolios.on_tick(self.snap)
        self.assertEqual(self.m.store.conn.execute("SELECT COUNT(*) FROM ai_plans WHERE account LIKE 'codex:%'").fetchone()[0],4)
        with self.assertRaises(ValueError):FourPortfolioDecisions.model_validate(fixtures.response().model_dump()['account_decisions'])

    def legacy(self):
        old=AutonomousPortfolio(replace(self.settings,ai_trading_mode='six_account'),self.m.store)
        old.on_tick(self.snap)
        c=self.m.store.conn
        c.execute("UPDATE ai_portfolios SET stop_price=950 WHERE start_mode='long_full'")
        d=fixtures.decisions()['long_empty'];d.update(action='buy',amount_cny=1000,execution='limit',limit_price_cny=900)
        c.execute("INSERT INTO ai_plans(account,view_id,generation,created_at,expires_at,decision_json,status) VALUES('codex:long_empty',999,0,?,?,?,'pending')",
                  (self.snap['ts'].isoformat(),(self.snap['ts']+fixtures.timedelta(hours=1)).isoformat(),json.dumps(d)))
        c.commit()
        self.m.portfolios=AutonomousPortfolio(self.settings,self.m.store)

    def test_removal_deletes_long_ledgers_and_never_recreates_accounts(self):
        self.legacy();c=self.m.store.conn
        for table,column in (('ai_portfolios','key'),('ai_plans','account'),('ai_portfolio_fills','account'),('ai_portfolio_equity','account'),('ai_portfolio_cycles','account')):
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE '%:long_%'").fetchone()[0],0)
        self.m.portfolios.on_tick(fixtures.snapshot(940))
        self.assertEqual(c.execute('SELECT COUNT(*) FROM ai_portfolios').fetchone()[0],8)
        self.assertEqual(len(self.context()['trading_accounts']['accounts']),4)

    def test_dashboard_only_exposes_retained_accounts_and_budget(self):
        self.legacy();d=dashboard(self.settings)
        self.assertEqual(len(d['autonomous']['accounts']),8)
        self.assertTrue(all(m['budget']==400000 for m in d['comparison']['models']))
        self.assertTrue(all(len(m['accounts'])==4 for m in d['comparison']['models']))
        self.assertTrue(all(':long_' not in s['key'] for s in d['equity']['series']))
        self.assertNotIn('codex:long_full',d['strategy_names'])
        self.assertNotIn('codex:long',[r['key'] for r in d['notifications']['subscriptions']])
        self.assertFalse(any(':long_' in p['account'] for p in d['autonomous']['plans']))

    def test_api_submits_four_in_one_response(self):
        value=self.value();self.a._check_active=Mock();self.a._add_usage=Mock()
        self.a._create=Mock(return_value=SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',name='submit_portfolio_decisions',input=value.model_dump())]))
        _,_,parsed=self.a._run_api_autonomous(self.context(),{})
        self.assertEqual(len(parsed.account_decisions.model_dump()),4)
        schema=self.a._create.call_args.kwargs['tools'][0]['input_schema']
        self.assertEqual(len(schema['$defs']['FourPortfolioDecisions']['properties']),4)

    def test_removed_long_cycles_do_not_change_active_win_rate(self):
        self.legacy();self.m.portfolios.on_tick(fixtures.snapshot(940))
        d=dashboard(self.settings)
        self.assertTrue(all(m['trades']==0 for m in d['comparison']['models']))

    def test_embedded_long_data_is_removed_without_touching_short_history(self):
        from monitor.retire_long import remove_long_accounts
        c=self.m.store.conn
        value={'account_decisions':{'short_empty':{'action':'wait'},'long_empty':{'action':'buy'}},
               '_account_inputs':{'short_empty':{'cash':100000},'long_empty':{'cash':20000}}}
        c.execute("INSERT INTO ai_views(ts,model,analyst,view_json) VALUES(?,?,?,?)",(self.snap['ts'].isoformat(),'test','codex',json.dumps(value)))
        self.m.store.set_state('test-embedded',{'accounts':{'short_full':{'grams':100},'long_full':{'grams':100}}})
        remove_long_accounts(c)
        view=json.loads(c.execute("SELECT view_json FROM ai_views ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(list(view['account_decisions']),['short_empty'])
        self.assertEqual(self.m.store.get_state('test-embedded'),{'accounts':{'short_full':{'grams':100}}})
        count=c.execute("SELECT COUNT(*) FROM ai_portfolio_fills WHERE account LIKE 'codex:short_%'").fetchone()[0]
        remove_long_accounts(c)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ai_portfolio_fills WHERE account LIKE 'codex:short_%'").fetchone()[0],count)
