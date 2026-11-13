import json
import sqlite3
import unittest
from datetime import datetime,timedelta,timezone

from monitor.participation_review import current,simulation,live,bank_cash
from monitor.public_ai import privacy_ok

NOW=datetime(2026,11,13,3,tzinfo=timezone.utc)


class ParticipationReviewTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE ai_views(ts TEXT,snapshot_at TEXT,view_json TEXT);
          CREATE TABLE ai_plans(id INTEGER PRIMARY KEY,account TEXT,view_id INTEGER,generation INTEGER,
             created_at TEXT,decision_json TEXT,status TEXT);
          CREATE TABLE jd_live_advice(id INTEGER PRIMARY KEY,bank TEXT,analyst TEXT,created_at TEXT,
             snapshot_revision TEXT,advice_json TEXT);''')
        self.a={'key':'codex:high_empty','model':'codex','start_mode':'high_empty','cash':20000,
                'grams':2,'revision':3,'plan':None,'risk_budget':{'buy_paused':False}}
        self.bank={'bank':'zheshang','revision':'synthetic-version','status':'fresh','total_grams':2,
            'cash_cny':20000,'sell_fee_rate':.004,
            'quote':{'status':'fresh','sell_price_cny':1100,'quoted_at':NOW.isoformat()},
            'pending_orders':{'status':'fresh','complete':True,'records':[]}}

    def quote(self,price=1100):return {'source':'jd_latest_price_api','price':price,'quote_at':NOW.isoformat(),'fetched_at':NOW.isoformat()}
    def sim(self,price=1100):return simulation(self.c,self.a,self.quote(price),NOW.isoformat(),.004,0)
    def plan(self,offset,price,action='hold',revision=3,generation=0,account='codex:high_empty',status='executed'):
        at=NOW+timedelta(minutes=offset)
        view={'_price_guard':{'jd':price},'_account_inputs':{'high_empty':{'revision':revision}}}
        v=self.c.execute('INSERT INTO ai_views VALUES(?,?,?)',(at.isoformat(),at.isoformat(),json.dumps(view))).lastrowid
        self.c.execute('INSERT INTO ai_plans(account,view_id,generation,created_at,decision_json,status) VALUES(?,?,?,?,?,?)',
            (account,v,generation,(at+timedelta(seconds=5)).isoformat(),json.dumps({'action':action}),status))

    def advice(self,offset,price,model='codex',bank='zheshang',revision='synthetic-version',**changes):
        at=NOW+timedelta(minutes=offset)
        d={'action':'hold','input_quote_status':'fresh','input_quote_cny':price,'input_quote_at':at.isoformat(),
           'input_state':{'total_grams':2,'cash_cny':20000,'pending_order_count':0},'checks':[],'suggested_orders':[]}
        d.update(changes)
        self.c.execute('INSERT INTO jd_live_advice(bank,analyst,created_at,snapshot_revision,advice_json) VALUES(?,?,?,?,?)',
            (bank,model,(at+timedelta(seconds=5)).isoformat(),revision,json.dumps(d)))

    def test_same_amplitude_cashflow_includes_fee_spread_and_cash_cap(self):
        v=current(20000,2,1000,.004,3)
        self.assertEqual(v['reference_amounts_cny'],[219.86,1099.3])
        self.assertAlmostEqual(v['up_delta_cny'][0],round(219.86*(1020*.996-3)/1000-219.86,2))
        self.assertAlmostEqual(v['down_delta_cny'][1],round(1099.3*(980*.996-3)/1000-1099.3,2))
        self.assertEqual(current(7,100,1000,.004)['reference_amounts_cny'],[7,7])
        self.assertEqual(current(1000,2,1000,.004,total_cash=20000)['reference_amounts_cny'],[219.92,1000])
        self.assertEqual(current(None,2,1000,.004)['status'],'data_unavailable')
        self.assertEqual(current(20000,2,float('nan'),.004)['status'],'data_unavailable')

    def test_holding_and_waiting_accumulate_without_refresh_reset_or_hindsight_peak(self):
        self.plan(-240,1000,'hold');self.plan(-120,1020,'wait')
        self.plan(60,9999);self.plan(-100,900,account='claude:high_empty')
        before=self.c.total_changes;v=self.sim()
        self.assertEqual(v['waiting']['rounds'],2)
        self.assertEqual(v['waiting']['reference_quote_cny'],1000)
        self.assertEqual(v['waiting']['reference_amount_cny'],219.92)
        self.assertEqual(v['waiting']['buy_hold_delta_cny'],21.02)
        self.assertEqual(self.sim()['waiting'],v['waiting'])
        self.assertEqual(self.c.total_changes,before)

    def test_losses_avoided_are_negative_counterfactual_not_missed_gain(self):
        self.plan(-60,1000)
        self.assertLess(self.sim(950)['waiting']['buy_hold_delta_cny'],0)
        flat=self.sim(1000)['waiting']
        self.assertAlmostEqual(flat['buy_hold_delta_cny'],-.88)
        self.assertEqual(flat['price_only_delta_cny'],0)
        self.assertAlmostEqual(flat['exit_cost_cny'],.88)
        self.assertEqual(flat['observed_minutes'],60)
        rise=self.sim()['waiting']
        self.assertAlmostEqual(rise['price_only_delta_cny']-rise['exit_cost_cny'],rise['buy_hold_delta_cny'],places=2)

    def test_actual_fill_revision_and_buy_or_invalid_plan_end_cohort(self):
        self.plan(-240,1000);self.a['revision']=4
        self.assertNotIn('waiting',self.sim())
        self.plan(-100,1020,revision=4);self.assertEqual(self.sim()['waiting']['rounds'],1)
        self.plan(-60,1030,action='buy',revision=4)
        self.assertNotIn('waiting',self.sim())
        self.plan(-30,1040,revision=4,status='rejected')
        self.assertNotIn('waiting',self.sim())

    def test_model_generation_pause_stale_quote_and_unknown_reservation(self):
        self.plan(-60,1000)
        self.c.execute('INSERT INTO state VALUES(?,?)',('model_control:codex',json.dumps({'enabled':True,'generation':1,'changed_at':None})))
        self.assertNotIn('waiting',self.sim())
        self.a['risk_budget']['buy_paused']=True
        self.assertEqual(self.sim()['status'],'buy_paused')
        self.a['risk_budget']['buy_paused']=False
        self.a['plan']={'status':'pending','decision':{'action':'buy','amount_cny':None}}
        self.assertEqual(self.sim()['status'],'data_unavailable')
        q=self.quote();q['quote_at']=(NOW-timedelta(minutes=10)).isoformat()
        self.assertEqual(simulation(self.c,self.a,q,NOW.isoformat(),.004,0)['status'],'data_unavailable')

    def test_bank_history_is_model_bank_and_account_version_scoped(self):
        self.advice(-240,1000);self.advice(-100,1020)
        self.advice(10,9999);self.advice(-90,900,model='claude');self.advice(-90,800,bank='minsheng')
        v=live(self.c,self.bank,'codex',NOW)
        self.assertEqual(v['waiting']['rounds'],2)
        self.assertEqual(v['waiting']['reference_quote_cny'],1000)
        self.assertEqual(v['waiting']['buy_hold_delta_cny'],21.02)
        self.bank['revision']='after-a-fill'
        self.assertNotIn('waiting',live(self.c,self.bank,'codex',NOW))

    def test_bank_no_unknown_or_partial_reservations_assumed_free(self):
        pending={'status':'partially_filled','action':'buy','grams':5,'price_cny':1000,'filled_grams':None}
        self.bank['pending_orders']['records']=[pending]
        self.assertIsNone(bank_cash(self.bank))
        pending['filled_grams']=2;self.assertEqual(bank_cash(self.bank),17000)
        self.bank['pending_orders']['complete']=False;self.assertIsNone(bank_cash(self.bank))
        self.bank['pending_orders']['complete']=True;self.bank['quote']['status']='historical'
        self.assertEqual(live(self.c,self.bank,'codex',NOW)['status'],'data_unavailable')

    def test_stale_analysis_or_recommended_buy_breaks_continuous_non_add(self):
        self.advice(-240,1000);self.advice(-120,1020,checks=['输入过期']);self.advice(-60,1040)
        self.assertEqual(live(self.c,self.bank,'codex',NOW)['waiting']['rounds'],1)
        self.advice(-30,1050,suggested_orders=[{'kind':'limit_buy','checks':[]}])
        self.assertNotIn('waiting',live(self.c,self.bank,'codex',NOW))

    def test_history_never_includes_an_advice_after_observed_quote_time(self):
        self.advice(-60,1000);self.advice(0,1020)
        self.bank['quote']['quoted_at']=(NOW-timedelta(seconds=30)).isoformat()
        self.assertEqual(live(self.c,self.bank,'codex',NOW)['waiting']['rounds'],1)

    def test_new_private_diagnostic_numbers_cannot_be_published(self):
        for field,value,shown in [('reference_amounts_cny',[7654.32],7654.32),('down_delta_cny',[-321.98],321.98),('buy_hold_delta_cny',-543.21,543.21),('price_only_delta_cny',-654.32,654.32),('exit_cost_cny',123.45,123.45)]:
            self.assertFalse(privacy_ok({'analysis':{'outlook':f'参考收益{shown}元'}},
                {'trading_accounts':{'accounts':{'synthetic':{'participation_review':{field:value}}}}}))


class LiveParticipationIntegrationTests(unittest.TestCase):
    def test_first_analysis_and_new_model_receive_current_diagnostics_without_fake_history(self):
        import test_live_proposals as fixtures
        from monitor.jd_live_readonly import model_input
        from monitor.controls import set_enabled
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        c=f.s.conn
        first=model_input(c,fixtures.NOW)['accounts']['zheshang']
        self.assertEqual(first['previous_advice'],[])
        self.assertEqual(first['participation_review']['status'],'ready')
        self.assertNotIn('waiting',first['participation_review'])
        f.saved(f.value(suggested_orders=[]))
        set_enabled(c,'claude',False);set_enabled(c,'claude',True)
        value=model_input(c,fixtures.NOW)['accounts']['zheshang']
        self.assertEqual(value['participation_review']['status'],'ready')
        self.assertNotIn('waiting',value['participation_review'])
        self.assertFalse(any(p['analyst']=='claude' for p in value['previous_advice']))

    def test_input_feedback_is_archived_and_reading_does_not_rewrite_advice(self):
        import test_live_proposals as fixtures
        from monitor.jd_live_readonly import model_input
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        f.saved(f.value(suggested_orders=[]));f.saved(f.value(suggested_orders=[]))
        c=f.s.conn;raw=c.execute('SELECT advice_json FROM jd_live_advice ORDER BY id DESC LIMIT 1').fetchone()[0]
        p=model_input(c,fixtures.NOW)['accounts']['zheshang']['previous_advice'][0]['strategy_review']['participation_review']
        self.assertEqual(p['waiting']['rounds'],2)
        f.saved(f.value(suggested_orders=[]))
        saved=json.loads(c.execute('SELECT advice_json FROM jd_live_advice ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(saved['input_strategy_review']['participation_review']['waiting']['rounds'],2)
        self.assertIn('input_participation_cash_cny',saved)
        self.assertEqual(c.execute('SELECT advice_json FROM jd_live_advice ORDER BY id DESC LIMIT 1 OFFSET 1').fetchone()[0],raw)

    def test_simulation_low_position_hold_gets_feedback_without_creating_trades(self):
        import test_simulation_path as fixtures
        import test_autonomous as market_fixtures
        f=fixtures.SimulationPathTests();f.setUp();self.addCleanup(f.f.tearDown)
        f.publish(action='buy',amount_cny=10000,risk_exit_cny=980)
        f.publish(action='hold')
        c=f.f.m.store.conn;plan=f.plan()
        row=c.execute('SELECT view_json FROM ai_views WHERE rowid=?',(plan['view_id'],)).fetchone()
        v=json.loads(row[0]);v['_price_guard']['jd']=1000
        c.execute('UPDATE ai_views SET view_json=? WHERE rowid=?',(json.dumps(v),plan['view_id']));c.commit()
        q=market_fixtures.snapshot(1030)['execution_quote']
        before=c.execute('SELECT count(*) FROM ai_portfolio_fills').fetchone()[0]
        account=f.f.m.portfolios.input('codex',q)['accounts']['high_empty']
        self.assertGreater(account['grams'],0)
        self.assertEqual(account['participation_review']['status'],'ready')
        self.assertEqual(account['participation_review']['waiting']['rounds'],1)
        self.assertGreater(account['participation_review']['waiting']['buy_hold_delta_cny'],0)
        self.assertEqual(c.execute('SELECT count(*) FROM ai_portfolio_fills').fetchone()[0],before)
