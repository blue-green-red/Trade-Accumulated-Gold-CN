"""Manual conditional-order proposals, compact reassessment and existing WeChat routing."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,model_input,accounts
from monitor.jd_live_advice import LiveAdvice,save,notification_events
from monitor.jd_live_orders import record,proposal_progress
from test_jd_live import payload,advice

NOW=datetime.now(timezone.utc)
REVIEW={'change_summary':'美元回落，较上轮反弹扩大。','thesis_status':'valid',
        'thesis':'反弹假设仍成立，跌破观察区则重新比较减仓。',
        'price_basis':'目标与复评价为现有采样形成的假设，本轮事件未推翻；不视为涨幅上限。'}


class LiveProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'live.db');ensure_schema(self.s.conn)
        self.data=parse_account('zheshang',payload());self.data.update(sellable_grams=12.5,frozen_grams=0,
            cash_cny=20000,cash_source='jd_electronic_account_api',
            quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':NOW.isoformat()},pending_order_count=0,
            pending_orders={'complete':True,'total_count':0,'records':[]},coverage={'pending_orders':'available'},
            component_times={'pending_orders':NOW.isoformat()})
        self.snapshot()
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def snapshot(self):record_success(self.s.conn,'zheshang',self.data,NOW.isoformat())
    def proposal(self,**changes):
        p={'kind':'limit_sell','grams':5,'price_cny':860,'loss_price_cny':None,
           'expires_at':(NOW+timedelta(days=1)).isoformat(),'reason':'反弹至假设目标附近时分批卖出。'}
        p.update(changes);return p
    def value(self,**changes):
        v=advice();v.update(action='hold',grams=None,reassessment=copy.deepcopy(REVIEW),suggested_orders=[self.proposal()])
        v.update(changes);return v
    def saved(self,value=None):
        view=self.s.conn.execute('SELECT COALESCE(MAX(view_id),0)+1 FROM jd_live_advice').fetchone()[0]
        save(self.s.conn,'codex',view,{'zheshang':value or self.value()},model_input(self.s.conn,NOW),NOW)
        self.s.conn.commit();return view
    def latest(self):return accounts(self.s.conn,NOW)[0]['advice'][0]

    def test_reassessment_round_trip_and_legacy_input_keep_one_full_reason(self):
        self.assertIsNone(LiveAdvice.model_validate(advice()).reassessment)
        for _ in range(4):self.saved(self.value(reason='方案比较：'+'有依据地保留持仓。'*100))
        value=self.latest();self.assertEqual(value['schema_version'],3);self.assertEqual(value['reassessment'],REVIEW)
        previous=model_input(self.s.conn,NOW)['accounts']['zheshang']['previous_advice']
        self.assertEqual(len(previous),1);self.assertEqual(previous[0]['reassessment'],REVIEW)
        self.assertEqual(len(previous[0]['reason']),420)
        self.assertEqual(previous[0]['suggested_orders'][0]['grams'],5)
        self.assertNotIn('risk_note',previous[0])
        for changes in ({'thesis_status':'certain'}, {'thesis':''}):
            with self.assertRaises(ValueError):LiveAdvice.model_validate(self.value(reassessment={**REVIEW,**changes}))

    def test_valid_independent_buy_and_sell_and_combined_protection(self):
        self.saved(self.value(suggested_orders=[self.proposal(kind='limit_buy',grams=2,price_cny=840),self.proposal()]))
        self.assertEqual(self.latest()['checks'],[])
        combined=self.proposal(kind='take_profit_stop_loss',loss_price_cny=830)
        self.saved(self.value(suggested_orders=[combined]));self.assertEqual(self.latest()['checks'],[])
        self.assertEqual(self.latest()['suggested_orders'][0]['execution_progress']['status'],'not_observed')
        self.assertFalse(accounts(self.s.conn,NOW)[0]['order_submission_enabled'])
        self.assertEqual(self.s.conn.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)
        self.saved(self.value(action='buy',grams=None,suggested_orders=[self.proposal(kind='limit_buy',grams=2,price_cny=840)]))
        self.assertTrue(any('即时买入预算未明确' in x for x in self.latest()['checks']))

    def test_new_orders_share_resources_with_existing_and_immediate_actions(self):
        old={'order_key':'a'*24,'kind':'limit_sell','action':'sell','status':'pending',
             'grams':5,'filled_grams':None,'price_cny':870,'price_condition':'at_or_above','expires_at':(NOW+timedelta(days=1)).isoformat()}
        self.data['pending_orders'].update(records=[old],total_count=1);self.data['pending_order_count']=1;self.snapshot()
        management=[{'order_key':old['order_key'],'action':'keep','reason':'保留原单'}]
        self.saved(self.value(order_management=management,suggested_orders=[self.proposal(grams=8)]))
        self.assertTrue(any('合计超过本户持仓' in x for x in self.latest()['checks']))
        self.saved(self.value(order_management=management,action='sell',grams=4))
        self.assertTrue(any('合计超过本户持仓' in x for x in self.latest()['checks']))
        self.data['cash_cny']=100;self.snapshot()
        self.saved(self.value(order_management=management,suggested_orders=[self.proposal(kind='limit_buy',price_cny=840)]))
        self.assertTrue(any('超过本户已知可用资金' in x for x in self.latest()['checks']))

    def test_bad_expiry_duplicate_missing_budget_and_unavailable_data_are_flagged(self):
        cases=[([self.proposal(expires_at='2026-01-01T00:00:00')],'截止时间'),
               ([self.proposal(),self.proposal()],'重复'),
               ([self.proposal(kind='take_profit_stop_loss',loss_price_cny=870)],'低于止盈价'),
               ([self.proposal(price_cny=849)],'当前已满足')]
        for proposed,expected in cases:
            with self.subTest(expected=expected):
                self.saved(self.value(suggested_orders=proposed));self.assertTrue(any(expected in x for x in self.latest()['checks']))
        self.data['cash_cny']=None;self.data['cash_source']='unknown';self.snapshot()
        self.saved(self.value(suggested_orders=[self.proposal(kind='limit_buy',price_cny=840)]))
        self.assertTrue(any('预算' in x for x in self.latest()['checks']))
        self.data['pending_orders']['complete']=False;self.snapshot();self.saved()
        self.assertTrue(any('明细不完整' in x for x in self.latest()['checks']))
        self.saved(self.value(suggested_orders=[]));self.assertEqual(self.latest()['suggested_orders'],[])

    def test_hold_proposals_notify_meaningful_changes_only_and_invalid_drafts_stay_quiet(self):
        view=self.saved();events=notification_events(self.s.conn,'codex',view)
        self.assertEqual(len(events),1);self.assertIn('条件单建议',events[0]['title'])
        self.assertEqual(events[0]['notification_topic'],'codex:live_zheshang')
        self.assertIn('尚未提交京东',events[0]['body']);self.assertIn('860元/克',events[0]['body'])
        v=self.value(suggested_orders=[self.proposal(expires_at=(NOW+timedelta(days=2)).isoformat(),reason='改写解释')])
        view=self.saved(v);self.assertEqual(notification_events(self.s.conn,'codex',view),[])
        view=self.saved(self.value(suggested_orders=[self.proposal(price_cny=865)]))
        self.assertEqual(len(notification_events(self.s.conn,'codex',view)),1)
        view=self.saved(self.value(suggested_orders=[]));events=notification_events(self.s.conn,'codex',view)
        self.assertEqual(len(events),1);self.assertIn('已挂到京东的单以本轮挂单处理建议为准',events[0]['body'])
        view=self.saved(self.value(suggested_orders=[self.proposal(grams=100)]))
        self.assertEqual(notification_events(self.s.conn,'codex',view),[])

    def test_manually_placed_proposal_is_inferred_and_pinned_without_withdrawal_notice(self):
        self.saved()
        order={**self.proposal(),'order_key':'a'*24,'action':'sell','status':'pending',
               'created_at':(NOW+timedelta(seconds=1)).isoformat(),'price_condition':'at_or_above','filled_grams':None}
        record(self.s.conn,'zheshang',[order],NOW.isoformat())
        self.data['pending_orders'].update(records=[order],total_count=1);self.data['pending_order_count']=1;self.snapshot()
        keep=[{'order_key':order['order_key'],'action':'keep','reason':'保留用户已挂的单'}]
        view=self.saved(self.value(suggested_orders=[],order_management=keep))
        self.assertEqual(notification_events(self.s.conn,'codex',view),[])
        for _ in range(4):self.saved(self.value(suggested_orders=[],order_management=keep))
        previous=model_input(self.s.conn,NOW)['accounts']['zheshang']['previous_advice']
        self.assertEqual(len(previous),2)
        original=next(x for x in previous if x['id']==1)
        self.assertEqual(original['suggested_orders'][0]['execution_progress']['status'],'pending')
        self.assertNotIn('reason',original)

    def test_same_proposal_notifies_when_previously_invalid_quote_recovers(self):
        self.data['quote']['quoted_at']=(NOW-timedelta(minutes=10)).isoformat();self.snapshot()
        view=self.saved();self.assertEqual(notification_events(self.s.conn,'codex',view),[])
        self.data['quote']['quoted_at']=NOW.isoformat();self.snapshot()
        view=self.saved();self.assertEqual(self.latest()['checks'],[])
        self.assertEqual(len(notification_events(self.s.conn,'codex',view)),1)

    def test_combined_order_matching_requires_both_prices_and_matching_kind(self):
        proposal=self.proposal(kind='take_profit_stop_loss',loss_price_cny=830)
        order={**proposal,'order_key':'a'*24,'status':'pending','action':'sell','price_condition':'none',
               'created_at':(NOW+timedelta(seconds=1)).isoformat()}
        result=proposal_progress(proposal,NOW.isoformat(),[order])
        self.assertEqual(result['match'],'exact_parameters')
        for change in ({'loss_price_cny':820},{'kind':'limit_sell'}):
            self.assertEqual(proposal_progress(proposal,NOW.isoformat(),[{**order,**change}])['status'],'not_observed')
