"""Retiring simulation must preserve ledgers and remove it from actual model requests."""
from dataclasses import replace
from pathlib import Path
import json
import unittest
from monitor.config import Settings
from monitor.ai import AIAnalyst,STRUCTURE_SYSTEM,live_advice_prompt
from monitor.autonomous import enabled,keys
from monitor.controls import daily_keys,short_labels,set_enabled
from monitor.history_access import enabled as history_enabled
from monitor.jd_live_readonly import model_input
from monitor.notification_control import topics_for_mode
from monitor.page_data import validate_page
import test_live_proposals as fixtures


class LiveOnlyTests(unittest.TestCase):
    def setUp(self):
        self.settings=replace(Settings(),ai_trading_mode='live_only')

    def test_request_has_no_simulation_instructions_schema_or_history_tool(self):
        a=AIAnalyst.__new__(AIAnalyst);a.settings=self.settings
        context={'live_readonly_accounts':{'accounts':{'zheshang':{'status':'fresh'}}}}
        request=a.research_system()+STRUCTURE_SYSTEM+a.trading_system(context)+a.result_instructions()+live_advice_prompt(context)
        schema=a.result_type(context).model_json_schema()
        for forbidden in ('模拟','六户','六账户','trading_accounts','account_decisions','query_account_history'):
            self.assertNotIn(forbidden,request+json.dumps(schema,ensure_ascii=False))
        self.assertIn('public_content',schema['properties'])
        self.assertIn('live_account_advice',schema['properties'])
        self.assertFalse(history_enabled(a,{'trading_accounts':{'accounts':{}}}))
        self.assertEqual(a.trading_system({'trading_accounts':{'accounts':{'high_empty':{}}}}),'')
        self.assertNotIn('account_decisions',a.result_type({'trading_accounts':{'accounts':{'high_empty':{}}}}).model_fields)

    def test_execution_keys_subscriptions_and_routes_are_retired(self):
        self.assertFalse(enabled(self.settings));self.assertEqual(keys(self.settings),{})
        self.assertEqual(daily_keys(self.settings),());self.assertEqual(short_labels(self.settings),{})
        self.assertTrue(all(k=='jd:login' or k.split(':')[1]=='analysis' or ':live_' in k for k in topics_for_mode('live_only')))
        for page,view in (('compare','auto'),('strategies','simulation-list'),('strategies','simulation/codex:high_empty')):
            with self.assertRaises(ValueError):validate_page(page,view,self.settings)
        validate_page('strategies','live/zheshang/advice',self.settings)

    def test_last_trade_size_survives_waits_and_stays_model_scoped(self):
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        f.saved(f.value(action='buy',grams=2,price_condition='at_or_below',price_cny=850,suggested_orders=[]))
        trade=f.latest();self.assertEqual(trade['checks'],[])
        for _ in range(4):f.saved(f.value(action='wait',grams=None,price_condition='none',price_cny=None,suggested_orders=[]))
        before=f.s.conn.execute('SELECT COUNT(*) FROM jd_live_advice').fetchone()[0]
        prior=model_input(f.s.conn,fixtures.NOW)['accounts']['zheshang']['previous_advice']
        self.assertEqual(len(prior),1)
        baseline=prior[0]['last_trade_advice']
        self.assertEqual(baseline['id'],trade['id']);self.assertEqual(baseline['grams'],2)
        self.assertTrue(baseline['historical_only']);self.assertEqual(baseline['input_state']['total_grams'],f.data['total_grams'])
        self.assertIn('input_account_risk',baseline)
        self.assertEqual(f.s.conn.execute('SELECT COUNT(*) FROM jd_live_advice').fetchone()[0],before)
        set_enabled(f.s.conn,'codex',False);set_enabled(f.s.conn,'codex',True)
        self.assertEqual(model_input(f.s.conn,fixtures.NOW)['accounts']['zheshang']['previous_advice'],[])

    def test_old_simulation_records_are_hidden_without_deleting_history(self):
        from monitor.web import history_page
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        for key in ('autonomous:old','paper:old','acct:old','ai:codex:old'):
            f.s.conn.execute('INSERT INTO alerts(ts,key,title,body,delivered) VALUES(?,?,?,?,?)',
                (fixtures.NOW.isoformat(),key,'历史记录','历史内容',1))
        f.s.conn.commit();settings=replace(self.settings,db_path=Path(f.tmp.name)/'live.db')
        rows=history_page(settings,{'kind':['alerts']})['items']
        self.assertEqual([r['key'] for r in rows],['ai:codex:old'])
        for kind in ('fills','plans','cycles'):
            self.assertEqual(history_page(settings,{'kind':[kind]})['items'],[])
        self.assertEqual(f.s.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],4)

    def test_live_ticks_still_invalidate_inflight_advice_without_running_simulation(self):
        from unittest.mock import Mock,patch
        from monitor.price_guard import register,read
        import test_risk_accounts as risk
        import test_autonomous as old
        f=risk.RiskAccountTests();f.setUp();self.addCleanup(f.tearDown)
        m=f.m;m.settings=replace(f.settings,ai_trading_mode='live_only',ai_inflight_max_move_pct=.4)
        m.portfolios=None;m.accounts=None;m.short_books={};m.retired_short_books={};m.legacy_short=None
        for name in ('poll_jin10_if_due','refresh_macro_if_due','request_ai_after_data_release'):
            setattr(m,name,Mock())
        m.run_ai_if_due=Mock(return_value=[]);m.alerts.check=Mock(return_value=[])
        m.market=Mock();m.market.snapshot.return_value=old.snapshot(1010)
        c=m.store.conn
        attempt=c.execute("INSERT INTO ai_runs(analyst,started_at,trigger,run_id,status) VALUES('codex',?,'test','test','running')",(f.snap['ts'].isoformat(),)).lastrowid
        register(c,'codex',attempt,{'snapshot_at':f.snap['ts'].isoformat(),'xauusd':4000,'jd_accumulation_quote':{'price':1000}},m.settings);c.commit()
        before=c.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0]
        with patch('monitor.__main__.trading_session',return_value={'is_open':True}):m.tick()
        self.assertTrue(read(c,'ai_price_guard:codex').get('invalidated_at'))
        self.assertEqual(c.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0],before)
