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
        self.assertIn('以日线和数日至周级别',text)
        self.assertIn('short_*必须忽略这些指标',text)
        self.assertIn('不要求补齐小时K线',text)
        self.assertIn('不是必须完成的配额',text)
        self.assertNotIn('数小时至3交易日',text)
        self.assertIn('不强制试仓',text)
        self.assertIn('不固定900',text)
        self.assertIn('上次条件是否出现',text)
        self.assertIn('短线reason不超过360字',self.f.a.result_instructions())
        self.assertIn('不得把未提供的完整京东人民币日线',text)
        self.assertIn('不能逐轮重复等待缺失数据',text)
        self.assertIn('情景测算',text)
        self.assertIn('不得为了避免等待而虚构目标或强制买入',text)
        self.assertIn('机会不合适：',self.f.a.result_instructions())
        self.assertIn('证据不足：',self.f.a.result_instructions())

    def test_wave_profile_and_trade_count_exclude_initialization_and_future(self):
        c=self.f.store.conn
        self.assertEqual(self.f.m.portfolios.input('codex',self.f.snap['execution_quote'])['accounts']['short_full']['short_trade_activity']['filled_operations'],0)
        original=dict(c.execute("SELECT * FROM ai_portfolio_fills WHERE account='codex:short_full' LIMIT 1").fetchone())
        for day,side in [(-2,'sell'),(-1,'buy'),(-8,'sell'),(1,'buy')]:
            stamp=(self.now+timedelta(days=day)).isoformat()
            c.execute("INSERT INTO ai_portfolio_fills(account,ts,side,grams,price,fee,pnl,reason,quote_json) VALUES(?,?,?,?,?,?,?,?,?)",
                ('codex:short_full',stamp,side,1,1000,0,0,'策略成交',original['quote_json']))
        c.commit()
        value=self.f.m.portfolios.input('codex',self.f.snap['execution_quote'],as_of=self.now.isoformat())
        self.assertEqual(value['accounts']['short_full']['short_trade_activity']['filled_operations'],2)
        self.assertNotIn('short_trade_activity',value['accounts']['medium_full'])
        self.assertEqual(value['short_strategy_profile']['primary_timeframes'],['1D'])
        self.assertIn('数日至一两周',value['short_strategy_profile']['holding_period'])
        self.assertEqual(value['short_strategy_profile']['ignored_timeframes'],['30m','1H','4H'])
