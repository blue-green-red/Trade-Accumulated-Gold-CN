"""Candidate drift is input evidence, never a reason to force a trade or reprice."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import sqlite3
import unittest

from monitor.jd_live_feedback import entry_reassessment,strategy_review,plan_signature
from monitor.jd_live_readonly import model_input
import test_live_proposals as fixtures
from test_live_entry import review

NOW=datetime(2026,11,11,tzinfo=timezone.utc)


def row(i,quote,entry,**changes):
    value={'id':i,'analyst':'codex','created_at':(NOW+timedelta(minutes=i)).isoformat(),
        'snapshot_revision':'same-account','action':'wait','input_quote_status':'fresh',
        'input_quote_cny':quote,'input_quote_at':(NOW+timedelta(minutes=i)).isoformat(),
        'suggested_orders':[],'limit_buy_reviews':[{'decision':'needs_review','entry_price_cny':entry,
            'expires_at':(NOW+timedelta(days=1)).isoformat(),'basis':'合成价位用于测试，不是真实行情。','checks':[]}]}
    value.update(changes);return value


class EntryReassessmentTests(unittest.TestCase):
    def setUp(self):self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close)
    def test_repeated_relative_discount_and_price_drift_are_reported_without_changing_terms(self):
        history=[row(3,980,940.8),row(2,990,950.4),row(1,1000,960)]
        before=deepcopy(history);signatures=[plan_signature(v) for v in history]
        facts=entry_reassessment(self.conn,history)
        self.assertEqual(facts['latest_discount_pct'],4)
        self.assertEqual(facts['same_discount_rounds'],3)
        self.assertEqual(facts['deferred_candidate_rounds'],3)
        self.assertEqual(facts['candidate_move_pct'],facts['input_price_move_pct'])
        self.assertFalse(facts['previous_candidate_reached'])
        self.assertEqual(before,history);self.assertEqual(signatures,[plan_signature(v) for v in history])

    def test_reached_old_candidate_is_a_fact_but_expired_candidate_is_not_counted(self):
        latest=row(2,950,912);old=row(1,1000,960)
        self.assertTrue(entry_reassessment(self.conn,[latest,old])['previous_candidate_reached'])
        old['limit_buy_reviews'][0]['expires_at']=latest['input_quote_at']
        self.assertFalse(entry_reassessment(self.conn,[latest,old])['previous_candidate_reached'])
        old['limit_buy_reviews'][0]['expires_at']=None
        self.assertIsNone(entry_reassessment(self.conn,[latest,old])['previous_candidate_reached'])

    def test_fixed_absolute_level_is_not_mislabeled_as_repeated_relative_discount(self):
        facts=entry_reassessment(self.conn,[row(3,980,900),row(2,990,900),row(1,1000,900)])
        self.assertEqual(facts['same_discount_rounds'],1);self.assertEqual(facts['candidate_move_pct'],0)

    def test_missing_invalid_changed_account_and_actual_buy_do_not_create_stale_feedback(self):
        old=row(1,1000,960)
        for latest in (row(2,980,940.8,action='buy'),
                       row(2,980,940.8,snapshot_revision='new-account'),
                       row(2,980,940.8,input_quote_status='stale'),
                       row(2,980,940.8,suggested_orders=[{'kind':'limit_buy'}]),
                       row(2,980,940.8,limit_buy_reviews=[])):
            self.assertIsNone(entry_reassessment(self.conn,[latest,old]))
        bad=row(2,980,940.8);bad['limit_buy_reviews'][0]['checks']=['测试无效候选']
        self.assertIsNone(entry_reassessment(self.conn,[bad,old]))
        self.assertIsNone(entry_reassessment(self.conn,[]))

    def test_strategy_feedback_stays_with_model_snapshot_and_input_time(self):
        account={'connected':True,'quote':{'status':'fresh','sell_price_cny':980,'quoted_at':(NOW+timedelta(minutes=3)).isoformat()},
            'advice':[row(3,980,940.8)],'advice_history':[row(2,990,950.4),row(1,1000,960),
                row(4,900,800),row(5,950,850,analyst='claude')]}
        facts=strategy_review(self.conn,account,'codex',NOW+timedelta(minutes=3))
        self.assertEqual(facts['lookback_rounds'],3)
        self.assertEqual(facts['entry_reassessment']['same_discount_rounds'],3)
        self.assertFalse(facts['revalidate_unchanged_plan'])


class EntryInputIntegrationTests(unittest.TestCase):
    def test_input_carries_one_basis_and_compact_feedback_and_validates_no_trade_without_repricing(self):
        f=fixtures.LiveProposalTests();f.setUp();self.addCleanup(f.tearDown)
        for i,quote in enumerate((850,849,848)):
            f.data['quote'].update(buy_price_cny=quote,sell_price_cny=quote,
                quoted_at=(fixtures.NOW+timedelta(seconds=i)).isoformat())
            f.snapshot()
            candidate=review(decision='needs_review',entry_price_cny=round(quote*.96,2),target_price_cny=quote,
                             risk_exit_cny=round(quote*.96*.97,2),basis='合成波动假设；百分比可保留，不强制改价。')
            f.saved(f.value(action='wait',limit_buy_reviews=[candidate],suggested_orders=[]))
        source=model_input(f.s.conn,fixtures.NOW)['accounts']['zheshang']
        previous=source['previous_advice'][0]
        self.assertEqual(len(source['previous_advice']),1)
        self.assertEqual(previous['limit_buy_reviews'][0]['basis'],candidate['basis'])
        self.assertNotIn('metrics',previous['limit_buy_reviews'][0])
        self.assertEqual(previous['strategy_review']['entry_reassessment']['same_discount_rounds'],3)
        f.saved(f.value(action='wait',limit_buy_reviews=[candidate],suggested_orders=[]))
        latest=f.latest()
        self.assertEqual(latest['checks'],[]);self.assertEqual(latest['suggested_orders'],[])
        self.assertEqual(latest['limit_buy_reviews'][0]['entry_price_cny'],candidate['entry_price_cny'])
        self.assertIn('entry_reassessment',latest['input_strategy_review'])
