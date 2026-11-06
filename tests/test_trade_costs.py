"""Regression for the real holding/new-buy fee error and ledger attribution."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from monitor.trade_costs import cost_reference,position_comparison,sum_breakdowns
from monitor.autonomous import summaries
import test_autonomous as fixtures


class TradeCostTests(unittest.TestCase):
    def test_holding_and_new_buy_have_different_fee_baselines(self):
        value=position_comparison(50,850.15,.004,target=851.96,stop=830.59)
        target=value['scenarios']['target']
        self.assertAlmostEqual(target['holding_return_pct'],.2129,places=4)
        self.assertAlmostEqual(target['new_buy_return_pct'],-.1879,places=4)
        self.assertEqual(target['delta_vs_sell_now_cny'],90.14)
        self.assertEqual(value['position_liquidation_cny'],42337.47)
        # No price movement means no advantage to holding over selling now, not a second fee.
        same=position_comparison(1,1000,.004,target=1000)['scenarios']['target']
        self.assertEqual(same['holding_return_pct'],0.)
        self.assertEqual(same['new_buy_return_pct'],-.4)

    def test_spread_zero_fee_and_unknown_inputs(self):
        v=position_comparison(2,1000,0,3,target=1010)['scenarios']['target']
        self.assertEqual(v['delta_vs_sell_now_cny'],20.)
        self.assertAlmostEqual(v['holding_return_pct'],1.003,places=3)
        self.assertEqual(cost_reference(1000,.004,2)['net_sell_price_cny'],994.)
        for bad in (None,False,float('nan'),float('inf'),-1,0):
            self.assertIsNone(cost_reference(bad,.004))
        for bad in (None,False,float('nan'),-1,1):self.assertIsNone(cost_reference(1000,bad))
        self.assertIsNone(position_comparison(None,1000,.004))


class LedgerAttributionTests(unittest.TestCase):
    def setUp(self):
        market=patch('monitor.trading_hours.trading_session',return_value={'is_open':True})
        market.start();self.addCleanup(market.stop)
        self.f=fixtures.AutonomousTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.engine=self.f.m.portfolios

    def row(self,price):
        return next(a for a in summaries(self.f.store.conn,self.engine.settings,price) if a['key']=='codex:short_empty')

    def fill(self,side,grams,price):
        a=self.f.account();self.engine._fill(a,side,grams,fixtures.snapshot(price)['execution_quote'],'test')
        self.engine._save(a);self.f.store.conn.commit()

    def test_open_partial_close_and_changed_spread_reconcile_without_double_charging(self):
        self.engine.settings=replace(self.f.settings,spread_cny=1)
        self.fill('buy',10,1000)
        a=self.row(1000);b=a['pnl_breakdown']
        self.assertEqual(b['price_pnl_cny'],0);self.assertEqual(b['paid_fees_cny'],0)
        self.assertEqual(b['estimated_exit_fee_cny'],40);self.assertEqual(b['estimated_exit_spread_cny'],10)
        self.assertEqual(b['net_pnl_cny'],-50);self.assertEqual(a['unrealized_pnl'],-50)
        self.fill('sell',5,1010)
        self.engine.settings=replace(self.engine.settings,spread_cny=2)
        a=self.row(1010);b=a['pnl_breakdown']
        self.assertAlmostEqual(b['paid_spread_cny'],5)
        self.assertAlmostEqual(b['paid_fees_cny'],20.2)
        self.assertEqual(b['price_pnl_cny'],100)
        self.assertAlmostEqual(b['net_pnl_cny'],44.6)
        self.assertAlmostEqual(b['net_pnl_cny'],a['value']-a['budget'])
        missing=self.row(None)['pnl_breakdown'];self.assertIsNone(missing['net_pnl_cny'])
        self.assertAlmostEqual(missing['paid_fees_cny'],20.2)
        self.fill('sell',5,1010)
        a=self.row(None);b=a['pnl_breakdown']
        self.assertEqual(b['estimated_exit_fee_cny'],0)
        self.assertEqual(b['estimated_exit_spread_cny'],0)
        self.assertAlmostEqual(b['paid_spread_cny'],15)
        self.assertAlmostEqual(b['net_pnl_cny'],44.6)
        self.assertAlmostEqual(a['unrealized_pnl'],0)

    def test_model_input_uses_the_actual_protection_scenarios_without_changing_the_account(self):
        self.fill('buy',10,1000)
        c=self.f.store.conn;c.execute("UPDATE ai_portfolios SET target_price=1010,stop_price=950 WHERE key='codex:short_empty'");c.commit()
        before=self.f.account()
        value=self.engine.input('codex',fixtures.snapshot(1000)['execution_quote'])['accounts']['short_empty']
        self.assertEqual(value['position_comparison']['scenarios']['target']['delta_vs_sell_now_cny'],99.6)
        self.assertEqual(self.f.account(),before)
        self.assertNotIn('pnl_breakdown',value)  # Frontend attribution is not repeated in model input.

    def test_aggregate_unknown_mark_does_not_become_zero(self):
        self.fill('buy',10,1000)
        values=[a for a in summaries(self.f.store.conn,self.engine.settings,None) if a['model']=='codex']
        result=sum_breakdowns(values)
        self.assertIsNone(result['net_pnl_cny']);self.assertIsNone(result['price_pnl_cny'])
        self.assertEqual(result['paid_fees_cny'],0)
