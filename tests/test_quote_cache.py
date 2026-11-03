import tempfile
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from monitor.config import Settings
from monitor.store import Store
from monitor.market import Market
from monitor.execution import execution_quote

NOW=datetime(2026,11,3,3,tzinfo=timezone.utc)

class QuoteCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'quotes.db')
        self.m=Market(replace(Settings(),db_path=Path(self.tmp.name)/'quotes.db'),self.s)
        tv={'status':'ok','price':4000,'day_high':4010,'day_low':3990,'day_open':4000,'change_pct':0,'signals':{}}
        tv.update({k:0 for k in ('rating_30m','rating_1h','rating_4h','rating_1d','rating_1w')})
        self.patches=[patch('monitor.market.fetch_tv_snapshot',return_value=tv),patch('monitor.market.fetch_spot_gold',return_value={'status':'error','error':'fixture offline'}),patch('monitor.wave_evidence.daily_indicators',return_value={})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()
    def quote(self,age=60,**changes):
        at=(NOW-timedelta(seconds=age)).isoformat()
        return {'source':'jd_latest_price_api','price':850.12,'quote_at':at,'fetched_at':at,'product_sku':'generic',**changes}
    def snapshot(self,result=None,now=NOW):
        effect=TimeoutError('fixture timeout') if result is None else None
        with patch('monitor.market.fetch_jd_latest_price',return_value=result,side_effect=effect),patch('monitor.market.utc_now',return_value=now),patch('monitor.execution.utc_now',return_value=now):
            return self.m.snapshot()
    def test_single_failed_poll_reuses_only_valid_quote_and_original_timestamps(self):
        q=self.quote();self.m.last_jd_quote=q;self.s.set_state('jd_last_quote',q)
        snap=self.snapshot()
        self.assertEqual(snap['execution_quote'],{**q,'cache_reused':True})
        self.assertEqual(snap['jd_price'],850.12);self.assertEqual(snap['jd_quote_status']['state'],'cached')
        self.assertEqual(self.m.last_jd_quote,q);self.assertEqual(self.s.get_state('jd_last_quote'),q)
    def test_repeated_failures_do_not_extend_cache_lifetime(self):
        self.m.last_jd_quote=self.quote()
        self.assertIsNotNone(self.snapshot()['execution_quote'])
        later=self.snapshot(now=NOW+timedelta(seconds=61))
        self.assertIsNone(later['execution_quote']);self.assertIsNone(later['jd_price'])
        self.assertEqual(later['jd_quote_status']['state'],'unavailable')
    def test_invalid_old_future_or_estimated_cache_cannot_execute(self):
        invalid=[self.quote(121),self.quote(fetched_at=(NOW-timedelta(seconds=121)).isoformat()),self.quote(-31),self.quote(source='estimated'),self.quote(price=float('nan'))]
        for q in invalid:
            with self.subTest(q=q):
                self.m.last_jd_quote=q;self.assertIsNone(self.snapshot()['execution_quote'])
    def test_bad_new_payload_does_not_replace_good_cache(self):
        old=self.quote();self.m.last_jd_quote=old
        result=self.snapshot(result=self.quote(200,price=999))
        self.assertEqual(result['execution_quote']['price'],old['price'])
        self.assertEqual(result['jd_quote_status']['reason'],'invalid_or_old_quote')
    def test_success_replaces_cache_and_clears_reuse_marker(self):
        self.m.last_jd_quote=self.quote();new=self.quote(0,price=852.34)
        result=self.snapshot(result=new)
        self.assertEqual(result['execution_quote']['price'],852.34)
        self.assertNotIn('cache_reused',result['execution_quote'])
        self.assertEqual(result['jd_quote_status']['state'],'fresh')
        self.assertEqual(self.s.get_state('jd_last_quote')['price'],852.34)
    def test_market_closed_never_reuses_cache_or_requests_quote(self):
        now=datetime(2026,11,8,3,tzinfo=timezone.utc)
        self.m.last_jd_quote=self.quote(0,quote_at=now.isoformat(),fetched_at=now.isoformat())
        with patch('monitor.market.fetch_jd_latest_price') as fetch,patch('monitor.market.utc_now',return_value=now),patch('monitor.execution.utc_now',return_value=now):
            result=self.m.snapshot()
        fetch.assert_not_called();self.assertIsNone(result['execution_quote'])
        self.assertEqual(result['jd_quote_status']['state'],'closed')

class QuoteCacheBrowserTests(unittest.TestCase):
    def test_cache_label_expires_without_another_fetch_on_overview_and_market(self):
        import json
        from playwright.sync_api import sync_playwright
        from test_dashboard_browser import local_dashboard
        from monitor.page_data import page_data
        with local_dashboard() as (f,url),sync_playwright() as p:
            now=datetime.now(timezone.utc);quoted=(now-timedelta(seconds=60)).isoformat()
            quote={'source':'jd_latest_price_api','price':850.12,'quote_at':quoted,'fetched_at':quoted,'cache_reused':True}
            overview=page_data(f.settings,'overview','summary');market=page_data(f.settings,'market','prices')
            for value in (overview,market):value['execution']={'ready':True,'quote':quote,'valuation_quote':quote,'status':{'state':'cached','reason':'fetch_failed'}}
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page(viewport={'width':390,'height':844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                for key,value in [('overview',overview),('market',market)]:
                    page.route('**/api/pages/'+key+'?*',lambda route,request,v=value:route.fulfill(status=200,content_type='application/json',body=json.dumps(v)))
                page.goto(url+'/#overview');page.get_by_text('沿用缓存 · 仍在120秒有效期内',exact=True).wait_for()
                page.get_by_role('tab',name='行情',exact=True).click();page.locator('[data-market-view="prices"]').wait_for()
                page.get_by_text('沿用缓存 · 仍在120秒有效期内',exact=True).wait_for()
                page.evaluate('window.originalDateNow=Date.now;Date.now=()=>window.originalDateNow()+70000')
                page.get_by_text('报价超过120秒 · 暂停模拟成交',exact=True).wait_for()
                page.get_by_role('tab',name='总览',exact=True).click()
                page.get_by_text('报价超过120秒 · 暂停模拟成交',exact=True).wait_for()
                self.assertIn('850.12',page.locator('#panel-overview').inner_text())
                self.assertEqual(errors,[])
            finally:browser.close()
