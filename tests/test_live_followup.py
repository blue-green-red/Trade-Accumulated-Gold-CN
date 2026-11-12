"""Discarded references never become buy candidates, protection, or bank executions."""
from copy import deepcopy
from datetime import timedelta
import unittest

from monitor.jd_live_entry import format_reviews
from monitor.jd_live_risk import effect,protection_state
from monitor.jd_live_readonly import model_input
from monitor.jd_live_semantics import notification_parts
from monitor.jd_live_notice import body
from test_live_entry import review
from test_live_risk import source
import test_live_proposals as fixtures


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveProposalTests();self.f.setUp();self.addCleanup(self.f.tearDown)

    def rejected(self):
        return review(decision='not_worthwhile',entry_price_cny=860,
            expires_at=(fixtures.NOW-timedelta(hours=1)).isoformat(),reason='旧方案已废弃，本轮独立比较新增一笔。')

    def test_rejected_reference_does_not_taint_current_checks_or_repeat_old_terms_in_input(self):
        self.f.saved(self.f.value(limit_buy_reviews=[self.rejected()],suggested_orders=[]))
        saved=self.f.latest();self.assertEqual(saved['checks'],[])
        candidate=saved['limit_buy_reviews'][0]
        self.assertIsNone(candidate['metrics']);self.assertEqual(candidate['entry_price_cny'],860)
        text=format_reviews(saved);self.assertIn('未采纳方案复核',text)
        self.assertNotIn('入场≤',text);self.assertNotIn('候选截止',text)
        prior=model_input(self.f.s.conn,fixtures.NOW)['accounts']['zheshang']['previous_advice'][0]['limit_buy_reviews'][0]
        self.assertEqual(set(prior),{'decision','basis','reason'})
        self.assertFalse(saved['input_protection']['status']=='present')

    def test_rejected_reference_cannot_authorize_a_buy_draft_and_pending_review_stays_strict(self):
        draft=self.f.proposal(kind='limit_buy',price_cny=840)
        self.f.saved(self.f.value(limit_buy_reviews=[self.rejected()],suggested_orders=[draft]))
        saved=self.f.latest();self.assertTrue(saved['suggested_orders'][0]['checks'])
        self.assertEqual(notification_parts(saved)['suggested_orders'],[])
        for decision in ('needs_review','recommend'):
            self.f.saved(self.f.value(limit_buy_reviews=[review(decision=decision,
                expires_at=(fixtures.NOW-timedelta(hours=1)).isoformat())],suggested_orders=[]))
            self.assertTrue(self.f.latest()['limit_buy_reviews'][0]['checks'])

    def test_protection_is_observed_from_complete_fresh_orders_not_advice(self):
        src={'status':'fresh','pending_orders':{'status':'fresh','complete':True,'records':[]}}
        before=deepcopy(src);self.assertEqual(protection_state(src)['status'],'none');self.assertEqual(src,before)
        src['suggested_orders']=[self.f.proposal(kind='take_profit_stop_loss',loss_price_cny=820)]
        self.assertEqual(protection_state(src)['status'],'none')
        order={**src['suggested_orders'][0],'status':'pending'};src['pending_orders']['records']=[order]
        self.assertEqual(protection_state(src)['status'],'present')
        order['status']='filled';self.assertEqual(protection_state(src)['status'],'none')
        src['pending_orders']['complete']=False;self.assertEqual(protection_state(src)['status'],'unknown')
        order['status']='partially_filled';self.assertEqual(protection_state(src)['status'],'present')
        src['pending_orders']['status']='stale';self.assertEqual(protection_state(src)['status'],'unknown')
        self.assertIsNone(protection_state(src)['count'])

    def test_cost_pnl_and_current_valuation_have_independent_fee_baselines(self):
        src={**source(),'average_cost_cny':860}
        target=effect(src,900,'sell',200);stop=effect(src,840,'sell',200)
        self.assertEqual(target['position_pnl_cny'],7280)
        self.assertEqual(target['account_change_cny'],4980)
        self.assertEqual(stop['position_pnl_cny'],-4672)
        self.assertEqual(stop['account_change_cny'],-6972)
        self.assertEqual(stop['sale_fee_cny'],672)
        self.assertNotIn('position_pnl_cny',effect(source(),840,'sell',200))
        self.assertNotIn('position_pnl_cny',effect(src,850,'buy',10))
        self.assertEqual(effect(src,840,'sell',100)['position_pnl_cny'],-2336)

    def test_notice_distinguishes_unsubmitted_protection_and_cost_pnl(self):
        from monitor.jd_live_risk import annotate
        account={'bank':'zheshang','pending_orders':{'status':'fresh','records':[]},
                 'protection_state':{'status':'none'}}
        value=self.f.value(suggested_orders=[self.f.proposal(kind='take_profit_stop_loss',
            grams=100,price_cny=900,loss_price_cny=840)])
        annotate(value,{**source(),'average_cost_cny':860})
        text=body(account,{'id':1,'expires_at':(fixtures.NOW+timedelta(hours=1)).isoformat()},value)
        self.assertIn('未发现已挂组合止盈止损单',text)
        self.assertIn('已扣卖费的盈亏-2336.00元',text)
        self.assertIn('整户相对分析时估值变化-6972.00元',text)
        self.assertNotIn('已生效',text)
        self.assertEqual(self.f.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)

    def test_full_cash_can_be_recommended_after_a_confirmed_first_purchase(self):
        self.f.data.update(total_grams=0,sellable_grams=0,frozen_grams=0,average_cost_cny=None)
        self.f.snapshot()
        self.f.saved(self.f.value(action='buy',grams=10,price_condition='reference',price_cny=850,suggested_orders=[]))
        self.assertEqual(self.f.latest()['checks'],[])
        self.f.data.update(total_grams=10,sellable_grams=10,average_cost_cny=850,cash_cny=11500)
        self.f.snapshot()
        self.f.saved(self.f.value(action='buy',grams=11500/850,price_condition='reference',price_cny=850,suggested_orders=[]))
        self.assertEqual(self.f.latest()['checks'],[])
        # Recommendations do not change the bank snapshot or fabricate a fill.
        self.assertEqual(self.f.latest()['input_state']['total_grams'],10)
        self.assertEqual(self.f.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)
