"""Synthetic staged paths: boundary precedence, real-order reservations, and coverage after adds."""
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
import unittest

from monitor.jd_live_advice import LiveAdvice
from monitor.jd_live_path import PositionPath,format_path
from monitor.jd_live_readonly import model_input,accounts
from monitor.jd_live_semantics import notification_parts,blocking_checks
from monitor.public_ai import privacy_ok
from test_live_entry import review
from test_live_risk import scenario
import test_live_proposals as fixtures


def path(**changes):
    v={'target_position_pct':100,'boundary_scenario_index':0,'exit_scope':'tranche',
       'continuation':'合成路径：原假设仍有效时独立比较追加；本批到边界退出后先核对成交和旧单，再用新快照重评，不机械续买。'}
    v.update(changes);return v


class LivePathTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveProposalTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.data.update(total_grams=10,sellable_grams=10,frozen_grams=0,average_cost_cny=860)
        self.f.snapshot()
    def value(self,**changes):
        stop=self.f.proposal(kind='take_profit_stop_loss',grams=10,price_cny=900,loss_price_cny=820)
        v=self.f.value(position_path=path(),downside_scenarios=[scenario(820,'risk_limit','sell')],
            suggested_orders=[stop],limit_buy_reviews=[],review_conditions=[])
        v.update(changes);return v
    def saved(self,**changes):
        self.f.saved(self.value(**changes));return self.f.latest()
    def candidate(self,entry=830):return review(entry_price_cny=entry,target_price_cny=900,risk_exit_cny=800,grams=5)
    def buy_order(self,r):return self.f.proposal(kind='limit_buy',price_cny=r['entry_price_cny'],grams=r['grams'],expires_at=r['expires_at'])
    def pending_buy(self,entry=810):
        return {**self.buy_order(self.candidate(entry)),'order_key':'b'*24,'action':'buy','status':'pending','filled_grams':None}

    def test_path_reuses_boundary_and_installs_one_existing_review_watcher_without_forcing_adds(self):
        v=self.saved()
        self.assertEqual(v['schema_version'],6);self.assertEqual(v['checks'],[]);self.assertEqual(v['action'],'hold')
        self.assertEqual(v['position_path']['boundary_role'],'risk_limit')
        self.assertEqual(len(v['review_conditions']),1);self.assertEqual(v['review_conditions'][0]['price_cny'],820)
        self.assertEqual(v['position_path']['coverage']['confirmed_protected_grams'],0)
        self.assertEqual(v['position_path']['coverage']['additional_buy_grams'],0)
        self.assertIn('目标黄金仓位100%',format_path(v))
        self.assertIn('非本轮追加指令',format_path(v))
        self.assertEqual(self.f.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)

    def test_actual_add_above_boundary_is_allowed_but_protection_does_not_expand_to_unfilled_buys(self):
        v=self.saved(action='buy',grams=5,price_condition='reference',price_cny=850)
        self.assertEqual(v['checks'],[])
        c=v['position_path']['coverage']
        self.assertEqual(c['after_all_buys_grams'],15);self.assertEqual(c['suggested_protected_grams'],10)
        self.assertEqual(c['uncovered_after_buys_grams'],5)
        self.assertIn('保护不自动扩量',format_path(v))

    def test_lower_buy_within_valid_path_is_allowed_with_gap_ordering_note(self):
        r=self.candidate();v=self.saved(limit_buy_reviews=[r],suggested_orders=[self.value()['suggested_orders'][0],self.buy_order(r)])
        self.assertEqual(v['checks'],[]);self.assertIn('跳价同时触发',v['position_path']['execution_note'])
        self.assertEqual(v['position_path']['coverage']['after_all_buys_grams'],15)

    def test_buy_at_or_below_exit_is_not_actionable_while_independent_protection_remains_visible(self):
        r=self.candidate(810)
        v=self.saved(limit_buy_reviews=[r],suggested_orders=[self.value()['suggested_orders'][0],self.buy_order(r)])
        self.assertTrue(v['suggested_orders'][1]['checks']);self.assertTrue(v['limit_buy_reviews'][0]['checks'])
        self.assertEqual(blocking_checks(v),[])
        safe=notification_parts(v);self.assertEqual([o['kind'] for o in safe['suggested_orders']],['take_profit_stop_loss'])
        v=self.saved(action='buy',grams=5,price_condition='at_or_below',price_cny=810)
        self.assertIsNone(notification_parts(v));self.assertTrue(blocking_checks(v))

    def test_bad_boundary_or_unexplained_protection_change_blocks_new_buy_not_safe_standalone_protection(self):
        for p in (path(boundary_scenario_index=1),path(boundary_scenario_index=None)):
            v=self.saved(action='buy',grams=1,price_cny=850,position_path=p)
            self.assertIsNone(notification_parts(v))
        v=self.saved(position_path=path(boundary_scenario_index=1))
        self.assertTrue(v['position_path']['checks']);self.assertIsNotNone(notification_parts(v))

    def test_cancel_proposal_does_not_confirm_old_buy_is_gone_or_authorize_new_adds(self):
        old=self.pending_buy();self.f.data['pending_orders'].update(records=[old],total_count=1)
        self.f.data['pending_order_count']=1;self.f.snapshot()
        management=[{'order_key':old['order_key'],'action':'cancel','reason':'旧单进入边界，先人工撤单。'}]
        v=self.saved(order_management=management,action='buy',grams=1,price_cny=850)
        self.assertTrue(v['position_path']['manual_resolution_required']);self.assertIsNone(notification_parts(v))
        r=self.candidate();v=self.saved(order_management=management,limit_buy_reviews=[r],
            suggested_orders=[self.value()['suggested_orders'][0],self.buy_order(r)])
        safe=notification_parts(v);self.assertEqual([o['kind'] for o in safe['suggested_orders']],['take_profit_stop_loss'])
        self.assertEqual(safe['order_management'][0]['action'],'cancel')

    def test_boundary_review_deduplicates_and_reaching_it_queues_only_existing_review_event(self):
        from monitor.jd_live_review import request_reviews
        from monitor.ai_events import read
        from monitor.config import Settings
        from dataclasses import replace
        from unittest.mock import patch
        condition={'condition':'at_or_below','price_cny':820,'quote_side':'sell','order_key':None,'reason':'同一边界。'}
        self.f.s.set_state('ai_status:codex',{'available':True})
        v=self.saved(review_conditions=[condition])
        self.assertEqual(len(v['review_conditions']),1)
        self.f.data['quote'].update(buy_price_cny=819,sell_price_cny=819,
            quoted_at=(fixtures.NOW+timedelta(seconds=1)).isoformat());self.f.snapshot()
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':True}):
            request_reviews(self.f.s,fixtures.NOW+timedelta(seconds=1),settings=replace(Settings(),db_path=Path(self.f.tmp.name)/'live.db',ai_short_review_minutes=-1))
        events=[e for e in read(self.f.s.conn,'codex')['pending'] if e['id'].startswith('live-price:')]
        self.assertEqual(len(events),1)
        paused=accounts(self.f.s.conn,fixtures.NOW+timedelta(seconds=1))[0]['advice'][0]
        self.assertEqual(paused['position_path']['state'],'boundary_reached')
        self.assertTrue(paused['suggested_orders'][0]['checks'])
        self.assertEqual(self.f.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)

    def test_boundary_pauses_an_existing_buy_but_still_queues_review_and_keeps_the_archive_unchanged(self):
        from monitor.jd_live_review import request_reviews
        from monitor.ai_events import read
        from monitor.config import Settings
        from dataclasses import replace
        from unittest.mock import patch
        import json
        self.f.s.set_state('ai_status:codex',{'available':True})
        v=self.saved(action='buy',grams=5,price_cny=850)
        stored=self.f.s.conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=?',(v['id'],)).fetchone()[0]
        self.f.data['quote'].update(buy_price_cny=819,sell_price_cny=819,
            quoted_at=(fixtures.NOW+timedelta(seconds=1)).isoformat());self.f.snapshot()
        now=fixtures.NOW+timedelta(seconds=1)
        live=accounts(self.f.s.conn,now)[0]['advice'][0]
        self.assertEqual(live['action_status'],'review_required')
        self.assertIsNone(notification_parts(live))
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':True}):
            request_reviews(self.f.s,now,settings=replace(Settings(),db_path=Path(self.f.tmp.name)/'live.db',ai_short_review_minutes=-1))
            request_reviews(self.f.s,now,settings=replace(Settings(),db_path=Path(self.f.tmp.name)/'live.db',ai_short_review_minutes=-1))
        events=[e for e in read(self.f.s.conn,'codex')['pending'] if e['id'].startswith('live-price:')]
        self.assertEqual(len(events),1)
        self.assertEqual(self.f.s.conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=?',(v['id'],)).fetchone()[0],stored)
        self.f.data['quote'].update(buy_price_cny=830,sell_price_cny=830,quoted_at=(fixtures.NOW+timedelta(seconds=2)).isoformat());self.f.snapshot()
        rebound=accounts(self.f.s.conn,fixtures.NOW+timedelta(seconds=2))[0]['advice'][0]
        self.assertEqual(rebound['position_path']['state'],'boundary_reached')
        self.assertEqual(rebound['action_status'],'review_required')
        self.assertIsNotNone(rebound['position_path']['boundary_triggered_at'])
        renewed=self.saved(action='buy',grams=5,price_cny=830)
        self.assertEqual(renewed['position_path']['state'],'current')
        self.assertEqual(renewed['action_status'],'current')

    def test_notice_shows_the_path_and_scope_but_drops_it_when_other_parts_are_unsafe(self):
        from monitor.jd_live_notice import body
        v=self.saved()
        a=accounts(self.f.s.conn,fixtures.NOW)[0]
        text=body(a,v,notification_parts(v))
        self.assertIn('建仓路径：条件成立时目标黄金仓位100%，非本轮追加指令',text)
        self.assertIn('当前这批退出',text);self.assertIn('刷新账户再复评',text)
        bad=self.candidate(810)
        v=self.saved(limit_buy_reviews=[bad],suggested_orders=[self.value()['suggested_orders'][0],self.buy_order(bad)])
        text=body(a,v,notification_parts(v))
        self.assertIn('已通过独立校验',text);self.assertNotIn('建仓路径：',text)

    def test_queued_notice_is_cancelled_if_its_buy_path_crosses_boundary_before_retry(self):
        from monitor.notification_control import set_subscription
        from monitor.notification_outbox import dispatch
        from monitor.jd_live_advice import notification_events
        from unittest.mock import Mock
        set_subscription(self.f.s.conn,'codex:live_zheshang',True)
        r=self.candidate();v=self.saved(limit_buy_reviews=[r],suggested_orders=[self.value()['suggested_orders'][0],self.buy_order(r)])
        view_id=self.f.s.conn.execute('SELECT view_id FROM jd_live_advice WHERE id=?',(v['id'],)).fetchone()[0]
        events=notification_events(self.f.s.conn,'codex',view_id)
        self.assertEqual(len(events),1)
        n=Mock();n.send.return_value=False;n.last_receipt={'status':'failed','channel':'pushplus'}
        dispatch(self.f.s,n,events,fixtures.NOW)
        self.f.data['quote'].update(buy_price_cny=819,sell_price_cny=819,quoted_at=(fixtures.NOW+timedelta(seconds=1)).isoformat());self.f.snapshot()
        dispatch(self.f.s,n,[],fixtures.NOW+timedelta(seconds=61))
        self.assertEqual(n.send.call_count,1)
        status=self.f.s.conn.execute('SELECT status FROM notification_outbox WHERE event_key=?',(events[0]['key'],)).fetchone()[0]
        self.assertEqual(status,'cancelled')

    def test_unknown_partial_fill_and_incomplete_orders_do_not_invent_protection_coverage(self):
        stop={**self.value()['suggested_orders'][0],'order_key':'c'*24,'action':'sell','status':'partially_filled','filled_grams':None}
        self.f.data['pending_orders'].update(records=[stop],total_count=1);self.f.data['pending_order_count']=1;self.f.snapshot()
        v=self.saved(suggested_orders=[],order_management=[{'order_key':stop['order_key'],'action':'keep','reason':'核对部分成交。'}])
        self.assertIsNone(v['position_path']['coverage']['confirmed_protected_grams'])
        self.f.data['pending_orders']['complete']=False;self.f.snapshot()
        v=self.saved(suggested_orders=[],order_management=[{'order_key':stop['order_key'],'action':'keep','reason':'需补全。'}])
        self.assertIsNone(v['position_path']['coverage']['after_all_buys_grams'])

    def test_bank_reported_pending_order_is_not_assumed_released_by_local_expiry(self):
        old=self.pending_buy();old['expires_at']=(fixtures.NOW-timedelta(minutes=1)).isoformat()
        self.f.data['pending_orders'].update(records=[old],total_count=1);self.f.data['pending_order_count']=1;self.f.snapshot()
        v=self.saved(order_management=[{'order_key':old['order_key'],'action':'cancel','reason':'需核对实际撤单状态。'}],
            action='buy',grams=1,price_cny=850)
        self.assertTrue(v['position_path']['manual_resolution_required'])
        self.assertIsNone(v['position_path']['coverage']['confirmed_protected_grams'])
        self.assertIsNone(notification_parts(v))

    def test_position_change_invalidates_old_path_and_new_input_carries_a_compact_previous_path(self):
        v=self.saved();before=deepcopy(v['position_path']);before.pop('state',None)
        prior=model_input(self.f.s.conn,fixtures.NOW)['accounts']['zheshang']['previous_advice'][0]
        self.assertEqual(prior['position_path']['target_position_pct'],100)
        self.assertNotIn('coverage',prior['position_path'])
        self.f.data.update(total_grams=15,sellable_grams=15,cash_cny=15750);self.f.snapshot()
        stale=self.f.latest();self.assertEqual(stale['action_status'],'account_changed')
        old_path=deepcopy(stale['position_path']);old_path.pop('state',None);self.assertEqual(old_path,before)
        v=self.saved();self.assertEqual(v['position_path']['input_grams'],15)

    def test_fresh_sell_at_an_already_crossed_boundary_is_not_blocked_as_a_stale_buy(self):
        self.f.data['quote'].update(buy_price_cny=810,sell_price_cny=810);self.f.snapshot()
        v=self.saved(action='sell',grams=5,price_condition='reference',price_cny=810,suggested_orders=[])
        self.assertEqual(v['position_path']['state'],'exit_reassessment')
        safe=notification_parts(v);self.assertIsNotNone(safe);self.assertEqual(safe['action'],'sell')

    def test_legacy_advice_remains_readable_but_new_protocol_cannot_omit_path(self):
        self.f.saved(self.f.value());self.assertIsNone(self.f.latest()['position_path'])
        v=self.saved(position_path=None);self.assertTrue(blocking_checks(v))
        with self.assertRaises(ValueError):PositionPath.model_validate(path(target_position_pct=101))
        with self.assertRaises(ValueError):PositionPath.model_validate(path(boundary_scenario_index=True))
        self.assertFalse(privacy_ok({'text':'边界845.67元/克'},
            {'live_readonly_accounts':{'accounts':{'bank':{'position_path':{'boundary_price_cny':845.67}}}}}))
