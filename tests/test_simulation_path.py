"""Synthetic six-account path execution, independent of bank advisory operations."""
import unittest
from unittest.mock import Mock

from monitor.ai import RiskAccountResult
from monitor.autonomous import summaries
from monitor.ai_events import read
import test_risk_accounts as risk_fixtures
import test_autonomous as fixtures


def path(**changes):
    p={'target_position_pct':100,'boundary_source':'risk_exit','continuation':'原假设成立时可分批增加；触边界暂停旧单，按新快照重评。'}
    p.update(changes);return p


class SimulationPathTests(unittest.TestCase):
    def setUp(self):
        self.f=risk_fixtures.RiskAccountTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def account(self):return dict(self.f.m.store.conn.execute("SELECT * FROM ai_portfolios WHERE key='codex:high_empty'").fetchone())
    def publish(self,**changes):
        data=fixtures.response().model_dump()
        data['account_decisions']=self.f.decisions(high_empty=changes)
        answer=RiskAccountResult.model_validate(data)
        self.f.a._run_codex=Mock(return_value=('test',[],answer))
        snap=fixtures.snapshot()
        context={'snapshot_at':snap['ts'].isoformat(),'xauusd':4000,
            'trading_accounts':self.f.m.portfolios.input('codex',snap['execution_quote'])}
        self.f.a.analyze(context,'simulation-path')
        self.f.m.portfolios.on_tick(snap)
    def plan(self):return self.f.m.store.conn.execute("SELECT * FROM ai_plans WHERE account='codex:high_empty' ORDER BY id DESC LIMIT 1").fetchone()
    def row(self):return next(a for a in summaries(self.f.m.store.conn,self.f.settings,1000) if a['key']=='codex:high_empty')
    def test_crossing_reference_cancels_pending_buy_and_rebound_does_not_reactivate_it(self):
        self.publish(action='buy',execution='limit',amount_cny=10000,limit_price_cny=990,risk_exit_cny=980,position_path=path())
        self.assertEqual(self.plan()['status'],'pending')
        self.f.m.portfolios.on_tick(fixtures.snapshot(970))
        self.assertEqual(self.plan()['status'],'cancelled');self.assertEqual(self.account()['grams'],0)
        self.f.m.portfolios.on_tick(fixtures.snapshot(1000))
        self.assertEqual(self.account()['grams'],0);self.assertEqual(self.row()['plan']['position_path']['state'],'boundary_reached')
        events=[e for e in read(self.f.m.store.conn,'codex')['pending'] if e['id'].startswith('portfolio-path:')]
        self.assertEqual(len(events),1)
        self.publish(action='buy',execution='market',amount_cny=10000,risk_exit_cny=980,position_path=path())
        self.assertEqual(self.account()['grams'],10);self.assertNotEqual(self.row()['plan']['position_path']['state'],'boundary_reached')
    def test_add_keeps_actual_stop_on_full_new_position_and_protection_precedes_stale_buy(self):
        self.publish(action='buy',amount_cny=10000,risk_exit_cny=980,stop_loss_cny=980,position_path=path(boundary_source='stop_loss'))
        self.publish(action='buy',amount_cny=5000,risk_exit_cny=980,position_path=path(boundary_source='stop_loss'))
        self.assertEqual(self.account()['grams'],15)
        self.assertEqual(self.row()['plan']['position_path']['stop_coverage_grams'],15)
        self.publish(action='buy',execution='limit',amount_cny=1000,limit_price_cny=990,risk_exit_cny=980,position_path=path(boundary_source='stop_loss'))
        self.assertEqual(self.plan()['status'],'pending')
        self.f.m.portfolios.on_tick(fixtures.snapshot(975))
        self.assertEqual(self.account()['grams'],0);self.assertEqual(self.plan()['status'],'cancelled')
        fills=list(self.f.m.store.conn.execute("SELECT side,grams FROM ai_portfolio_fills WHERE account='codex:high_empty' ORDER BY id"))
        self.assertEqual([r[0] for r in fills],['buy','buy','sell']);self.assertEqual(fills[-1][1],15)
    def test_pending_buy_cannot_assume_its_clear_flag_has_already_removed_old_stop(self):
        self.publish(action='buy',amount_cny=10000,risk_exit_cny=980,stop_loss_cny=980,position_path=path(boundary_source='stop_loss'))
        self.publish(action='buy',execution='limit',amount_cny=1000,limit_price_cny=970,risk_exit_cny=960,
                     clear_stop_loss=True,position_path=path())
        self.assertEqual(self.plan()['status'],'rejected');self.assertEqual(self.account()['stop_price'],980)
    def test_reference_boundary_on_hold_requests_review_without_automatically_selling(self):
        self.publish(action='buy',amount_cny=10000,risk_exit_cny=980,position_path=path())
        self.publish(action='hold',risk_exit_cny=980,position_path=path())
        self.f.m.portfolios.on_tick(fixtures.snapshot(970));self.f.m.portfolios.on_tick(fixtures.snapshot(1000))
        self.assertEqual(self.account()['grams'],10);self.assertIsNone(self.account()['stop_price'])
        self.assertEqual(self.row()['plan']['position_path']['state'],'boundary_reached')
        events=[e for e in read(self.f.m.store.conn,'codex')['pending'] if e['id'].startswith('portfolio-path:')]
        self.assertEqual(len(events),1)
