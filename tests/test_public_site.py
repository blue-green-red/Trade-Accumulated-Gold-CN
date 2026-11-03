"""Public site and allowlisted data stay independent of private dashboard APIs."""
import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from monitor.config import Settings
from monitor.store import Store
from monitor.web import Handler, ThreadingHTTPServer
from monitor.public_market import quote_data, history_data, cached_market, _cache
from monitor.public_ai import SCHEMA, save
from test_public_ai import content

ROOT=Path(__file__).resolve().parents[1]


@contextmanager
def site():
    with tempfile.TemporaryDirectory() as tmp:
        settings=replace(Settings(),db_path=Path(tmp)/'public.db',ai_analysts=('codex','claude'))
        store=Store(settings.db_path);store.conn.executescript(SCHEMA)
        now=datetime.now(timezone.utc)
        quote={'source':'jd_latest_price_api','price':850.,'quote_at':now.isoformat(),'fetched_at':now.isoformat(),
               'private_marker':'DO-NOT-PUBLISH'}
        store.set_state('model_control:claude',{'enabled':False,'generation':0,'changed_at':None})
        store.set_state('web_snapshot',{'ts':now.isoformat(),'xauusd':4000.,'change_pct':-.5,'fx':{'rate':7.1,'secret':'DO-NOT-PUBLISH'},
                        'execution_quote':quote,'jd_last_quote':quote,'bank_est':{'account':'DO-NOT-PUBLISH'},'private_account':'DO-NOT-PUBLISH'})
        for i in range(30):
            store.conn.execute('INSERT INTO ticks(ts,jd_price,xauusd,market_open) VALUES(?,?,?,1)',((now-timedelta(hours=29-i)).isoformat(),840+i/3,4000+i))
        report=content();report['decision_basis']['review_triggers']=[{'kind':'time','at':(now+timedelta(hours=2)).isoformat(),'event':None,'condition':None,'price_cny':None}]
        save(store.conn,'codex',0,1,now.isoformat(),now.isoformat(),report,{'jd_accumulation_quote':quote})
        store.conn.commit()
        class Local(Handler):
            password='test-private-password'
            public_static_root=ROOT/'frontend'/'dist-public'
            def log_message(self,*args):pass
        Local.settings=settings
        server=ThreadingHTTPServer(('127.0.0.1',0),Local)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:yield store,settings,f'http://127.0.0.1:{server.server_port}'
        finally:server.shutdown();server.server_close();worker.join();store.conn.close();_cache.clear()


class PublicSiteTests(unittest.TestCase):
    def test_market_allowlist_freshness_and_sampled_history(self):
        with site() as (store,settings,url):
            now=datetime.now(timezone.utc)
            with patch('monitor.public_market.trading_session',return_value={'is_open':True,'status':'交易中','next_open':None,'close_at':None,'rule':'test'}):
                value=quote_data(store.conn,now)
                self.assertEqual(value['quote']['price_cny'],850)
                self.assertEqual(value['quote']['status'],'fresh')
                self.assertNotIn('DO-NOT-PUBLISH',json.dumps(value))
                old=quote_data(store.conn,now+timedelta(minutes=3))
                self.assertEqual(old['quote']['status'],'stale')
                self.assertEqual(old['quote']['price_cny'],850)
            rows=history_data(store.conn,'7d',now)
            self.assertEqual(len(rows['points']),30)
            self.assertTrue(all(840<=r[1]<851 for r in rows['points']))
            self.assertNotIn('account',json.dumps(rows))

    def test_public_caches_share_queries_and_unknown_selectors_are_rejected(self):
        with site() as (store,settings,url):
            with patch('monitor.public_market.history_data',wraps=history_data) as build:
                a=cached_market(settings,{'view':['history'],'range':['7d']})
                b=cached_market(settings,{'view':['history'],'range':['7d']})
                self.assertEqual(a,b);self.assertEqual(build.call_count,1)
                cached_market(settings,{'view':['history'],'range':['30d']})
                self.assertEqual(build.call_count,2)
            for query in ({'view':['accounts']},{'range':['7d']},{'view':['quote','history']},{'view':['history'],'bank':['zheshang']}):
                with self.assertRaises(ValueError):cached_market(settings,query)

    def test_only_public_shell_assets_and_exact_public_apis_are_anonymous(self):
        with site() as (store,settings,url):
            host=url.removeprefix('http://')
            def get(path,headers=None):
                c=http.client.HTTPConnection(host);c.request('GET',path,headers=headers or {})
                r=c.getresponse();body=r.read();result=r.status,dict(r.getheaders()),body;c.close();return result
            self.assertEqual(get('/public')[0],301)
            status,headers,shell=get('/public/')
            self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'public, no-cache')
            import re
            asset=re.search(rb'src="([^"]+\.js)"',shell).group(1).decode()
            status,headers,body=get(asset)
            self.assertEqual(status,200);self.assertIn('immutable',headers['Cache-Control'])
            self.assertNotIn(b'account_decisions',body);self.assertNotIn(b'/api/dashboard',body)
            status,headers,body=get('/api/public/market?view=quote')
            self.assertEqual(status,200);self.assertNotIn(b'DO-NOT-PUBLISH',body)
            self.assertEqual(get('/api/public/market?view=quote',{'If-None-Match':headers['ETag']})[0],304)
            for path in ('/api/dashboard','/api/history','/api/pages/ai','/','/assets/index.js'):
                self.assertEqual(get(path)[0],401,path)
            for path in ('/public/../static/index.html','/public/assets/%2e%2e/%2e%2e/static/index.html','/public/.env','/public/assets/fake.map'):
                self.assertEqual(get(path)[0],404,path)
            for path in ('/api/public/market?view=live','/api/public/market?range=bad','/api/public/market?bank=minsheng'):
                self.assertEqual(get(path)[0],400,path)
            data=json.loads(get('/api/public/ai')[2]);self.assertEqual(data['available_analysts'],['codex'])


class PublicSiteBrowserTests(unittest.TestCase):
    def test_anonymous_mobile_and_desktop_lazy_views_shared_ai_cache_and_clock_expiry(self):
        from playwright.sync_api import sync_playwright
        with site() as (store,settings,url),sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page(viewport={'width':390,'height':844});requests=[];errors=[]
                page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url+'/public/')
                page.locator('[data-public-view="market"][data-ready="true"]').wait_for()
                page.get_by_role('img',name='京东积存金价格走势，数字标注在曲线旁').wait_for()
                self.assertIn('850.00',page.locator('.main-price').inner_text())
                page.screenshot(path='/tmp/gold-public-market-mobile.png')
                self.assertFalse(any('/api/public/ai' in u for u in requests))
                self.assertFalse(any('Analysis-' in u or 'Recommendations-' in u for u in requests))
                page.get_by_role('button',name='AI分析',exact=True).click()
                page.get_by_role('heading',name='影响市场的因素',exact=True).wait_for()
                self.assertEqual(sum('/api/public/ai' in u for u in requests),1)
                page.get_by_role('button',name='仓位推荐',exact=True).click()
                page.locator('.target-flow').wait_for()
                self.assertEqual(page.locator('.positions button').count(),11)
                for pct in ('100%','20%','0%'):
                    page.get_by_role('button',name=pct,exact=True).click()
                self.assertEqual(sum('/api/public/ai' in u for u in requests),1)
                page.get_by_role('button',name='AI分析',exact=True).click()
                page.get_by_role('heading',name='影响市场的因素',exact=True).wait_for()
                self.assertEqual(sum('/api/public/ai' in u for u in requests),1)
                page.get_by_role('button',name='行情',exact=True).click()
                self.assertEqual(sum('view=quote' in u for u in requests),1)
                with page.expect_response('**/api/public/market?view=history&range=30d'):
                    page.get_by_role('button',name='30天',exact=True).click()
                page.get_by_role('button',name='7天',exact=True).click()
                self.assertEqual(sum('range=7d' in u for u in requests),1)
                self.assertFalse(any('/api/' in u and '/api/public/' not in u for u in requests))
                for width in (360,390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    for tab in ('行情','AI分析','仓位推荐'):
                        page.get_by_role('button',name=tab,exact=True).click();page.wait_for_timeout(50)
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,tab))
                        self.assertNotIn('DO-NOT-PUBLISH',page.locator('body').inner_text())
                        self.assertNotIn('模拟账户',page.locator('body').inner_text())
                        self.assertNotIn('真实账户',page.locator('body').inner_text())
                        if width==390 and tab=='仓位推荐':page.screenshot(path='/tmp/gold-public-recommendations-mobile.png',full_page=True)
                page.evaluate('Date.now = () => new Date().getTime() + 86400000')
                page.get_by_text('方案已到期，等待新分析',exact=True).wait_for()
                self.assertEqual(errors,[])
            finally:browser.close()
