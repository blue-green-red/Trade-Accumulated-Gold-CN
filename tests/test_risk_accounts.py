import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock
import test_autonomous as fixtures
from monitor.config import Settings
from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, RiskAccountResult
from monitor.autonomous import AutonomousPortfolio, summaries
from monitor.risk_profiles import MODE, MODES, reset_experiment
from monitor.history_tool import HistoryReader
from monitor.notification_control import visible_subscriptions
from monitor.web import dashboard


class RiskAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=replace(Settings(),ai_trading_mode=MODE,db_path=Path(self.tmp.name)/'risk.db',
            ai_analysts=('claude','codex'),ai_schedule_bj=(),ai_inflight_max_move_pct=100,
            clawx_api_key='',anthropic_api_key='',codex_cli='missing',claude_cli='missing')
        self.m=Monitor(self.settings,True)
        for model in self.settings.ai_analysts:self.m.store.set_state("ai_status:"+model,{"available":True})
        self.snap=fixtures.snapshot()
        self.m.portfolios.on_tick(self.snap)
        self.a=AIAnalyst(self.settings,self.m.store,'codex','codex')

    def tearDown(self):
        self.m.store.conn.close();self.tmp.cleanup()

    def decisions(self,**changes):
        d={k:fixtures.decisions()['short_empty'].copy() for k in MODES}
        for k in d:d[k].update(changes.get(k,{}))
        return d

    def test_six_account_round_isolated_models_and_dashboard(self):
        c=self.m.store.conn
        self.assertEqual(c.execute('select count(*) from ai_portfolios').fetchone()[0],12)
        stamps=c.execute('select count(distinct started_at),count(distinct initial_price) from ai_portfolios').fetchone()
        self.assertEqual(tuple(stamps),(1,1))
        context={'snapshot_at':self.snap['ts'].isoformat(),'xauusd':4000,
            'trading_accounts':self.m.portfolios.input('codex',self.snap['execution_quote'])}
        self.assertEqual(set(context['trading_accounts']['accounts']),set(MODES))
        policy=self.a.trading_system(context)
        self.assertNotIn('medium_full',policy);self.assertNotIn('不固定900',policy)
        self.assertIn('risk_exit_cny',policy);self.assertIn('加仓须包含原有仓位',policy)
        self.assertIn('null保留原设置',policy);self.assertIn('不自动设置止损',policy)
        self.assertNotIn('1000 字',self.a.research_system())
        self.assertNotIn('中长线持有',self.a.result_instructions())
        for k,v in context['trading_accounts']['accounts'].items():
            self.assertTrue(v['key'].startswith('codex:'))
            self.assertEqual(v['grams']>0,k.endswith('_full'))
            self.assertIn('risk_budget',v)
        data=fixtures.response().model_dump();data['account_decisions']=self.decisions()
        self.a._run_codex=Mock(return_value=('notes',[],RiskAccountResult.model_validate(data)))
        result=self.a.analyze(context,'risk-test')
        self.assertEqual(result['_trading_version'],'autonomous-risk-six-v1')
        self.m.portfolios.on_tick(self.snap)
        self.assertEqual(c.execute("select count(*) from ai_plans where account like 'codex:%'").fetchone()[0],6)
        web=dashboard(self.settings)
        self.assertEqual(len(web['autonomous']['accounts']),12)
        self.assertEqual(set(web['strategy_names']),{'hold','position','short',*(m+':'+k for m in ('claude','codex') for k in MODES)})
        for m in web['comparison']['models']:self.assertEqual(m['budget'],600000)
        topics=[r['key'] for r in web['notifications']['subscriptions'] if r['key'].startswith('codex:')]
        self.assertEqual(topics,['codex:low','codex:standard','codex:high','codex:analysis'])
        reader=HistoryReader(self.settings.db_path,'codex',self.snap['ts'].isoformat())
        self.assertNotIn('error',reader.call({'account':'low_full','kind':'fills'}))

    def test_risk_budget_fees_additions_and_drawdown(self):
        c=self.m.store.conn
        def account(k):return dict(c.execute('select * from ai_portfolios where key=?',('codex:'+k,)).fetchone())
        buy=fixtures.decisions()['short_empty'];buy.update(action='buy',amount_cny=20000,risk_exit_cny=950)
        # 5% price loss + fee => >1%, but below 2% and 4% of account equity.
        with self.assertRaisesRegex(ValueError,'风险预算'):self.m.portfolios._validate(account('low_empty'),buy,self.snap['execution_quote'])
        self.m.portfolios._validate(account('standard_empty'),buy,self.snap['execution_quote'])
        self.m.portfolios._validate(account('high_empty'),buy,self.snap['execution_quote'])
        buy.update(amount_cny=1000,risk_exit_cny=None)
        with self.assertRaisesRegex(ValueError,'风险退出'):self.m.portfolios._validate(account('low_empty'),buy,self.snap['execution_quote'])
        buy.update(risk_exit_cny=990)
        a=account('low_full');a['cash']=1000;a['grams']=99
        # Added amount alone fits budget, existing position risk makes total exceed it.
        with self.assertRaisesRegex(ValueError,'风险预算'):self.m.portfolios._validate(a,buy,self.snap['execution_quote'])
        a=account('low_empty');a['max_drawdown']=.05
        with self.assertRaisesRegex(ValueError,'回撤触线'):self.m.portfolios._validate(a,buy,self.snap['execution_quote'])
        sell=fixtures.decisions()['short_full'];sell.update(action='sell',grams=1)
        a['grams']=1
        self.m.portfolios._validate(a,sell,self.snap['execution_quote'])

    def test_budget_is_enforced_on_actual_fill_and_initialization_not_an_ai_trade(self):
        c=self.m.store.conn
        context={'snapshot_at':self.snap['ts'].isoformat(),'xauusd':4000,
            'trading_accounts':self.m.portfolios.input('codex',self.snap['execution_quote'])}
        d=self.decisions(low_empty={'action':'buy','amount_cny':20000,'risk_exit_cny':950},
                         standard_empty={'action':'buy','amount_cny':20000,'risk_exit_cny':950})
        data=fixtures.response().model_dump();data['account_decisions']=d
        self.a._run_codex=Mock(return_value=('notes',[],RiskAccountResult.model_validate(data)))
        self.a.analyze(context,'budget-fill')
        self.m.portfolios.on_tick(self.snap)
        low=c.execute("select grams from ai_portfolios where key='codex:low_empty'").fetchone()[0]
        standard=c.execute("select grams from ai_portfolios where key='codex:standard_empty'").fetchone()[0]
        self.assertEqual(low,0);self.assertEqual(standard,20)
        rejected=c.execute("select status,reason from ai_plans where account='codex:low_empty'").fetchone()
        self.assertEqual(rejected[0],'rejected');self.assertIn('风险预算',rejected[1])
        fills=c.execute("select count(*) from ai_portfolio_fills where account like 'codex:%' and reason not like '实验初始化%'").fetchone()[0]
        self.assertEqual(fills,1)
        budget=c.execute("select risk_exit_cny from (select json_extract(decision_json,'$.risk_exit_cny') risk_exit_cny from ai_plans where account='codex:standard_empty')").fetchone()[0]
        self.assertEqual(budget,950)

    def test_reset_removes_old_positions_once_preserves_market_and_controls(self):
        c=self.m.store.conn
        self.m.store.set_state('model_control:claude',{'enabled':False,'generation':4})
        self.m.store.set_state('market-test',{'value':1})
        # Simulate explicit first migration of an old experiment.
        c.execute("delete from state where key='risk_experiment:v1'");c.commit()
        self.assertTrue(reset_experiment(c))
        self.assertEqual(c.execute('select count(*) from ai_portfolios').fetchone()[0],0)
        self.assertEqual(c.execute('select count(*) from ai_portfolio_fills').fetchone()[0],0)
        self.assertEqual(self.m.store.get_state('market-test'),{'value':1})
        self.assertFalse(self.m.store.get_state('model_control:claude')['enabled'])
        p=AutonomousPortfolio(self.settings,self.m.store);p.on_tick(self.snap)
        self.assertEqual(c.execute('select count(*) from ai_portfolios where started_at is not null').fetchone()[0],12)
        before=[tuple(r) for r in c.execute('select * from ai_portfolios order by key')]
        self.assertFalse(reset_experiment(c))
        self.assertEqual(before,[tuple(r) for r in c.execute('select * from ai_portfolios order by key')])
