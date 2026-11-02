import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch
from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,model_input,accounts,PRODUCTS
from monitor.jd_live_advice import save
from monitor.jd_live_orders import parse_list,parse_order,record,progress
from monitor.jd_live_review import request_reviews
from monitor.ai_events import read,acknowledge
from monitor.jd_live_sync import refresh,ReadonlyClient
from monitor.notification_control import set_subscription
from monitor.notification_outbox import dispatch
from test_jd_live import payload,advice

NOW=datetime(2026,11,2,2,tzinfo=timezone.utc)


def raw_order(**changes):
    return dict(productSku=PRODUCTS['zheshang'][0],uuid='unit-private-order',jrid='unit-private-person',
                ruleType=2,status=1,orderGram=5,orderPrice=850,createdTime=int((NOW+timedelta(minutes=1)).timestamp()*1000),
                deadLineTime=int((NOW+timedelta(days=1)).timestamp()*1000),**changes)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'test.db');ensure_schema(self.s.conn)
        self.s.set_state('ai_status:codex',{'available':True})
        self.s.set_state('model_control:claude',{'enabled':False})
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def snapshot(self,bank='zheshang',**changes):
        d=parse_account(bank,payload(bank));d.update(quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':NOW.isoformat()},pending_order_count=0,cash_cny=10)
        d.update(changes);record_success(self.s.conn,bank,d,NOW.isoformat());return d
    def saved(self,action='sell',expiry=None):
        d=advice();d['action']=action
        view_id=self.s.conn.execute('SELECT COALESCE(MAX(view_id),0)+1 FROM jd_live_advice').fetchone()[0]
        save(self.s.conn,'codex',view_id,{'zheshang':d},model_input(self.s.conn,NOW),now=NOW)
        if expiry:self.s.conn.execute('UPDATE jd_live_advice SET expires_at=?',(expiry.isoformat(),))
        self.s.conn.commit()
    def clear_events(self):
        with self.s.conn:acknowledge(self.s.conn,'codex',[e['id'] for e in read(self.s.conn,'codex')['pending']])

    def test_order_redaction_matching_and_adjusted_parameters(self):
        o=parse_order(raw_order(),'zheshang')
        self.assertNotIn('unit-private',json.dumps(o));self.assertEqual(o['status'],'pending')
        p=progress(advice(),NOW.isoformat(),[o]);self.assertEqual(p['match'],'exact_parameters')
        changed={**o,'price_cny':850.4,'grams':4.98}
        self.assertEqual(progress(advice(),NOW.isoformat(),[changed])['match'],'similar_parameters')
        self.assertEqual(progress(advice(),NOW.isoformat(),[o,changed])['status'],'ambiguous')
        self.assertEqual(progress(advice(),(NOW+timedelta(minutes=2)).isoformat(),[o])['status'],'not_observed')
        with self.assertRaises(ValueError):parse_order(raw_order(),'minsheng')

    def test_pending_change_triggers_once_even_after_advice_expiry(self):
        self.snapshot();self.saved(expiry=NOW-timedelta(seconds=1))
        self.snapshot(pending_order_count=1)
        request_reviews(self.s,NOW);request_reviews(self.s,NOW)
        events=read(self.s.conn,'codex')['pending'];self.assertEqual(len(events),1)
        self.assertIn('条件单数量',events[0]['reason'])
        self.clear_events();request_reviews(self.s,NOW)
        self.assertEqual(read(self.s.conn,'codex')['pending'],[])

    def test_return_to_previous_account_state_is_a_new_event(self):
        self.snapshot();self.saved();self.snapshot(pending_order_count=1)
        request_reviews(self.s,NOW);self.clear_events();self.saved()
        self.snapshot(pending_order_count=0);request_reviews(self.s,NOW)
        self.assertEqual(len(read(self.s.conn,'codex')['pending']),1)

    def test_collection_batches_events_before_main_loop_review(self):
        self.snapshot();self.saved();self.snapshot(pending_order_count=1)
        self.s.set_state('jd_live_sync_status',{'status':'running','at':NOW.isoformat()})
        request_reviews(self.s,NOW);self.assertEqual(read(self.s.conn,'codex')['pending'],[])
        request_reviews(self.s,NOW,after_sync=True)
        self.assertEqual(len(read(self.s.conn,'codex')['pending']),1)

    def test_quote_recovery_and_actionable_expiry_not_hold_polling(self):
        old_quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':(NOW-timedelta(days=1)).isoformat()}
        self.snapshot(quote=old_quote);self.saved(action='wait');request_reviews(self.s,NOW)
        self.assertFalse(read(self.s.conn,'codex')['pending'])
        self.snapshot();request_reviews(self.s,NOW);request_reviews(self.s,NOW)
        self.assertEqual(len(read(self.s.conn,'codex')['pending']),1)
        self.assertIn('报价恢复',read(self.s.conn,'codex')['pending'][0]['reason']);self.clear_events()
        self.saved(action='sell',expiry=NOW-timedelta(seconds=1));request_reviews(self.s,NOW)
        self.assertIn('到期复评',read(self.s.conn,'codex')['pending'][0]['reason'])

    def test_pending_order_uses_its_own_expiry_and_is_present_in_model_input(self):
        self.snapshot();self.saved(expiry=NOW-timedelta(seconds=1))
        order=parse_order(raw_order(),'zheshang');record(self.s.conn,'zheshang',[order],NOW.isoformat())
        request_reviews(self.s,NOW)
        self.assertEqual(read(self.s.conn,'codex')['pending'],[])
        context=model_input(self.s.conn,NOW)
        self.assertEqual(context['accounts']['zheshang']['previous_advice'][0]['execution_progress']['status'],'pending')
        self.assertNotIn('unit-private',json.dumps(context))

    def test_quotes_do_not_refresh_account_age_or_create_trade_events(self):
        self.snapshot();self.saved(action='hold')
        client=Mock();client.query.return_value={'resultData':{'code':'00000000','data':{'productSku':PRODUCTS['zheshang'][0],'buyPrice':851,'sellPrice':851,'time':NOW.timestamp()*1000}}}
        def query(op,bank):
            return {'resultData':{'code':'00000000','data':{'productSku':PRODUCTS[bank][0],'buyPrice':851,'sellPrice':851,'time':NOW.timestamp()*1000}}}
        # Both banks already have independent account snapshots.
        self.snapshot('minsheng');client.query.side_effect=query
        refresh(self.s,client,{'quote'},NOW+timedelta(minutes=1))
        self.assertEqual([c.args[0] for c in client.query.call_args_list],['quote','quote'])
        self.assertEqual(accounts(self.s.conn,NOW+timedelta(minutes=1))[0]['fetched_at'],NOW.isoformat())

    def test_order_states_are_provider_confirmed_and_partial_fill_is_explicit(self):
        for code,state in ((2,'filled'),(3,'expired'),(4,'cancelled'),(5,'failed')):
            r=raw_order();r['status']=code;self.assertEqual(parse_order(r,'zheshang')['status'],state)
        r=raw_order();r['filledGram']=2
        self.assertEqual(parse_order(r,'zheshang')['status'],'partially_filled')
        part,_=parse_list({'resultData':{'code':'00000000','data':{'total':0}}},'zheshang')
        self.assertTrue(part['complete']);self.assertEqual(part['records'],[])

    def test_official_history_joins_orders_and_does_not_invent_a_fill(self):
        from monitor.jd_live_orders import trade_history
        row={'productSku':PRODUCTS['zheshang'][0],'jdorderId':'unit-private-order','isConditional':1,
             'leftTitile1':'限价卖出订单过期','leftTitile2':'2026-11-02 10:01:00','rightTitile2':'5克','orderPrice':850}
        wrapper={'resultData':{'status':'SUCCESS','errorCode':'00000','datas':[row]}}
        result=trade_history(wrapper,'zheshang')[0]
        self.assertEqual(result['order_key'],parse_order(raw_order(),'zheshang')['order_key'])
        self.assertEqual(result['status'],'expired');self.assertNotIn('unit-private',json.dumps(result))
        row['leftTitile1']='限价卖出未成交';self.assertEqual(trade_history(wrapper,'zheshang')[0]['status'],'unknown')
        with self.assertRaises(ValueError):trade_history(wrapper,'minsheng')

    def test_latest_hold_does_not_hide_previous_pending_sell(self):
        self.snapshot();self.saved();record(self.s.conn,'zheshang',[parse_order(raw_order(),'zheshang')],NOW.isoformat())
        self.saved(action='hold')
        account=model_input(self.s.conn,NOW)['accounts']['zheshang']
        self.assertEqual(len(account['previous_advice']),2)
        self.assertEqual(account['previous_advice'][0]['action'],'hold')
        self.assertEqual(account['previous_advice'][1]['execution_progress']['status'],'pending')


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'test.db')
        set_subscription(self.s.conn,'codex:analysis',True)
        self.n=Mock();self.n.last_receipt={'channel':'pushplus','status':'failed'}
        self.e={'key':'ai:codex:unit','notification_topic':'codex:analysis','title':'unit','body':'unit'}
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def status(self):return tuple(self.s.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone())
    def test_persistent_retry_success_and_dedup(self):
        self.n.send.side_effect=[False,True]
        dispatch(self.s,self.n,[self.e],NOW);self.assertEqual(self.status(),('retry',1))
        dispatch(self.s,self.n,[],NOW+timedelta(seconds=30));self.assertEqual(self.n.send.call_count,1)
        dispatch(self.s,self.n,[],NOW+timedelta(seconds=61));self.assertEqual(self.status(),('accepted',2))
        dispatch(self.s,self.n,[self.e],NOW+timedelta(minutes=5));self.assertEqual(self.n.send.call_count,2)
        self.assertEqual(self.s.conn.execute('SELECT delivered FROM alerts').fetchone()[0],1)
    def test_maximum_three_attempts(self):
        self.n.send.return_value=False
        for seconds in (0,61,242,500):dispatch(self.s,self.n,[self.e] if seconds==0 else [],NOW+timedelta(seconds=seconds))
        self.assertEqual(self.n.send.call_count,3);self.assertEqual(self.status(),('failed',3))
    def test_uncertain_never_retries_and_crash_remains_uncertain(self):
        self.n.send.return_value=False;self.n.last_receipt['status']='uncertain'
        dispatch(self.s,self.n,[self.e],NOW);dispatch(self.s,self.n,[],NOW+timedelta(minutes=2))
        self.assertEqual(self.n.send.call_count,1);self.assertEqual(self.status(),('uncertain',1))
        with self.s.conn:self.s.conn.execute("UPDATE notification_outbox SET status='sending'")
        dispatch(self.s,self.n,[],NOW+timedelta(minutes=3));self.assertEqual(self.status(),('uncertain',1))
    def test_subscription_or_expiry_cancels_retry(self):
        self.n.send.return_value=False;dispatch(self.s,self.n,[self.e],NOW)
        set_subscription(self.s.conn,'codex:analysis',False)
        dispatch(self.s,self.n,[],NOW+timedelta(seconds=61));self.assertEqual(self.status(),('cancelled',1))

    def test_expired_or_disabled_model_cancels_retry(self):
        self.n.send.return_value=False;dispatch(self.s,self.n,[self.e],NOW)
        dispatch(self.s,self.n,[],NOW+timedelta(minutes=16));self.assertEqual(self.status(),('cancelled',1))
        with self.s.conn:self.s.conn.execute('DELETE FROM notification_outbox')
        dispatch(self.s,self.n,[{**self.e,'key':'ai:codex:other'}],NOW)
        self.s.set_state('model_control:codex',{'enabled':False})
        dispatch(self.s,self.n,[],NOW+timedelta(minutes=2));self.assertEqual(self.status(),('cancelled',1))
