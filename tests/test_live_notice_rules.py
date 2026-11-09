"""Milestone reminders use persisted inputs; all delivery tests mock the provider."""
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock

from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,model_input
from monitor.jd_live_advice import LiveAdvice,save,notification_events
from monitor.jd_live_notice_rules import reasons
from monitor.notification_control import set_subscription
from monitor.notification_outbox import dispatch
from test_jd_live import payload

NOW=datetime(2026,11,9,2,tzinfo=timezone.utc)


def hold():
    return LiveAdvice(action='hold',reason='维持本轮仓位',risk_note='价格仍可能波动').model_dump()


class MilestoneRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
        self.s=Store(self.path);ensure_schema(self.s.conn)
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def add(self,at=NOW,bank='zheshang',model='codex',grams=12.5,cash=20000,**changes):
        value=hold();value.update(input_quote_status='fresh',input_state={'total_grams':grams,'cash_cny':cash})
        value.update(changes)
        count=self.s.conn.execute('SELECT COUNT(*) FROM jd_live_advice').fetchone()[0]
        at=at.isoformat() if isinstance(at,datetime) else at
        cur=self.s.conn.execute('INSERT INTO jd_live_advice(bank,analyst,view_id,created_at,expires_at,snapshot_revision,advice_json) VALUES(?,?,?,?,?,?,?)',
            (bank,model,count+1,at,(NOW+timedelta(days=7)).isoformat(),'fixture',json.dumps(value)))
        result=reasons(self.s.conn,model,bank,cur.lastrowid,at,value)
        self.s.conn.commit();return result

    def test_daily_first_and_restart_persist_without_delivery(self):
        self.assertEqual(self.add(),['daily_first'])
        self.assertEqual(self.add(at=NOW+timedelta(hours=1)),[])
        self.s.conn.close();self.s=Store(self.path)
        self.assertEqual(self.add(at=NOW+timedelta(hours=2)),[])
        self.assertEqual(self.add(at=NOW+timedelta(days=1)),['daily_first'])

    def test_beijing_midnight_handles_offsets_and_microseconds(self):
        self.assertEqual(self.add(at='2026-11-09T15:59:59.999999Z'),['daily_first'])
        self.assertEqual(self.add(at='2026-11-10T00:00:00+08:00'),['daily_first'])
        self.assertEqual(self.add(at='2026-11-09T16:00:00.000001+00:00'),[])

    def test_each_material_change_once_including_return_to_old_position(self):
        self.add()
        self.assertEqual(self.add(grams=10),['position_changed'])
        self.assertEqual(self.add(grams=10),[])
        self.assertEqual(self.add(grams=10,cash=21000),['position_changed'])
        self.assertEqual(self.add(grams=10,cash=21000),[])
        self.assertEqual(self.add(),['position_changed'])

    def test_thresholds_ignore_rounding_not_boundary_or_cash_only_changes(self):
        self.add()
        self.assertEqual(self.add(grams=12.5002,cash=20000.01),[])
        self.assertEqual(self.add(grams=12.5102,cash=20000.01),['position_changed'])
        self.assertEqual(self.add(grams=12.5102,cash=20001.01),['position_changed'])

    def test_quote_percentage_orders_and_cost_changes_do_not_count_as_position(self):
        self.add(input_position_pct=30,input_quote_cny=850)
        self.assertEqual(self.add(input_position_pct=33,input_quote_cny=900,
            input_state={'total_grams':12.5,'cash_cny':20000,'pending_order_count':1,'recent_trade':'metadata'},
            average_cost_cny=800),[])

    def test_unknown_values_do_not_become_zero_or_erase_known_baseline(self):
        self.add(grams=None,cash=None)
        self.assertEqual(self.add(),[])
        self.assertEqual(self.add(grams=None,cash=None),[])
        self.assertEqual(self.add(grams=10,cash=None),['position_changed'])
        self.assertEqual(self.add(grams=10),[])
        self.assertEqual(self.add(grams=10,cash=0),['position_changed'])

    def test_invalid_analyses_do_not_consume_milestones(self):
        self.assertEqual(self.add(checks=['输入账户数据不可用或过期']),[])
        self.assertEqual(self.add(input_quote_status='stale'),[])
        self.assertEqual(self.add(),['daily_first'])
        self.assertEqual(self.add(grams=10,checks=['未知校验失败']),[])
        self.assertEqual(self.add(grams=10),['position_changed'])
        self.assertEqual(self.add(grams=10),[])

    def test_blocked_partial_hold_does_not_consume_daily_milestone(self):
        self.assertEqual(self.add(checks=['复评条件：无效阈值'],
            review_conditions=[{'checks':['无效阈值']}]),[])
        self.assertEqual(self.add(),['daily_first'])

    def test_bank_model_scopes_and_overlapping_reasons(self):
        self.assertEqual(self.add(),['daily_first'])
        self.assertEqual(self.add(bank='minsheng'),['daily_first'])
        self.assertEqual(self.add(model='claude'),['daily_first'])
        self.assertEqual(self.add(at=NOW+timedelta(days=1),grams=10),['daily_first','position_changed'])
        self.assertEqual(self.add(at=NOW+timedelta(days=1),grams=10),[])


class MilestoneNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
        self.s=Store(self.path);ensure_schema(self.s.conn)
        self.now=datetime.now(timezone.utc)
        self.data=parse_account('zheshang',payload())
        self.data.update(cash_cny=20000,cash_source='jd_electronic_account_api',
            quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':self.now.isoformat()},
            pending_order_count=0,pending_orders={'records':[],'total_count':0,'complete':True},
            coverage={'pending_orders':'available'},component_times={'pending_orders':self.now.isoformat()})
        self.snapshot();self.n=Mock();self.n.send.return_value=True;self.n.last_receipt={}
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def snapshot(self):record_success(self.s.conn,'zheshang',self.data,self.now.isoformat())
    def saved(self,at=None,value=None):
        view=self.s.conn.execute('SELECT COALESCE(MAX(view_id),0)+1 FROM jd_live_advice').fetchone()[0]
        save(self.s.conn,'codex',view,{'zheshang':value or hold()},model_input(self.s.conn,self.now),at or self.now)
        self.s.conn.commit();return view
    def events(self,view):return notification_events(self.s.conn,'codex',view)

    def test_daily_hold_then_position_review_quiet_followups_and_no_duplicate_send(self):
        set_subscription(self.s.conn,'codex:live_zheshang',True)
        view=self.saved();events=self.events(view)
        self.assertEqual(len(events),1);self.assertEqual(events[0]['notification_reasons'],['daily_first'])
        self.assertIn('当日首次评估',events[0]['title']);self.assertIn('当日首次真实账户分析',events[0]['body'])
        dispatch(self.s,self.n,events);self.assertEqual(self.events(view),[])
        self.assertEqual(self.events(self.saved()),[])
        self.data.update(total_grams=10,sellable_grams=10,frozen_grams=0,cash_cny=22000);self.snapshot()
        view=self.saved();events=self.events(view)
        self.assertEqual(len(events),1);self.assertEqual(events[0]['notification_reasons'],['position_changed'])
        self.assertIn('仓位变化复评',events[0]['title'])
        dispatch(self.s,self.n,events);dispatch(self.s,self.n,events)
        self.assertEqual(self.n.send.call_count,2)
        self.s.conn.close();self.s=Store(self.path)
        self.assertEqual(self.events(view),[]);self.assertEqual(self.events(self.saved()),[])
        categories=[r[0] for r in self.s.conn.execute('SELECT category FROM notification_deliveries')]
        self.assertEqual(categories,['real_advice','real_advice'])

    def test_daily_and_position_overlap_is_one_event_and_keeps_buy_sell_title(self):
        self.saved(at=self.now-timedelta(days=1))
        self.data.update(total_grams=10,sellable_grams=10,frozen_grams=0);self.snapshot()
        value=hold();value.update(action='buy',grams=1,price_condition='reference',price_cny=850)
        events=self.events(self.saved(value=value))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['notification_reasons'],['daily_first','position_changed'])
        self.assertIn('买入建议',events[0]['title']);self.assertIn('当日首次',events[0]['body'])
        self.assertIn('仓位或确认资金变化',events[0]['body'])

    def test_disabled_subscription_records_without_delivery_or_later_catchup(self):
        events=self.events(self.saved());dispatch(self.s,self.n,events)
        self.n.send.assert_not_called()
        set_subscription(self.s.conn,'codex:live_zheshang',True)
        self.assertEqual(self.events(self.saved()),[])
        dispatch(self.s,self.n,[]);self.n.send.assert_not_called()

    def test_quote_only_snapshot_revision_is_not_a_position_reminder(self):
        self.saved()
        self.data['quote'].update(buy_price_cny=851,sell_price_cny=851);self.snapshot()
        self.assertEqual(self.events(self.saved()),[])
        self.data['average_cost_cny']=1080;self.snapshot()
        self.assertEqual(self.events(self.saved()),[])

    def test_global_validation_stale_account_expiry_and_model_switch_still_block(self):
        value=hold();value.update(action='sell',grams=100)
        self.assertEqual(self.events(self.saved(value=value)),[])
        view=self.saved();self.assertEqual(len(self.events(view)),1)
        with self.s.conn:self.s.conn.execute('UPDATE jd_live_advice SET expires_at=?',((self.now-timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.events(view),[])
        view=self.saved()
        self.s.set_state('model_control:codex',{'enabled':False})
        self.assertEqual(self.events(view),[])
        self.s.set_state('model_control:codex',{'enabled':True})
        self.data['cash_cny']=23000;self.snapshot()
        self.assertEqual(self.events(view),[])
        view=self.saved()
        old=(self.now-timedelta(hours=1)).isoformat()
        with self.s.conn:self.s.conn.execute('UPDATE jd_live_snapshots SET fetched_at=?',(old,))
        self.assertEqual(self.events(view),[])

    def test_failed_milestone_delivery_uses_existing_bounded_retry(self):
        set_subscription(self.s.conn,'codex:live_zheshang',True)
        self.n.send.side_effect=[False,True];self.n.last_receipt={'status':'failed'}
        view=self.saved();dispatch(self.s,self.n,self.events(view),self.now)
        self.assertEqual(self.events(view),[])
        dispatch(self.s,self.n,[],self.now+timedelta(seconds=61))
        self.assertEqual(self.n.send.call_count,2)
        self.assertEqual(tuple(self.s.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone()),('accepted',2))
