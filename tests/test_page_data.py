"""Page boundaries, private caching and independent invalidation for the Vue dashboard."""
import base64
import gzip
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_risk_accounts as risk
from monitor.controls import set_enabled
from monitor.page_data import page_data,cached_page,invalidate_pages
from monitor.web import Handler,ThreadingHTTPServer,dashboard


class PageDataTests(unittest.TestCase):
    def setUp(self):
        self.f=risk.RiskAccountTests();self.f.setUp()
        self.f.m.publish_for_web(self.f.snap,None)
        self.addCleanup(self.f.tearDown)

    def test_overview_and_notifications_skip_unrelated_queries(self):
        f=self.f
        with patch('monitor.page_data.equity_curves',side_effect=AssertionError('no curves')),patch('monitor.page_data.ai_section',side_effect=AssertionError('no research')),patch('monitor.page_data._wave',side_effect=AssertionError('no daily aggregation')),patch('monitor.wave_evidence.daily_reference',side_effect=AssertionError('overview does not query futures history')):
            overview=page_data(f.settings,'overview')
            notification=page_data(f.settings,'notifications')
            records=page_data(f.settings,'records')
        self.assertNotIn('equity',overview);self.assertNotIn('ai',overview);self.assertNotIn('prices',overview)
        self.assertNotIn('daily_change',overview)
        self.assertNotIn('live_readonly',notification);self.assertNotIn('comparison',notification)
        self.assertNotIn('fills',records);self.assertNotIn('comparison',records)
        self.assertLess(len(json.dumps(overview)),len(json.dumps(dashboard(f.settings)))/2)
        self.assertEqual([m['budget'] for m in overview['comparison']['models']],[600000,600000])

    def test_account_views_and_disabled_models_are_filtered_before_delivery(self):
        f=self.f
        set_enabled(f.m.store.conn,'claude',False)
        live=page_data(f.settings,'strategies','live')
        self.assertEqual(live['account_view'],'live');self.assertNotIn('equity',live)
        simulation=page_data(f.settings,'strategies','simulation')
        self.assertEqual(len(simulation['leaderboard']),6);self.assertNotIn('live_readonly',simulation)
        self.assertTrue(all(a['key'].startswith('codex:') for a in simulation['leaderboard']))
        for page in ('overview','compare','ai','records','notifications'):
            value=page_data(f.settings,page);controls=value.pop('model_controls')
            self.assertNotIn('claude',json.dumps(value).lower(),page)
            self.assertIn({'key':'claude','name':'Claude','enabled':False},controls)
        full=dashboard(f.settings)
        self.assertEqual(simulation['comparison']['models'][0]['baseline_return'],full['comparison']['models'][0]['baseline_return'])

    def test_market_sends_dated_prices_without_sampling_diagnostics(self):
        sample={'status':'available','source':'local','timezone':'Asia/Shanghai','as_of':'now',
                'missing_dates':['2026-10-29'],'days':[{'date_bj':'2026-10-30','last_price_cny':847.45,
                'first_price_cny':830,'samples':12,'missing_minute_samples':100,'max_gap_minutes':15,'partial':True}]}
        with patch('monitor.wave_evidence.jd_daily_samples',return_value=sample):
            result=page_data(self.f.settings,'market')['wave_evidence']['jd_daily']
        self.assertEqual(result['days'],[{'date_bj':'2026-10-30','last_price_cny':847.45}])
        self.assertNotIn('missing_dates',result)
        self.assertEqual(sample['days'][0]['missing_minute_samples'],100)

    def test_page_caches_are_independent_and_control_writes_invalidate_them(self):
        settings=self.f.settings
        invalidate_pages(settings)
        with patch('monitor.page_data.page_data',return_value={'page':'overview','generated_at':'first','snapshot':{'ts':'same'}}) as query:
            body,tag=cached_page(settings,'overview')
            self.assertEqual((body,tag),cached_page(settings,'overview'));self.assertEqual(query.call_count,1)
            cached_page(settings,'notifications');self.assertEqual(query.call_count,2)
            invalidate_pages(settings);cached_page(settings,'overview');self.assertEqual(query.call_count,3)
        invalidate_pages(settings)
        with patch('monitor.page_data.page_data',return_value={'page':'overview','generated_at':'second','snapshot':{'ts':'same'}}):
            _,second=cached_page(settings,'overview')
        self.assertEqual(tag,second) # unchanged data may reuse the client copy, despite retrieval time
        invalidate_pages(settings)

    def test_invalidation_during_an_inflight_read_cannot_repopulate_old_cache(self):
        settings=self.f.settings;invalidate_pages(settings)
        entered=threading.Event();release=threading.Event();count=[]
        def query(*args):
            count.append(1)
            if len(count)==1:entered.set();release.wait(3)
            return {'value':len(count)}
        with patch('monitor.page_data.page_data',side_effect=query):
            worker=threading.Thread(target=lambda:cached_page(settings,'overview'));worker.start()
            self.assertTrue(entered.wait(2));invalidate_pages(settings);release.set();worker.join(3)
            body,_=cached_page(settings,'overview');self.assertEqual(json.loads(body)['value'],2)
        invalidate_pages(settings)

    def test_authenticated_static_shell_and_page_http_cache_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'assets').mkdir()
            (root/'index.html').write_text('<div id="app"></div><script type="module" src="/assets/app-hash.js"></script>')
            (root/'assets'/'app-hash.js').write_text('const view="static";'+(' '*1200))
            class Local(Handler):
                password='test-only';static_root=root
                def log_message(self,*args):pass
            Local.settings=self.f.settings
            server=ThreadingHTTPServer(('127.0.0.1',0),Local);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            auth={'Authorization':'Basic '+base64.b64encode(b'viewer:test-only').decode()}
            def get(path,headers=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('GET',path,headers=headers or {});r=conn.getresponse();body=r.read();result=(r.status,dict(r.getheaders()),body);conn.close();return result
            try:
                self.assertEqual(get('/assets/app-hash.js')[0],401)
                with patch('monitor.web.cached_dashboard',side_effect=AssertionError('shell never queries accounts')):
                    status,headers,body=get('/',auth)
                self.assertEqual(status,200);self.assertNotIn(b'initial-dashboard',body)
                self.assertEqual(headers['Cache-Control'],'private, no-cache')
                self.assertEqual(get('/',{**auth,'If-None-Match':headers['ETag']})[0],304)
                status,headers,body=get('/assets/app-hash.js',{**auth,'Accept-Encoding':'gzip'})
                self.assertEqual(status,200);self.assertIn('immutable',headers['Cache-Control']);self.assertEqual(headers['Content-Encoding'],'gzip')
                self.assertTrue(gzip.decompress(body).startswith(b'const view='))
                self.assertEqual(get('/assets/%2e%2e/index.html',auth)[0],404)
                status,headers,body=get('/api/pages/overview',auth)
                self.assertEqual(status,200);self.assertIn('no-store',headers['Cache-Control']);self.assertIn('Server-Timing',headers)
                self.assertEqual(get('/api/pages/overview',{**auth,'If-None-Match':headers['ETag']})[0],304)
                self.assertEqual(get('/api/pages/unknown',auth)[0],404)
                self.assertEqual(get('/api/pages/strategies?view=invalid',auth)[0],400)
            finally:server.shutdown();server.server_close();thread.join()
