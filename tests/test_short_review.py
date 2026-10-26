import json
import unittest
from datetime import timedelta
from unittest.mock import Mock
import test_autonomous as fixtures
from monitor.store import utc_now
from monitor.ai_inputs import compact_context
from monitor.short_review import wait_review


class ShortReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AutonomousTests();self.f.setUp()
        self.now=utc_now();self.since=self.now-timedelta(minutes=10)
        self.f.a._run_codex=Mock(return_value=('notes',[],fixtures.response(short_full={'action':'wait'},short_empty={'action':'wait'})))
        self.f.a.analyze({'snapshot_at':self.since.isoformat(),'xauusd':4000,
            'trading_accounts':self.f.m.portfolios.input('codex',self.f.snap['execution_quote'])},'test')
        c=self.f.store.conn
        r=c.execute("SELECT rowid,view_json FROM ai_views WHERE analyst='codex' ORDER BY rowid DESC LIMIT 1").fetchone()
        view=json.loads(r['view_json']);view['_price_guard']={'jd':1000}
        c.execute('UPDATE ai_views SET view_json=? WHERE rowid=?',(json.dumps(view),r['rowid']));c.commit()
        self.view_id=r['rowid']
        self.f.m.portfolios.on_tick(self.f.snap)
        for price,offset in [(990,2),(1020,5),(900,11),(-1,6)]:
            s=fixtures.snapshot(price);s.update(ts=self.since+timedelta(minutes=offset),jd_price=price)
            self.f.store.add_tick(s)

    def tearDown(self):
        self.f.tearDown()

    def test_feedback_cutoff_scope_and_compaction(self):
        quote=dict(self.f.snap['execution_quote'],price=1010)
        value=self.f.m.portfolios.input('codex',quote,as_of=self.now.isoformat())
        review=value['accounts']['short_empty']['short_wait_review']
        self.assertEqual(review['previous_quote_cny'],1000)
        self.assertEqual(review['current_quote_cny'],1010)
        self.assertEqual(review['change_pct'],1)
        self.assertEqual((review['sampled_low_cny'],review['sampled_high_cny'],review['samples']),(990,1020,2))
        self.assertNotIn('short_wait_review',value['accounts']['short_full'])  # still holds initial position
        self.assertNotIn('short_wait_review',value['accounts']['medium_empty'])
        compact=compact_context({'trading_accounts':value})
        self.assertEqual(compact['trading_accounts']['accounts']['short_empty']['short_wait_review'],review)

    def test_missing_anchor_does_not_invent_price_and_future_decision_is_omitted(self):
        c=self.f.store.conn;r=c.execute('SELECT view_json FROM ai_views WHERE rowid=?',(self.view_id,)).fetchone()
        v=json.loads(r[0]);v['_price_guard']['jd']=None
        c.execute('UPDATE ai_views SET view_json=? WHERE rowid=?',(json.dumps(v),self.view_id));c.commit()
        account=self.f.m.portfolios.input('codex',None,as_of=self.now.isoformat())['accounts']['short_empty']
        review=account['short_wait_review']
        self.assertIsNone(review['previous_quote_cny']);self.assertIsNone(review['change_pct']);self.assertIsNone(review['current_quote_cny'])
        self.assertIsNone(wait_review(c,{'view_id':self.view_id,'decision':{'action':'wait'}},None,(self.since-timedelta(seconds=1)).isoformat()))

    def test_short_policy_and_output_budget_are_consistent(self):
        text=self.f.a.trading_system({'trading_accounts':{}})
        # Empty contexts are intentionally not trading requests.
        self.assertEqual(text,'')
        text=self.f.a.trading_system({'trading_accounts':{'accounts':{}}})
        self.assertIn('30分钟和1小时',text)
        self.assertIn('不强制试仓',text)
        self.assertIn('不固定900',text)
        self.assertIn('上次条件是否出现',text)
        self.assertIn('短线reason不超过240字',self.f.a.result_instructions())
