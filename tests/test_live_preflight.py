import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema, parse_account, record_success, accounts, model_input, PRODUCTS
from monitor.jd_live_preflight import prepare, missing_inputs
from monitor.jd_live_sync import refresh, sync_once, BrowserBusy, request_refresh
from monitor.runtime_resources import slot
from monitor.jd_live_advice import save
from monitor.jd_live_review import request_reviews
from monitor.ai_events import read, acknowledge
from test_jd_live import payload
from test_live_lifecycle import raw_order
from monitor.jd_live_orders import parse_order

NOW=datetime.now(timezone.utc)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.db'
        self.s=Store(self.path);ensure_schema(self.s.conn)
        self.s.set_state('ai_status:codex',{'available':True})
        self.s.set_state('model_control:claude',{'enabled':False})
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def populate(self,**changes):
        for bank in ('zheshang','minsheng'):
            data=parse_account(bank,payload(bank))
            data.update(cash_cny=10,cash_source='jd_electronic_account_api',
                        quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':NOW.isoformat()},
                        pending_order_count=0,pending_orders={'records':[],'total_count':0,'complete':True},
                        coverage={'pending_orders':'available','cash_balance':'available'},
                        component_times={k:NOW.isoformat() for k in ('account','cash_balance','pending_orders','quote')})
            data.update(changes);record_success(self.s.conn,bank,data,NOW.isoformat())
    def fail_orders(self):
        self.populate(coverage={'pending_orders':'unavailable:OperationalError','cash_balance':'available'})
    def completed(self,status='completed'):
        r=self.s.get_state('jd_live_refresh');self.s.set_state('jd_live_refresh',{**r,'status':status,'finished_at':NOW.isoformat()})
        self.s.set_state('jd_live_sync_status',{'status':status,'at':NOW.isoformat()})

    def test_no_accounts_and_short_cache_never_start_browser_or_request(self):
        with patch('monitor.jd_live_preflight.request_refresh') as request:
            self.assertEqual(prepare(self.s)['status'],'not_configured')
            self.populate();self.assertEqual(prepare(self.s)['status'],'ready')
        request.assert_not_called()

    def test_refresh_finishes_before_input_and_uses_only_critical_components(self):
        self.fail_orders();steps=[]
        def collector(_seconds):
            r=self.s.get_state('jd_live_refresh');steps.append(r)
            self.populate();self.completed()
        with patch('monitor.jd_live_preflight.time.sleep',side_effect=collector):
            result=prepare(self.s,wait_seconds=2)
        self.assertEqual(result['status'],'ready');self.assertTrue(result['can_analyze'])
        self.assertEqual(len(steps),1);self.assertEqual(set(steps[0]['components']),{'core','quote','orders'})
        self.assertEqual(steps[0]['source'],'analysis')
        self.assertTrue(all(a['pending_orders']['complete'] for a in model_input(self.s.conn)['accounts'].values()))
        with patch('monitor.jd_live_preflight.request_refresh') as request:
            self.assertEqual(prepare(self.s)['status'],'ready')
        request.assert_not_called()

    def test_timeout_defers_ai_while_collection_may_still_own_chrome(self):
        self.fail_orders();self.s.set_state('jd_live_sync_status',{'status':'running','at':NOW.isoformat()})
        result=prepare(self.s,wait_seconds=0)
        self.assertEqual(result['status'],'deferred');self.assertFalse(result['can_analyze'])
        self.assertEqual(self.s.get_state('jd_live_refresh')['status'],'queued')

    def test_unresponsive_collector_does_not_stop_simulation_indefinitely(self):
        self.fail_orders();result=prepare(self.s,wait_seconds=0)
        self.assertEqual(result['status'],'degraded');self.assertTrue(result['can_analyze'])
        self.assertIn('orders',result['missing']['zheshang'])

    def test_failure_is_explicit_and_cooldown_anchor_does_not_slide(self):
        self.fail_orders()
        with patch('monitor.jd_live_preflight.time.sleep',side_effect=lambda _:self.completed('unavailable')):
            result=prepare(self.s,wait_seconds=2)
        self.assertEqual(result['status'],'degraded');self.assertIn('orders',result['missing']['zheshang'])
        with patch('monitor.jd_live_preflight.request_refresh') as request:
            again=prepare(self.s)
        request.assert_not_called();self.assertEqual(result['checked_at'],again['checked_at'])

    def test_resources_wait_is_a_defer_not_permission_to_run_model(self):
        self.fail_orders()
        with patch('monitor.jd_live_preflight.time.sleep',side_effect=lambda _:self.completed('waiting_for_resources')):
            self.assertFalse(prepare(self.s,wait_seconds=2)['can_analyze'])

    def test_newer_manual_refresh_can_satisfy_existing_wait(self):
        self.fail_orders()
        def collector(_):
            old=self.s.get_state('jd_live_refresh')
            newer=(datetime.fromisoformat(old['requested_at'])+timedelta(seconds=1)).isoformat()
            self.populate();self.s.set_state('jd_live_refresh',{**old,'requested_at':newer,'status':'completed'})
        with patch('monitor.jd_live_preflight.time.sleep',side_effect=collector):
            self.assertEqual(prepare(self.s,wait_seconds=2)['status'],'ready')

    def test_manual_refresh_upgrades_queued_analysis_request_without_losing_history(self):
        first=request_refresh(self.s.conn,components=('core','quote','orders'),source='analysis')
        full=request_refresh(self.s.conn)
        self.assertEqual(full['source'],'manual');self.assertNotIn('components',full)
        self.assertGreaterEqual(full['requested_at'],first['requested_at'])
        self.assertEqual(request_refresh(self.s.conn,components=('core','quote','orders'),source='analysis'),full)

    def test_timestamps_are_per_component_completion_not_entire_batch_start(self):
        self.populate();clock=[NOW]
        def query(op,bank):
            clock[0]+=timedelta(seconds=10)
            if op in ('indexData','indexDataStd'):return payload(bank)
            if op=='cash_balance':
                return {'resultData':{'errorCode':'00000','datas':{'availBalance':10}}} if bank=='zheshang' else {'resultData':{'datas':{'code':'DS00000','availableBalance':10}}}
            raise ValueError('optional component not provided')
        client=Mock();client.query.side_effect=query
        with patch('monitor.jd_live_sync.datetime') as dt:
            dt.now.side_effect=lambda *a:clock[0]
            refresh(self.s,client,{'core'})
        raw=json.loads(self.s.conn.execute("SELECT data_json FROM jd_live_snapshots WHERE bank='zheshang'").fetchone()[0])
        self.assertGreater(datetime.fromisoformat(raw['component_times']['cash_balance']),datetime.fromisoformat(raw['component_times']['account']))
        self.assertGreater(datetime.fromisoformat(raw['component_times']['account']),NOW)

    def test_model_slot_blocks_cookie_recovery_and_excludes_another_model(self):
        self.populate();client=Mock();client.session=None
        with slot(self.path) as acquired:
            self.assertTrue(acquired)
            with slot(self.path) as second:self.assertFalse(second)
            with self.assertRaises(BrowserBusy):sync_once(self.s,client)
        client.recover.assert_not_called()
        with slot(self.path) as after:self.assertTrue(after)

    def test_wal_allows_reader_snapshot_and_collector_commit_together(self):
        self.assertEqual(self.s.conn.execute('PRAGMA journal_mode').fetchone()[0],'wal')
        reader=sqlite3.connect(f'file:{self.path}?mode=ro',uri=True,timeout=.1)
        writer=Store(self.path)
        try:
            reader.execute('BEGIN');reader.execute('SELECT * FROM state').fetchall()
            writer.set_state('parallel_commit',True)
            self.assertIsNone(reader.execute("SELECT value FROM state WHERE key='parallel_commit'").fetchone())
            reader.rollback()
            self.assertIsNotNone(reader.execute("SELECT value FROM state WHERE key='parallel_commit'").fetchone())
        finally:reader.close();writer.conn.close()

    def test_db_failure_retains_previous_order_list_and_distinguishes_freshness(self):
        self.populate();client=Mock()
        def query(op,bank):
            if op=='pending_orders':
                order=raw_order();order['productSku']=PRODUCTS[bank][0]
                return {'resultData':{'code':'00000000','data':{'total':1,'dataList':[order]}}}
            raise ValueError('unavailable test component')
        client.query.side_effect=query
        with patch('monitor.jd_live_orders.record',side_effect=sqlite3.OperationalError('locked in fixture')):
            refresh(self.s,client,{'orders'},NOW)
        for a in accounts(self.s.conn,NOW,include_advice=False):
            self.assertEqual(a['pending_orders']['records'],[])
            self.assertEqual(a['pending_orders']['freshness_reason'],'refresh_failed')
            self.assertFalse(a['pending_orders']['complete'])

    def test_order_recovery_reviews_old_stale_advice_once_without_erasing_warnings(self):
        order=parse_order(raw_order(),'zheshang')
        self.fail_orders()
        data={'action':'hold','valid_for_minutes':120,'reason':'keep exposure','risk_note':'test',
              'order_management':[{'order_key':order['order_key'],'action':'keep','reason':'retain'}]}
        # An old snapshot still contains the order, even though the last refresh failed.
        self.populate(pending_order_count=1,pending_orders={'records':[order],'total_count':1,'complete':True},
                      coverage={'pending_orders':'unavailable:OperationalError','cash_balance':'available'})
        save(self.s.conn,'codex',1,{'zheshang':data},model_input(self.s.conn,NOW),NOW);self.s.conn.commit()
        old_revision=accounts(self.s.conn,NOW)[0]['revision']
        self.populate(pending_order_count=1,pending_orders={'records':[order],'total_count':1,'complete':True})
        self.assertEqual(accounts(self.s.conn,NOW)[0]['revision'],old_revision)
        self.s.set_state('live-data-ready:codex:zheshang',True)
        request_reviews(self.s,NOW);request_reviews(self.s,NOW)
        events=[e for e in read(self.s.conn,'codex')['pending'] if e['id'].startswith('live-orders-recovered:')]
        self.assertEqual(len(events),1)
        with self.s.conn:acknowledge(self.s.conn,'codex',[e['id'] for e in events])
        request_reviews(self.s,NOW)
        self.assertFalse(any(e['id'].startswith('live-orders-recovered:') for e in read(self.s.conn,'codex')['pending']))
        self.assertIn('挂单明细不是新鲜数据',' '.join(accounts(self.s.conn,NOW)[0]['advice'][0]['checks']))


class WorkerPreflightTests(unittest.TestCase):
    def setUp(self):
        from test_risk_accounts import RiskAccountTests
        self.f=RiskAccountTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_preflight_runs_once_before_shared_context_and_both_models(self):
        f=self.f;sequence=[]
        f.m.analysts={name:Mock() for name in ('codex','claude')}
        for name,a in f.m.analysts.items():a.analyze.side_effect=lambda *args,n=name,**kwargs:sequence.append(n)
        def prep(store):sequence.append('refresh');return {'status':'ready','can_analyze':True,'waited_seconds':0}
        def context(*args):sequence.append('input');return {'xauusd':4000}
        with patch('monitor.jd_live_preflight.prepare',side_effect=prep) as prepare_mock,patch.object(f.m,'ai_context',side_effect=context):
            f.m._ai_worker(f.snap,{}, {'codex':'test','claude':'test'})
        self.assertEqual(sequence,['refresh','input','codex','claude']);self.assertEqual(prepare_mock.call_count,1)
    def test_deferred_refresh_consumes_no_model_round_or_manual_request(self):
        f=self.f
        from monitor.manual_ai import request,TRIGGER
        request(f.m.store.conn,f.settings,'codex')
        f.m.analysts={'codex':Mock()}
        before=f.m.store.conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0]
        with patch('monitor.jd_live_preflight.prepare',return_value={'status':'deferred','can_analyze':False}),patch.object(f.m,'ai_context') as ctx:
            f.m._ai_worker(f.snap,{}, {'codex':TRIGGER})
        ctx.assert_not_called();f.m.analysts['codex'].analyze.assert_not_called()
        self.assertEqual(f.m.store.conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0],before)
        self.assertEqual(f.m.store.get_state('ai_manual:codex')['status'],'queued')

    def test_failed_preflight_marks_only_affected_real_input_unverified(self):
        f=self.f;captured=[]
        f.m.analysts={'codex':Mock()};f.m.analysts['codex'].analyze.side_effect=lambda ctx,*a,**k:captured.append(ctx)
        context={'xauusd':4000,'live_readonly_accounts':{'accounts':{'zheshang':{'status':'fresh','previous_advice':[]},'minsheng':{'status':'fresh','previous_advice':[]}}}}
        result={'status':'degraded','can_analyze':True,'waited_seconds':0,'missing':{'zheshang':['orders']}}
        with patch('monitor.jd_live_preflight.prepare',return_value=result),patch.object(f.m,'ai_context',return_value=context):
            f.m._ai_worker(f.snap,{}, {'codex':'test'})
        self.assertEqual(captured[0]['live_readonly_accounts']['accounts']['zheshang']['status'],'stale')
        self.assertEqual(captured[0]['live_readonly_accounts']['accounts']['minsheng']['status'],'fresh')

    def test_busy_browser_slot_keeps_manual_request_queued(self):
        f=self.f
        from monitor.manual_ai import request,TRIGGER
        request(f.m.store.conn,f.settings,'codex');f.m.analysts={'codex':Mock()}
        result={'status':'ready','can_analyze':True,'waited_seconds':0}
        with slot(f.settings.db_path),patch('monitor.jd_live_preflight.prepare',return_value=result),patch.object(f.m,'ai_context',return_value={'xauusd':4000}):
            f.m._ai_worker(f.snap,{}, {'codex':TRIGGER})
        f.m.analysts['codex'].analyze.assert_not_called()
        self.assertEqual(f.m.store.get_state('ai_manual:codex')['status'],'queued')

class OrderFreshnessBrowserTests(unittest.TestCase):
    def test_failed_refresh_and_old_cache_do_not_mark_existing_order_expired(self):
        from playwright.sync_api import sync_playwright
        from test_dashboard_browser import local_dashboard
        from monitor.page_data import invalidate_pages
        with local_dashboard() as (f,url),sync_playwright() as p:
            order=parse_order(raw_order(),'zheshang');order['expires_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
            data=parse_account('zheshang',payload('zheshang'))
            data.update(cash_cny=0,pending_order_count=1,pending_orders={'records':[order],'total_count':1,'complete':True},
                        coverage={'pending_orders':'unavailable:OperationalError'},component_times={'pending_orders':NOW.isoformat()})
            record_success(f.m.store.conn,'zheshang',data,NOW.isoformat())
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page(viewport={'width':390,'height':844})
                page.goto(url+'/#strategies/live/zheshang/orders')
                page.get_by_text('挂单明细更新失败，保留上次记录；不代表挂单已撤销或到期。',exact=False).wait_for()
                self.assertIn('已挂单 · 等待成交',page.locator('#panel-strategies').inner_text())
                self.assertNotIn('条件单已到期',page.locator('#panel-strategies').inner_text())
                data['coverage']['pending_orders']='available'
                data['component_times']['pending_orders']=(NOW-timedelta(minutes=16)).isoformat()
                record_success(f.m.store.conn,'zheshang',data,NOW.isoformat());invalidate_pages(f.settings)
                page.get_by_role('button',name='刷新数据',exact=True).click()
                page.get_by_text('挂单明细距上次成功查询较久，需刷新核对；挂单本身仍以京东状态为准。',exact=False).wait_for()
                self.assertIn('已挂单 · 等待成交',page.locator('#panel-strategies').inner_text())
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            finally:browser.close()
