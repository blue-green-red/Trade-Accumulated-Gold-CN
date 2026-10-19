"""六账户自主交易离线验证，不调用外部模型或行情接口。"""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitor.config import Settings
from monitor.store import utc_now
from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, AutonomousResult, MarketView
from monitor.autonomous import LABELS, PERIODS
from monitor.controls import set_enabled
from monitor.web import dashboard


def snapshot(price=1000):
    now=utc_now()
    return {"ts":now,"market_open":True,"xauusd":price*4,"signals":{},"ratings":{},
        "execution_quote":{"source":"jd_latest_price_api","price":price,"quote_at":now.isoformat(),"fetched_at":now.isoformat(),"product_sku":"test"}}


def decisions(**overrides):
    rows={key:dict(horizon=PERIODS[key.split('_')[0]][1],action="hold",execution="market",amount_cny=None,grams=None,
        limit_price_cny=None,min_execution_price_cny=1,max_execution_price_cny=100000,stop_loss_cny=None,take_profit_cny=None,valid_for_minutes=120,reason="独立账户测试",invalidation="市场条件变化") for key in LABELS}
    for key,values in overrides.items(): rows[key].update(values)
    return rows


def response(**overrides):
    return AutonomousResult(bias_short="bearish",bias_long="neutral",confidence="low",p_up_3d=.1,p_down_3d=.7,
        p_up_3m=.3,p_down_3m=.3,hard_risk_event=False,hard_risk_note="",drivers=[],upcoming_events=[],summary="test",
        research_notes="test",sources=[],account_decisions=decisions(**overrides))


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=replace(Settings(),ai_trading_mode="six_account",db_path=Path(self.temp.name)/"test.db",ai_schedule_bj=(),ai_inflight_max_move_pct=100,
            ai_analysts=("claude","codex"),anthropic_api_key="",claude_cli="missing-claude",codex_cli="missing-codex",clawx_api_key="")
        self.m=Monitor(self.settings,True)
        self.store=self.m.store
        for name in self.settings.ai_analysts: self.store.set_state("ai_status:"+name,{"available":True})
        self.snap=snapshot()
        self.m.portfolios.on_tick(self.snap)
        self.a=AIAnalyst(self.settings,self.store,"codex","codex")
        self.m.analysts={"codex":self.a}

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def account(self,key="short_empty",model="codex"):
        return dict(self.store.conn.execute("SELECT * FROM ai_portfolios WHERE key=?",(model+":"+key,)).fetchone())

    def context(self):
        return {"xauusd":4000,"snapshot_at":utc_now().isoformat(),"trading_accounts":self.m.portfolios.input("codex",self.snap["execution_quote"])}

    def publish(self,context=None,**overrides):
        value=response(**overrides)
        self.a._run_codex=Mock(return_value=("notes",[],value))
        view=self.a.analyze(context or self.context(),"test",run_id="test-six")
        self.assertIsNotNone(view)
        return view

    def plan(self,key="short_empty"):
        return dict(self.store.conn.execute("SELECT * FROM ai_plans WHERE account=? ORDER BY id DESC LIMIT 1",("codex:"+key,)).fetchone())

    def test_initialization_twelve_accounts_is_atomic_and_not_repeated(self):
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM ai_portfolios").fetchone()[0],12)
        for mode in LABELS:
            a=self.account(mode)
            self.assertAlmostEqual(a['cash'],0 if mode.endswith('_full') else 100000)
            self.assertAlmostEqual(a['grams'],100 if mode.endswith('_full') else 0)
        count=self.store.conn.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0]
        self.m.portfolios.on_tick(snapshot())
        restarted=Monitor(self.settings,True)
        restarted.portfolios.on_tick(snapshot())
        restarted.store.conn.close()
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0],count)

    def test_direct_buy_despite_bearish_probabilities_once_and_no_cross_account_money(self):
        self.publish(short_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=950,take_profit_cny=1100))
        self.m.portfolios.on_tick(snapshot())
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.a._run_codex.call_count,1)
        self.assertAlmostEqual(self.account()['cash'],90000)
        self.assertAlmostEqual(self.account()['grams'],10)
        self.assertEqual(self.account('medium_empty')['cash'],100000)
        self.assertEqual(self.account(model='claude')['cash'],100000)
        count=self.store.conn.execute("SELECT COUNT(*) FROM ai_portfolio_fills WHERE account='codex:short_empty'").fetchone()[0]
        self.assertEqual(count,1)

    def test_one_rejection_does_not_block_other_valid_accounts(self):
        self.publish(short_empty=dict(action='buy',amount_cny=100001,stop_loss_cny=950),
            medium_empty=dict(action='buy',amount_cny=5000,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan()['status'],'rejected')
        self.assertEqual(self.account()['grams'],0)
        self.assertEqual(self.account('medium_empty')['grams'],5)

    def test_cannot_sell_more_than_holding_or_change_account_horizon(self):
        self.publish(short_full=dict(action='sell',grams=101),short_empty=dict(horizon='position'))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan('short_full')['status'],'rejected')
        self.assertEqual(self.plan()['status'],'rejected')
        self.assertEqual(self.account('short_full')['grams'],100)

    def test_limit_order_waits_then_fills_only_once(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan()['status'],'pending')
        self.assertEqual(self.account()['grams'],0)
        self.m.portfolios.on_tick(snapshot(985))
        self.m.portfolios.on_tick(snapshot(985))
        self.assertEqual(self.plan()['status'],'executed')
        self.assertAlmostEqual(self.account()['grams'],10000/985)

    def test_limit_price_below_stop_is_rejected_instead_of_buying(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        self.m.portfolios.on_tick(snapshot(930))
        self.assertEqual(self.account()['grams'],0)
        self.assertEqual(self.plan()['status'],'rejected')

    def test_old_account_revision_rejects_action_after_risk_fill(self):
        self.publish(short_full=dict(stop_loss_cny=900))
        self.m.portfolios.on_tick(snapshot())
        ctx=self.context()
        self.m.portfolios.on_tick(snapshot(870))
        self.publish(ctx,short_full=dict(action='sell',grams=20))
        self.m.portfolios.on_tick(snapshot(870))
        self.assertEqual(self.plan('short_full')['status'],'rejected')
        self.assertIn('账户已变化',self.plan('short_full')['reason'])
        self.assertEqual(self.account('short_full')['grams'],0)

    def test_disabled_model_keeps_protective_stop(self):
        self.publish(short_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        set_enabled(self.store.conn,'codex',False)
        self.m.portfolios.on_tick(snapshot(940))
        self.assertEqual(self.account()['grams'],0)
        self.assertAlmostEqual(self.account()['cash'],90000+10*940*.996)

    def test_pending_order_cancelled_after_disable_and_reenable(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        set_enabled(self.store.conn,'codex',False)
        set_enabled(self.store.conn,'codex',True)
        self.m.portfolios.on_tick(snapshot(985))
        self.assertEqual(self.plan()['status'],'cancelled')
        self.assertEqual(self.account()['grams'],0)

    def test_expiration_cancels_pending_but_does_not_force_sell_long_position(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990,stop_loss_cny=950,valid_for_minutes=2),
            long_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=900,valid_for_minutes=2))
        self.m.portfolios.on_tick(snapshot())
        with patch('monitor.autonomous.utc_now',return_value=utc_now()+timedelta(minutes=3)):
            self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan()['status'],'expired')
        self.assertEqual(self.account('long_empty')['grams'],10)

    def test_stale_input_or_missing_quote_never_executes(self):
        ctx=self.context();ctx['snapshot_at']=(utc_now()-timedelta(minutes=16)).isoformat()
        self.publish(ctx,short_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan()['status'],'rejected')
        self.publish(short_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=950))
        s=snapshot();s['execution_quote']['quote_at']=(utc_now()-timedelta(minutes=3)).isoformat()
        self.m.portfolios.on_tick(s)
        self.assertEqual(self.account()['grams'],0)

    def test_full_cycle_win_rate_combines_partial_sells_and_fees(self):
        self.publish(short_full=dict(action='sell',grams=50))
        self.m.portfolios.on_tick(snapshot(1100))
        self.publish(short_full=dict(action='sell',grams=50))
        self.m.portfolios.on_tick(snapshot(900))
        self.m.publish_for_web(snapshot(900),None)
        d=dashboard(self.settings)
        a=next(a for a in d['autonomous']['accounts'] if a['key']=='codex:short_full')
        self.assertEqual(a['trades'],1)
        self.assertEqual(a['win_rate'],0)
        self.assertAlmostEqual(a['realized_pnl'],-400)
        self.assertEqual(len(d['comparison']['models'][0]['accounts']),6)
        self.assertEqual(d['comparison']['models'][0]['budget'],600000)
        self.assertTrue(any(f['strategy']=='codex:short_full' for f in d['fills']))

    def test_api_returns_six_decisions_in_one_model_response(self):
        value=response()
        self.a._check_active=Mock();self.a._add_usage=Mock()
        self.a._create=Mock(return_value=SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',name='submit_portfolio_decisions',input=value.model_dump())]))
        _,_,parsed=self.a._run_api_autonomous(self.context(),{})
        self.assertEqual(len(parsed.account_decisions.model_dump()),6)
        self.assertEqual(self.a._create.call_count,1)
        sent=json.loads(self.a._create.call_args.kwargs['messages'][0]['content'])
        self.assertEqual(len(sent['trading_accounts']['accounts']),6)

    def test_missing_six_account_output_fails_closed(self):
        self.a._run_codex=Mock(return_value=('notes',[],MarketView.model_validate(response().model_dump())))
        self.assertIsNone(self.a.analyze(self.context(),'test'))
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM ai_plans').fetchone()[0],0)

    def test_ai_can_remove_take_profit_while_retaining_stop(self):
        self.publish(long_full=dict(take_profit_cny=1100,stop_loss_cny=950))
        self.m.portfolios.on_tick(snapshot())
        self.publish(long_full=dict(clear_take_profit=True))
        self.m.portfolios.on_tick(snapshot())
        self.assertIsNone(self.account('long_full')['target_price'])
        self.assertEqual(self.account('long_full')['stop_price'],950)
        self.m.portfolios.on_tick(snapshot(1101))
        self.assertEqual(self.account('long_full')['grams'],100)

    def test_full_initialization_has_no_program_stop_and_input_has_no_loss_floor(self):
        self.assertIsNone(self.account('short_full')['stop_price'])
        ctx=self.context()['trading_accounts']
        self.assertEqual(ctx['constraints']['protection_policy'],'model_discretion')
        self.assertFalse(ctx['constraints']['stop_loss_required'])
        self.assertNotIn('hard_stop_trigger_from_average_cost_pct',ctx['constraints'])
        self.assertNotIn('hard_stop_floor_cny',ctx['accounts']['short_full'])
        self.m.portfolios.on_tick(snapshot(750))
        self.assertEqual(self.account('short_full')['grams'],100)

    def test_buy_without_stop_or_target_does_not_force_close_at_twelve_percent(self):
        self.publish(short_empty=dict(action='buy',amount_cny=10000))
        self.m.portfolios.on_tick(snapshot())
        self.assertIsNone(self.account()['stop_price'])
        self.assertIsNone(self.account()['target_price'])
        set_enabled(self.store.conn,'codex',False)
        self.m.portfolios.on_tick(snapshot(700))
        self.assertEqual(self.account()['grams'],10)

    def test_model_can_set_and_widen_stop_beyond_previous_loss_floor(self):
        self.publish(short_empty=dict(action='buy',amount_cny=10000,stop_loss_cny=850))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.account()['stop_price'],850)
        self.publish(short_empty=dict(stop_loss_cny=600))
        self.m.portfolios.on_tick(snapshot())
        self.m.portfolios.on_tick(snapshot(650))
        self.assertEqual(self.account()['grams'],10)
        self.m.portfolios.on_tick(snapshot(590))
        self.assertEqual(self.account()['grams'],0)

    def test_clear_stop_retains_target_and_never_reinstates_program_stop(self):
        self.publish(long_full=dict(stop_loss_cny=950,take_profit_cny=1100))
        self.m.portfolios.on_tick(snapshot())
        self.publish(long_full=dict(clear_stop_loss=True))
        self.m.portfolios.on_tick(snapshot())
        self.assertIsNone(self.account('long_full')['stop_price'])
        self.assertEqual(self.account('long_full')['target_price'],1100)
        restarted=Monitor(self.settings,True)
        restarted.portfolios.on_tick(snapshot(850));restarted.store.conn.close()
        self.assertEqual(self.account('long_full')['grams'],100)
        set_enabled(self.store.conn,'codex',False)
        self.m.portfolios.on_tick(snapshot(1101))
        self.assertEqual(self.account('long_full')['grams'],0)

    def test_clear_and_set_stop_together_rejected_without_mutating_existing_protection(self):
        self.publish(long_full=dict(stop_loss_cny=900))
        self.m.portfolios.on_tick(snapshot())
        self.publish(long_full=dict(clear_stop_loss=True,stop_loss_cny=850))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan('long_full')['status'],'rejected')
        self.assertEqual(self.account('long_full')['stop_price'],900)

    def test_limit_buy_without_stop_can_fill(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990))
        self.m.portfolios.on_tick(snapshot())
        self.assertEqual(self.plan()['status'],'pending')
        self.m.portfolios.on_tick(snapshot(980))
        self.assertEqual(self.plan()['status'],'executed')
        self.assertAlmostEqual(self.account()['grams'],10000/980)
        self.assertIsNone(self.account()['stop_price'])

    def test_cancel_order_can_also_clear_existing_stop(self):
        self.publish(long_full=dict(stop_loss_cny=900,take_profit_cny=1100))
        self.m.portfolios.on_tick(snapshot())
        self.publish(long_full=dict(action='cancel',clear_stop_loss=True,clear_take_profit=True))
        self.m.portfolios.on_tick(snapshot())
        self.assertIsNone(self.account('long_full')['stop_price'])
        self.assertIsNone(self.account('long_full')['target_price'])
        self.assertEqual(self.plan('long_full')['status'],'executed')
        self.m.portfolios.on_tick(snapshot(800))
        self.assertEqual(self.account('long_full')['grams'],100)

    def test_generation_change_while_waiting_rejects_old_context_before_call(self):
        ctx=self.context()
        set_enabled(self.store.conn,'codex',False)
        set_enabled(self.store.conn,'codex',True)
        self.a._run_codex=Mock(side_effect=AssertionError('old context must not reach AI'))
        self.assertIsNone(self.a.analyze(ctx,'test'))
        self.a._run_codex.assert_not_called()

    def test_backend_unavailable_or_switch_off_does_not_initialize(self):
        settings=replace(self.settings,db_path=Path(self.temp.name)/'unavailable.db')
        m=Monitor(settings,True)
        try:
            m.portfolios.on_tick(snapshot())
            self.assertEqual(m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolios WHERE started_at IS NOT NULL').fetchone()[0],0)
            self.assertEqual(len(dashboard(settings)['autonomous']['accounts']),12)
            m.store.set_state('ai_status:codex',{'available':True})
            set_enabled(m.store.conn,'codex',False)
            m.portfolios.on_tick(snapshot())
            self.assertEqual(m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolios WHERE started_at IS NOT NULL').fetchone()[0],0)
            set_enabled(m.store.conn,'codex',True)
            m.portfolios.on_tick(snapshot())
            self.assertEqual(m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolios WHERE started_at IS NOT NULL').fetchone()[0],6)
        finally:m.store.conn.close()

    def test_serial_models_share_market_but_receive_only_own_six_accounts(self):
        b=AIAnalyst(self.settings,self.store,'codex','claude')
        calls=[]
        for name,a in (('claude',b),('codex',self.a)):
            def run(ctx,usage,name=name):
                calls.append((name,ctx))
                return 'notes',[],response()
            a._run_codex=run
        self.m.analysts={'claude':b,'codex':self.a}
        self.m._ai_worker(snapshot(),{}, {'claude':'test','codex':'test'})
        self.assertEqual([n for n,c in calls],['claude','codex'])
        for name,ctx in calls:
            self.assertEqual(len(ctx['trading_accounts']['accounts']),6)
            self.assertTrue(all(a['key'].startswith(name+':') for a in ctx['trading_accounts']['accounts'].values()))
        shared=[{k:v for k,v in c.items() if k!='trading_accounts'} for n,c in calls]
        self.assertEqual(shared[0],shared[1])


    def test_expiry_updates_without_quote_and_keeps_executed_decisions(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990,valid_for_minutes=1),
                     short_full=dict(stop_loss_cny=950,valid_for_minutes=1))
        self.m.portfolios.on_tick(snapshot())
        no_quote=snapshot();no_quote.pop('execution_quote')
        with patch('monitor.autonomous.utc_now',return_value=utc_now()+timedelta(minutes=2)):
            self.m.portfolios.on_tick(no_quote)
        self.assertEqual(self.plan()['status'],'expired')
        self.assertEqual(self.plan('short_full')['status'],'executed')
        self.assertEqual(self.account('short_full')['stop_price'],950)
        self.assertEqual(self.account('short_full')['grams'],100)

    def test_expiry_updates_during_closed_market(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990,valid_for_minutes=1))
        self.m.portfolios.on_tick(snapshot())
        closed=snapshot();closed['market_open']=False
        with patch('monitor.autonomous.utc_now',return_value=utc_now()+timedelta(minutes=2)):
            self.m.portfolios.on_tick(closed)
        self.assertEqual(self.plan()['status'],'expired')
        self.assertEqual(self.account()['cash'],100000)

    def test_monitor_expires_before_all_market_sources_fail(self):
        self.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990,valid_for_minutes=1))
        self.m.portfolios.on_tick(snapshot())
        self.m.market.refresh_fx_if_due=Mock()
        self.m.market.snapshot=Mock(return_value={'xauusd':None,'errors':['offline']})
        self.m.weekly_report_if_due=Mock(return_value=[])
        with patch('monitor.autonomous.utc_now',return_value=utc_now()+timedelta(minutes=2)):
            self.m.tick()
        self.assertEqual(self.plan()['status'],'expired')


if __name__=='__main__':unittest.main()
