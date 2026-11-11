"""Existing real-advice actions can express staged entries without executing bank trades."""
import unittest

from monitor.jd_live_readonly import accounts,record_success
import test_live_proposals as fixtures
from test_live_entry import review


class StagedEntryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LiveProposalTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.data.update(total_grams=0,sellable_grams=0,frozen_grams=0,average_cost_cny=None)
        self.f.snapshot()
    def value(self):
        candidates=[review(entry_price_cny=830,target_price_cny=880,risk_exit_cny=800),
                    review(entry_price_cny=810,target_price_cny=850,risk_exit_cny=780)]
        drafts=[self.f.proposal(kind='limit_buy',grams=r['grams'],price_cny=r['entry_price_cny'],expires_at=r['expires_at']) for r in candidates]
        return self.f.value(action='buy',grams=5,price_condition='reference',price_cny=850,
            reason='合成分批路径：首笔参与，后续按价格与假设变化重评；总预算明确，不要求首笔最低。',
            limit_buy_reviews=candidates,suggested_orders=drafts)
    def test_first_buy_plus_two_lower_entries_share_one_cash_pool_without_auto_submission(self):
        value=self.value();self.f.saved(value);saved=self.f.latest()
        self.assertEqual(saved['checks'],[]);self.assertEqual(saved['action'],'buy')
        self.assertEqual(len(saved['suggested_orders']),2)
        self.assertEqual([o['price_cny'] for o in saved['suggested_orders']],[830,810])
        self.assertFalse(accounts(self.f.s.conn,fixtures.NOW)[0]['order_submission_enabled'])
        self.assertEqual(self.f.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)
    def test_next_review_may_add_after_a_decline_when_cash_is_confirmed(self):
        self.f.saved(self.value())
        self.f.data.update(total_grams=5,sellable_grams=5,average_cost_cny=850,cash_cny=15750)
        self.f.data['quote'].update(buy_price_cny=830,sell_price_cny=830)
        record_success(self.f.s.conn,'zheshang',self.f.data,fixtures.NOW.isoformat())
        self.f.saved(self.f.value(action='buy',grams=5,price_condition='reference',price_cny=830,
            limit_buy_reviews=[],suggested_orders=[],reason='合成测试：价格下降后按新账户状态决定第二笔，不因浮亏自动加仓。'))
        saved=self.f.latest();self.assertEqual(saved['checks'],[])
        self.assertEqual(saved['action'],'buy');self.assertEqual(saved['grams'],5)
    def test_staging_cannot_spend_the_same_cash_more_than_once(self):
        self.f.data['cash_cny']=10000;self.f.snapshot();self.f.saved(self.value())
        saved=self.f.latest()
        self.assertTrue(saved['suggested_orders'][1]['checks'])
        self.assertTrue(any('合计超过本户已知可用资金' in s for s in saved['checks']))
