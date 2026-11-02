import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch

from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,model_input,accounts
from monitor.jd_live_advice import LiveAdvice,save,notification_events
from monitor.jd_live_review import request_reviews
from monitor.ai_events import read,acknowledge
from monitor.notification_control import set_subscription
from monitor.__main__ import Monitor
from test_jd_live import payload,advice

NOW=datetime.now(timezone.utc)
KEY='a'*24


class StructuredAdviceTests(unittest.TestCase):
    def setUp(self):
        market=patch('monitor.trading_hours.trading_session',return_value={'is_open':True})
        market.start();self.addCleanup(market.stop)
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
        self.s=Store(self.path);ensure_schema(self.s.conn)
        self.s.set_state('ai_status:codex',{'available':True})
        self.s.set_state('model_control:claude',{'enabled':False,'generation':0,'changed_at':None})
        self.order={'order_key':KEY,'action':'sell','kind':'limit_sell','grams':5.,'price_cny':850.,
                    'price_condition':'at_or_above','status':'pending','created_at':(NOW-timedelta(minutes=2)).isoformat(),
                    'expires_at':(NOW+timedelta(days=1)).isoformat(),'filled_grams':None}
        self.data=parse_account('zheshang',payload());self.data.update(cash_cny=100.,
            quote={'buy_price_cny':855.,'sell_price_cny':855.,'quoted_at':NOW.isoformat()},
            pending_order_count=1,pending_orders={'records':[self.order],'total_count':1,'complete':True},
            coverage={'pending_orders':'available'},component_times={'pending_orders':NOW.isoformat()})
        record_success(self.s.conn,'zheshang',self.data,NOW.isoformat())
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def value(self,action='keep',**changes):
        value=advice();value.update(action='hold',grams=None,order_management=[{'order_key':KEY,'action':action,'replacement':None,'reason':'处理现有单'}],review_conditions=[])
        value.update(changes);return value
    def condition(self,**changes):
        result=dict(condition='at_or_below',price_cny=850.,quote_side='sell',order_key=None,reason='达到阈值复评')
        result.update(changes);return result
    def saved(self,value=None,now=NOW):
        view=self.s.conn.execute('SELECT COALESCE(MAX(view_id),0)+1 FROM jd_live_advice').fetchone()[0]
        save(self.s.conn,'codex',view,{'zheshang':value or self.value()},model_input(self.s.conn,now),now=now)
        self.s.conn.commit();return view
    def latest(self,now=NOW):return accounts(self.s.conn,now)[0]['advice'][0]
    def price(self,sell,seconds,buy=None):
        self.data=copy.deepcopy(self.data)
        self.data['quote']={'buy_price_cny':sell if buy is None else buy,'sell_price_cny':sell,'quoted_at':(NOW+timedelta(seconds=seconds)).isoformat()}
        record_success(self.s.conn,'zheshang',self.data,NOW.isoformat())
    def price_events(self):return [e for e in read(self.s.conn,'codex')['pending'] if e['id'].startswith('live-price:')]
    def cover(self):
        with self.s.conn:acknowledge(self.s.conn,'codex',[e['id'] for e in read(self.s.conn,'codex')['pending']])

    def test_legacy_output_and_numeric_schema_boundaries(self):
        from monitor.jd_live_conditions import stamp
        self.assertIsNone(stamp(None));self.assertIsNotNone(stamp(NOW.isoformat().replace('+00:00','Z')))
        value=LiveAdvice.model_validate(advice())
        self.assertEqual(value.order_management,[]);self.assertEqual(value.review_conditions,[])
        for bad in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):LiveAdvice.model_validate(self.value(review_conditions=[{'condition':'at_or_below','price_cny':bad,'reason':'test'}]))
        with self.assertRaises(ValueError):LiveAdvice.model_validate(self.value(order_management=[{'order_key':'raw-bank-id','action':'cancel','reason':'test'}]))

    def test_keep_and_replace_preserve_original_values_and_validate(self):
        self.saved();self.assertEqual(self.latest()['order_management'][0]['state'],'current')
        value=self.value('replace');value['order_management'][0]['replacement']={'price_cny':848.}
        self.saved(value);entry=self.latest();item=entry['order_management'][0]
        self.assertEqual(entry['checks'],[]);self.assertEqual(item['input_order']['price_cny'],850)
        self.assertEqual(item['effective_replacement']['grams'],5)
        self.assertEqual(item['effective_replacement']['expires_at'],self.order['expires_at'])
        self.assertEqual(item['effective_replacement']['price_cny'],848)

    def test_invalid_replacement_unknown_target_and_omitted_order_are_flagged(self):
        for replacement in (None,{}, {'price_cny':850}, {'grams':100}, {'expires_at':'2020-01-01'}, {'expires_at':(NOW-timedelta(hours=1)).isoformat()}):
            value=self.value('replace');value['order_management'][0]['replacement']=replacement
            self.saved(value);self.assertEqual(self.latest()['status'],'review_required')
        value=self.value('cancel');value['order_management'][0]['order_key']='b'*24
        self.saved(value);self.assertEqual(self.latest()['status'],'review_required')
        self.saved(self.value(order_management=[]));self.assertTrue(self.latest()['checks'])

    def test_price_hit_exact_threshold_is_persistent_and_read_only_gets_do_not_trigger(self):
        self.saved(self.value(review_conditions=[self.condition()]))
        request_reviews(self.s,NOW);self.assertFalse(self.price_events())
        self.price(850,10)
        self.latest(NOW+timedelta(seconds=10));self.assertFalse(self.price_events())
        request_reviews(self.s,NOW+timedelta(seconds=10));request_reviews(self.s,NOW+timedelta(seconds=11))
        events=self.price_events();self.assertEqual(len(events),1)
        self.assertEqual(events[0]['data']['bank'],'zheshang');self.assertEqual(events[0]['data']['price_cny'],850)
        self.assertEqual(self.latest(NOW+timedelta(seconds=11))['review_conditions'][0]['monitoring']['status'],'queued')
        self.s.conn.close();self.s=Store(self.path)
        request_reviews(self.s,NOW+timedelta(seconds=12));self.assertEqual(len(self.price_events()),1)

    def test_cross_round_latch_prevents_loops_and_rearms_after_price_recovers(self):
        value=self.value(review_conditions=[self.condition()]);self.saved(value)
        self.price(850,10);request_reviews(self.s,NOW+timedelta(seconds=10));self.cover()
        self.saved(value,NOW+timedelta(seconds=20));self.price(849,30)
        request_reviews(self.s,NOW+timedelta(seconds=30));self.assertFalse(self.price_events())
        self.assertEqual(self.latest(NOW+timedelta(seconds=30))['review_conditions'][0]['monitoring']['status'],'latched')
        self.price(851,40);request_reviews(self.s,NOW+timedelta(seconds=40))
        self.price(850,50);request_reviews(self.s,NOW+timedelta(seconds=50));self.assertEqual(len(self.price_events()),1)

    def test_only_selected_bank_quote_side_is_used(self):
        condition=self.condition();condition.update(condition='at_or_above',quote_side='buy')
        self.data['quote'].update(buy_price_cny=840,sell_price_cny=860)
        record_success(self.s.conn,'zheshang',self.data,NOW.isoformat());self.saved(self.value(review_conditions=[condition]))
        request_reviews(self.s,NOW);self.assertFalse(self.price_events())
        self.price(840,10,buy=850);request_reviews(self.s,NOW+timedelta(seconds=10));self.assertEqual(len(self.price_events()),1)
        self.assertEqual(self.price_events()[0]['data']['quote_side'],'buy')

    def test_stale_quote_changed_account_and_closed_session_do_not_trigger(self):
        self.saved(self.value(review_conditions=[self.condition()]));self.price(849,10)
        request_reviews(self.s,NOW+timedelta(seconds=200));self.assertFalse(self.price_events())
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':False}):request_reviews(self.s,NOW+timedelta(seconds=10))
        self.assertFalse(self.price_events())
        self.data['cash_cny']=101;record_success(self.s.conn,'zheshang',self.data,NOW.isoformat())
        request_reviews(self.s,NOW+timedelta(seconds=11));self.assertFalse(self.price_events())

    def test_order_specific_condition_pauses_when_order_disappears(self):
        c=self.condition(order_key=KEY);self.saved(self.value(review_conditions=[c]))
        self.data['pending_orders']['records']=[];self.data['pending_orders']['total_count']=0;self.data['pending_order_count']=0
        record_success(self.s.conn,'zheshang',self.data,NOW.isoformat());self.price(849,10)
        request_reviews(self.s,NOW+timedelta(seconds=10));self.assertFalse(self.price_events())

    def test_expiry_of_hold_with_price_conditions_schedules_one_review(self):
        self.saved(self.value(review_conditions=[self.condition()],valid_for_minutes=15))
        now=NOW+timedelta(minutes=16);self.price(855,16*60)
        self.data['component_times']['pending_orders']=now.isoformat()
        record_success(self.s.conn,'zheshang',self.data,now.isoformat())
        request_reviews(self.s,now);request_reviews(self.s,now)
        pending=read(self.s.conn,'codex')['pending'];self.assertEqual(len(pending),1)
        self.assertIn('到期复评',pending[0]['reason']);self.assertFalse(self.price_events())

    def test_old_schema_upgrade_is_queued_once_without_parsing_legacy_risk_text(self):
        self.saved();row=self.s.conn.execute('SELECT id,advice_json FROM jd_live_advice').fetchone();value=json.loads(row[1])
        for k in ('schema_version','order_management','review_conditions'):value.pop(k,None)
        value['risk_note']='跌到830.59再复评'
        with self.s.conn:self.s.conn.execute('UPDATE jd_live_advice SET advice_json=? WHERE id=?',(json.dumps(value),row[0]))
        request_reviews(self.s,NOW);request_reviews(self.s,NOW)
        pending=read(self.s.conn,'codex')['pending'];self.assertEqual(len(pending),1)
        self.assertTrue(pending[0]['id'].startswith('live-schema-v2:'));self.assertFalse(self.price_events())

    def test_hold_with_cancel_or_replace_notifies_and_keep_only_is_quiet(self):
        set_subscription(self.s.conn,'codex:live_zheshang',True)
        view=self.saved();self.assertEqual(notification_events(self.s.conn,'codex',view),[])
        view=self.saved(self.value('cancel'));events=notification_events(self.s.conn,'codex',view)
        self.assertEqual(len(events),1);self.assertIn('撤单建议',events[0]['title'])
        m=object.__new__(Monitor);m.store=self.s;m.notifier=Mock();m.notifier.send.return_value=True;m.dispatch(events)
        view=self.saved(self.value('cancel'));self.assertEqual(notification_events(self.s.conn,'codex',view),[])
        value=self.value('replace',review_conditions=[self.condition()]);value['order_management'][0]['replacement']={'price_cny':848.}
        view=self.saved(value);events=notification_events(self.s.conn,'codex',view)
        self.assertIn('改单建议',events[0]['title']);self.assertIn('修改后',events[0]['body']);self.assertIn('价格复评',events[0]['body'])
        view=self.saved();events=notification_events(self.s.conn,'codex',view)
        self.assertIn('挂单处理建议更新',events[0]['title'])

    def test_invalid_order_management_never_produces_a_manual_action_notification(self):
        value=self.value('cancel');value['order_management'][0]['order_key']='b'*24
        view=self.saved(value);self.assertEqual(notification_events(self.s.conn,'codex',view),[])

    def test_model_input_keeps_compact_structured_previous_advice(self):
        self.saved(self.value(review_conditions=[self.condition()]))
        previous=model_input(self.s.conn,NOW)['accounts']['zheshang']['previous_advice'][0]
        self.assertEqual(previous['order_management'][0]['action'],'keep')
        self.assertEqual(previous['review_conditions'][0]['price_cny'],850)
        self.assertNotIn('input_order',previous['order_management'][0])

    def test_partial_fill_replacement_uses_remaining_quantity_and_overcommit_is_flagged(self):
        self.order.update(status='partially_filled',filled_grams=2.)
        record_success(self.s.conn,'zheshang',self.data,NOW.isoformat())
        value=self.value('replace');value['order_management'][0]['replacement']={'price_cny':848}
        self.saved(value);item=self.latest()['order_management'][0]
        self.assertEqual(item['effective_replacement']['grams'],3)
        self.assertFalse(item['checks'])
        value=self.value(action='keep',grams=10.)
        value['action']='sell';self.saved(value)
        self.assertTrue(any('合计超过' in c for c in self.latest()['checks']))

    def test_duplicate_conditions_and_unknown_condition_order_are_not_activated(self):
        self.saved(self.value(review_conditions=[self.condition(),self.condition()]))
        self.assertEqual(self.latest()['status'],'review_required')
        self.price(849,10);request_reviews(self.s,NOW+timedelta(seconds=10));self.assertFalse(self.price_events())
        self.saved(self.value(review_conditions=[self.condition(order_key='b'*24)]))
        self.assertEqual(self.latest()['status'],'review_required')

    def test_disabled_model_has_no_price_events_and_reenable_uses_new_generation(self):
        from monitor.controls import set_enabled
        value=self.value(review_conditions=[self.condition()]);self.saved(value)
        set_enabled(self.s.conn,'codex',False);self.price(849,10)
        request_reviews(self.s,NOW+timedelta(seconds=10));self.assertFalse(self.price_events())
        set_enabled(self.s.conn,'codex',True)
        reopened=max(NOW+timedelta(seconds=20),datetime.now(timezone.utc)+timedelta(seconds=1))
        self.price(849,(reopened-NOW).total_seconds())
        self.saved(value,reopened)
        request_reviews(self.s,reopened);self.assertEqual(len(self.price_events()),1)

    def test_collector_and_monitor_race_preserves_one_event_and_its_trigger_state(self):
        import threading
        from monitor.jd_live_conditions import monitor_prices
        self.saved(self.value(review_conditions=[self.condition()]))
        self.price(850,10);now=NOW+timedelta(seconds=10)
        account=accounts(self.s.conn,now)[0];value=account['advice'][0]
        barrier=threading.Barrier(2);errors=[]
        def run():
            store=Store(self.path)
            try:
                barrier.wait(timeout=5);monitor_prices(store,account,value,now)
            except Exception as error:errors.append(error)
            finally:store.conn.close()
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads));self.assertEqual(errors,[])
        self.assertEqual(len(self.price_events()),1)
        state=self.latest(now)['review_conditions'][0]['monitoring']
        self.assertEqual(state['status'],'queued');self.assertIsNotNone(state['triggered_at'])
