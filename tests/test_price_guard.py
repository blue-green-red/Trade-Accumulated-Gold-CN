"""Fast-market execution checks using isolated accounts and mocked model calls."""
import unittest
from dataclasses import replace
from unittest.mock import Mock
from pydantic import ValidationError
import test_autonomous as fixtures
from monitor.autonomous import AccountDecision
from monitor.ai_events import read,enqueue
from monitor.price_guard import read as state_read


class PriceGuardTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AutonomousTests();self.f.setUp()
        self.f.settings=replace(self.f.settings,ai_inflight_max_move_pct=.4)
        self.f.m.portfolios.settings=self.f.settings;self.f.a.settings=self.f.settings
    def tearDown(self):self.f.tearDown()

    def test_buy_sell_require_ordered_finite_range_and_feasible_limit(self):
        row=fixtures.decisions(short_empty=dict(action='buy',amount_cny=1000))['short_empty']
        for change in ({'min_execution_price_cny':None},{'max_execution_price_cny':None},
                       {'min_execution_price_cny':1001,'max_execution_price_cny':1000},
                       {'max_execution_price_cny':float('inf')},
                       {'execution':'limit','limit_price_cny':990,'min_execution_price_cny':995}):
            with self.subTest(change=change),self.assertRaises(ValidationError):AccountDecision.model_validate({**row,**change})

    def test_out_of_range_market_buy_preserves_previous_pending_order(self):
        f=self.f
        f.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990))
        f.m.portfolios.on_tick(fixtures.snapshot())
        old=f.plan()['id']
        f.publish(short_empty=dict(action='buy',amount_cny=1000,min_execution_price_cny=999,max_execution_price_cny=1001))
        f.m.portfolios.on_tick(fixtures.snapshot(1003))
        self.assertEqual(f.account()['cash'],100000)
        self.assertEqual(f.plan()['status'],'rejected')
        self.assertEqual(f.store.conn.execute('SELECT status FROM ai_plans WHERE id=?',(old,)).fetchone()[0],'pending')
        self.assertIsNotNone(f.a.due())

    def test_market_sell_outside_range_cannot_execute(self):
        f=self.f
        f.publish(short_full=dict(action='sell',grams=10,min_execution_price_cny=999,max_execution_price_cny=1001))
        f.m.portfolios.on_tick(fixtures.snapshot(998))
        self.assertEqual(f.account('short_full')['grams'],100)
        self.assertEqual(f.plan('short_full')['status'],'rejected')

    def test_limit_waits_then_gap_below_range_rejects_without_fill(self):
        f=self.f
        f.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990,
                                   min_execution_price_cny=988,max_execution_price_cny=990))
        f.m.portfolios.on_tick(fixtures.snapshot())
        self.assertEqual(f.plan()['status'],'pending')
        f.m.portfolios.on_tick(fixtures.snapshot(985))
        self.assertEqual(f.plan()['status'],'rejected')
        self.assertEqual(f.account()['cash'],100000)
        self.assertTrue(any(e['id'].startswith('price-range:') for e in read(f.store.conn,'codex')['pending']))

    def test_limit_range_boundaries_fill_at_latest_quote_once(self):
        f=self.f
        f.publish(short_empty=dict(action='buy',execution='limit',amount_cny=990,limit_price_cny=990,
                                   min_execution_price_cny=988,max_execution_price_cny=990))
        f.m.portfolios.on_tick(fixtures.snapshot())
        f.m.portfolios.on_tick(fixtures.snapshot(990));f.m.portfolios.on_tick(fixtures.snapshot(990))
        self.assertEqual(f.account()['grams'],1)
        self.assertEqual(f.plan()['status'],'executed')

    def test_inflight_move_is_sticky_deduplicated_and_does_not_ack_old_events(self):
        f=self.f
        enqueue(f.store.conn,'codex','news',0,'news:test')
        ctx=f.context();ctx['event_coverage']={'codex':['news:test']}
        def model(*args):
            f.m.portfolios.on_tick(fixtures.snapshot(1004))
            f.m.portfolios.on_tick(fixtures.snapshot(1000))
            return 'notes',[],fixtures.response(short_empty=dict(action='buy',amount_cny=1000))
        f.a._run_codex=Mock(side_effect=model)
        view=f.a.analyze(ctx,'test',run_id='fast-market')
        self.assertTrue(view['_price_guard']['invalidated_at'])
        f.m.portfolios.on_tick(fixtures.snapshot())
        self.assertEqual(f.account()['cash'],100000)
        self.assertEqual(f.plan()['status'],'rejected')
        events=read(f.store.conn,'codex')['pending']
        self.assertEqual(sum(e['id'].startswith('price-guard:') for e in events),1)
        self.assertTrue(any(e['id']=='news:test' for e in events))
        self.assertIsNotNone(f.a.due())
        self.assertEqual(state_read(f.store.conn,'ai_price_guard:claude'),{})

    def test_small_inflight_move_does_not_invalidate(self):
        f=self.f
        def model(*args):
            f.m.portfolios.on_tick(fixtures.snapshot(1003))
            return 'notes',[],fixtures.response(short_empty=dict(action='buy',amount_cny=1000,min_execution_price_cny=999,max_execution_price_cny=1003))
        f.a._run_codex=Mock(side_effect=model)
        view=f.a.analyze(f.context(),'test',run_id='small-move')
        self.assertFalse(view['_price_guard'].get('invalidated_at'))
        f.m.portfolios.on_tick(fixtures.snapshot(1003))
        self.assertAlmostEqual(f.account()['grams'],1000/1003)

    def test_xau_move_invalidates_even_if_jd_quote_is_unchanged(self):
        f=self.f
        def model(*args):
            snap=fixtures.snapshot();snap['xauusd']=4020
            f.m.portfolios.on_tick(snap)
            return 'notes',[],fixtures.response(short_empty=dict(action='buy',amount_cny=1000))
        f.a._run_codex=Mock(side_effect=model)
        view=f.a.analyze(f.context(),'test',run_id='xau-move')
        self.assertEqual(view['_price_guard']['source'],'xauusd')

    def test_existing_stop_executes_while_analysis_is_invalidated(self):
        f=self.f
        f.publish(short_full=dict(stop_loss_cny=950));f.m.portfolios.on_tick(fixtures.snapshot())
        def model(*args):
            f.m.portfolios.on_tick(fixtures.snapshot(940))
            return 'notes',[],fixtures.response(short_full=dict(clear_stop_loss=True))
        f.a._run_codex=Mock(side_effect=model)
        view=f.a.analyze(f.context(),'test',run_id='protect')
        self.assertTrue(view['_price_guard']['invalidated_at'])
        self.assertEqual(f.account('short_full')['grams'],0)

    def test_old_pending_trade_without_range_is_rejected(self):
        f=self.f
        f.publish(short_empty=dict(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990))
        f.m.portfolios.on_tick(fixtures.snapshot())
        import json
        row=f.plan();d=json.loads(row['decision_json']);d.pop('min_execution_price_cny');d.pop('max_execution_price_cny')
        f.store.conn.execute('UPDATE ai_plans SET decision_json=? WHERE id=?',(json.dumps(d),row['id']));f.store.conn.commit()
        f.m.portfolios.on_tick(fixtures.snapshot())
        self.assertEqual(f.plan()['status'],'rejected')
        self.assertEqual(f.account()['cash'],100000)

    def test_move_after_return_before_accept_is_also_rejected(self):
        f=self.f
        f.publish(short_empty=dict(action='buy',amount_cny=1000))
        f.m.portfolios.on_tick(fixtures.snapshot(1005))
        self.assertEqual(f.plan()['status'],'rejected')
        self.assertEqual(f.account()['cash'],100000)
        self.assertTrue(any(e['id'].startswith('price-guard:') for e in read(f.store.conn,'codex')['pending']))

    def test_stale_jd_price_does_not_falsely_invalidate(self):
        f=self.f
        from datetime import timedelta
        from monitor.store import utc_now
        def model(*args):
            snap=fixtures.snapshot();snap['execution_quote']['price']=1100
            snap['execution_quote']['quote_at']=(utc_now()-timedelta(minutes=3)).isoformat()
            f.m.portfolios.on_tick(snap)
            return 'notes',[],fixtures.response()
        f.a._run_codex=Mock(side_effect=model)
        view=f.a.analyze(f.context(),'test',run_id='stale-quote')
        self.assertFalse(view['_price_guard'].get('invalidated_at'))
