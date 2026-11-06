"""Lower-entry plans remain independent reviews until complete, consistent drafts exist."""
from copy import deepcopy
from datetime import timedelta
import unittest

from monitor.jd_live_entry import LimitBuyReview,metrics,same_order,format_reviews
from monitor.jd_live_semantics import notification_parts
from monitor.public_ai import privacy_ok
from test_live_risk import source
import test_live_proposals as fixtures


def review(**changes):
    value={'decision':'recommend','entry_price_cny':840.,'target_price_cny':880.,'risk_exit_cny':820.,
           'grams':5.,'expires_at':(fixtures.NOW+timedelta(hours=6)).isoformat(),
           'basis':'候选为独立低位假设；新增退出用于重新评估原交易假设，不是旧仓保护。',
           'reason':'按自选规模提前参与，风险可承受；仍需本人确认京东条件单。'}
    value.update(changes);return value


class EntryMathTests(unittest.TestCase):
    def test_new_buy_and_whole_account_math_have_separate_correct_fee_baselines(self):
        candidate=review(entry_price_cny=850,target_price_cny=900,risk_exit_cny=820,grams=10)
        s=source();before=deepcopy(s);m=metrics(candidate,s)
        self.assertEqual(m['budget_cny'],8500)
        self.assertEqual(m['new_buy_upside_cny'],464)
        self.assertEqual(m['new_buy_exit_pnl_cny'],-332.8)
        self.assertAlmostEqual(m['net_upside_pct'],5.4588,places=4)
        self.assertAlmostEqual(m['net_exit_return_pct'],-3.9153,places=4)
        self.assertEqual(m['position_after_pct'],20.5172)
        self.assertEqual(m['account_at_exit_change_cny'],-11288.8)
        self.assertEqual(s,before)

    def test_missing_data_stays_unknown_and_no_old_protection_is_used(self):
        s=source();s.update(target_price=9999,stop_price=1)
        self.assertIsNone(metrics(review(risk_exit_cny=None),s))
        self.assertIsNone(metrics(review(),{**s,'sell_fee_rate':None}))
        self.assertIsNone(metrics(review(target_price_cny=800),s))
        m=metrics(review(grams=None),s);self.assertIn('net_upside_pct',m);self.assertNotIn('budget_cny',m)
        m=metrics(review(),{**s,'cash_cny':None});self.assertNotIn('position_after_pct',m)
        self.assertNotIn('account_at_exit_change_pct',m)

    def test_review_schema_preserves_unavailable_values_and_private_entry_is_not_public(self):
        value=review(decision='unavailable',entry_price_cny=None,target_price_cny=None,risk_exit_cny=None,grams=None,expires_at=None)
        self.assertIsNone(LimitBuyReview.model_validate(value).entry_price_cny)
        with self.assertRaises(ValueError):LimitBuyReview.model_validate(review(grams=0))
        ctx={'live_readonly_accounts':{'accounts':{'bank':{'previous_advice':[{'limit_buy_reviews':[review(entry_price_cny=845.67,target_price_cny=899.12)]}]}}}}
        self.assertFalse(privacy_ok({'text':'入场845.67、目标899.12'},ctx))


class EntryValidationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveProposalTests();self.f.setUp();self.addCleanup(self.f.tearDown)

    def candidate_draft(self,r):
        return self.f.proposal(kind='limit_buy',price_cny=r['entry_price_cny'],grams=r['grams'],expires_at=r['expires_at'])

    def save(self,r,orders=None):
        self.f.saved(self.f.value(limit_buy_reviews=[r],suggested_orders=[] if orders is None else orders))
        return self.f.latest()

    def test_complete_recommendation_has_matching_draft_metrics_and_input_history(self):
        from monitor.jd_live_readonly import model_input
        r=review();value=self.save(r,[self.candidate_draft(r)])
        self.assertEqual(value['schema_version'],5);self.assertEqual(value['checks'],[])
        stored=value['limit_buy_reviews'][0];self.assertIsNotNone(stored['metrics'])
        self.assertIn('低位买入评估：建议提前挂出',format_reviews(value))
        prior=model_input(self.f.s.conn,fixtures.NOW)['accounts']['zheshang']['previous_advice'][0]
        self.assertEqual(prior['limit_buy_reviews'][0]['risk_exit_cny'],820)
        self.assertNotIn('metrics',prior['limit_buy_reviews'][0])

    def test_recommend_requires_complete_plan_and_actual_same_terms(self):
        for field in ('entry_price_cny','target_price_cny','risk_exit_cny','grams','expires_at'):
            candidate=review(**{field:None});draft=self.candidate_draft(review())
            with self.subTest(field=field):
                value=self.save(candidate,[draft])
                self.assertTrue(value['limit_buy_reviews'][0]['checks'])
                self.assertTrue(value['suggested_orders'][0]['checks'])
        value=self.save(review());self.assertTrue(value['limit_buy_reviews'][0]['checks'])
        candidate=review();order=self.candidate_draft(candidate);order['grams']=4
        value=self.save(candidate,[order]);self.assertTrue(value['suggested_orders'][0]['checks'])

    def test_unrecommended_and_incomplete_reviews_do_not_create_orders(self):
        for decision in ('not_worthwhile','needs_review','unavailable'):
            r=review(decision=decision)
            if decision=='unavailable':r.update(entry_price_cny=None,target_price_cny=None,risk_exit_cny=None,grams=None,expires_at=None)
            value=self.save(r)
            self.assertEqual(value['checks'],[]);self.assertEqual(value['suggested_orders'],[])
        r=review(decision='needs_review');buy=self.candidate_draft(r);sell=self.f.proposal()
        value=self.save(r,[buy,sell]);safe=notification_parts(value)
        self.assertIsNotNone(safe);self.assertEqual([o['kind'] for o in safe['suggested_orders']],['limit_sell'])

    def test_invalid_prices_cash_deadline_and_actual_reservations_are_flagged(self):
        for changes in ({'entry_price_cny':851},{'target_price_cny':830},{'risk_exit_cny':845},
                        {'grams':100},{'expires_at':'2026-01-01'}):
            r=review(**changes)
            with self.subTest(changes=changes):
                value=self.save(r,[self.candidate_draft(review())]);self.assertTrue(value['limit_buy_reviews'][0]['checks'])
        r=review();order=self.candidate_draft(r);order['expires_at']=order['expires_at'].replace('+00:00','Z')
        self.assertTrue(same_order(r,order))
        value=self.save(r,[order]);self.assertEqual(value['checks'],[])

    def test_new_buy_draft_cannot_skip_review_but_legacy_raw_records_remain_readable(self):
        buy=self.candidate_draft(review())
        self.f.saved(self.f.value(limit_buy_reviews=[],suggested_orders=[buy]))
        self.assertTrue(self.f.latest()['suggested_orders'][0]['checks'])
        self.f.saved(self.f.value(suggested_orders=[buy]))
        self.assertEqual(self.f.latest()['checks'],[])

    def test_recommended_risk_terms_notify_but_rewording_and_rolling_deadline_do_not(self):
        from monitor.jd_live_advice import notification_events
        r=review();v=self.f.saved(self.f.value(limit_buy_reviews=[r],suggested_orders=[self.candidate_draft(r)]))
        self.assertEqual(len(notification_events(self.f.s.conn,'codex',v)),1)
        r=review(reason='仅补充解释',expires_at=(fixtures.NOW+timedelta(hours=7)).isoformat())
        v=self.f.saved(self.f.value(limit_buy_reviews=[r],suggested_orders=[self.candidate_draft(r)]))
        self.assertEqual(notification_events(self.f.s.conn,'codex',v),[])
        r['risk_exit_cny']=815
        v=self.f.saved(self.f.value(limit_buy_reviews=[r],suggested_orders=[self.candidate_draft(r)]))
        events=notification_events(self.f.s.conn,'codex',v)
        self.assertEqual(len(events),1);self.assertIn('新增退出参考815',events[0]['body'])
