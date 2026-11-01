import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from monitor.jd_readonly_probe import allowed,response_info,safe_path,write_report,probe
from monitor.jd_live_readonly import ensure_schema,accounts
from monitor.risk_profiles import reset_experiment
from monitor.store import Store


class JDReadonlyTests(unittest.TestCase):
    def test_trade_requests_and_unknown_business_apis_are_blocked(self):
        base='https://api.jdjygold.com'
        for name in ('buy','sell','pay','cancelOrder','submitOrder','signAgreement','queryUnknown'):
            for method in ('GET','POST'):
                self.assertFalse(allowed(method,base+'/gw2/generic/hj/h5/m/'+name,login=True))
        self.assertTrue(allowed('POST',base+'/gw2/generic/6440/h5/m/queryStallForGold'))
        self.assertFalse(allowed('POST','https://evil.example/gw2/generic/6440/h5/m/queryStallForGold',login=True))
        self.assertFalse(allowed('POST','https://passport.jd.com/login',login=False))
        self.assertTrue(allowed('POST','https://passport.jd.com/login',login=True))
        self.assertFalse(allowed('GET','http://api.jdjygold.com/gw/generic/hj/h5/m/latestPrice'))

    def test_report_contains_shapes_not_credentials_or_financial_values(self):
        data={'success':True,'resultCode':'0','resultData':{'pin':'private-user','cookie':'secret-token','cash':12345.67,'positions':[{'grams':99.123}], '13900000000':{'realName':'张三'}}}
        text=json.dumps(response_info(data),ensure_ascii=False)
        for secret in ('private-user','secret-token','12345.67','99.123','13900000000','张三'):self.assertNotIn(secret,text)
        self.assertNotIn('key',safe_path('https://passport.jd.com/secret?token=key#fragment'))
        self.assertNotIn('secret',safe_path('https://passport.jd.com/secret?token=key#fragment'))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'report.json';write_report(p,response_info(data))
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):write_report(p,data)

    def test_ecs_headed_login_fails_clearly_without_starting_browser(self):
        with patch('sys.platform','linux'),patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'ECS只运行后台'):probe('zheshang',True)

    def test_live_account_boundary_never_initializes_from_simulation_and_survives_reset(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'test.db');ensure_schema(store.conn)
            result=accounts(store.conn)
            self.assertEqual([a['bank'] for a in result],['zheshang','minsheng'])
            for a in result:
                self.assertIsNone(a['cash_cny']);self.assertIsNone(a['total_grams']);self.assertIsNone(a['orders'])
                self.assertFalse(a['connected']);self.assertFalse(a['order_submission_enabled'])
            store.conn.execute("update jd_live_readonly set checked_at='2026-11-01T00:00:00+00:00',public_status='reachable'");store.conn.commit()
            before=accounts(store.conn);reset_experiment(store.conn);self.assertEqual(before,accounts(store.conn))
            ensure_schema(store.conn);self.assertEqual(before,accounts(store.conn));store.conn.close()


class JDLoginLoopTests(unittest.TestCase):
    def test_headed_uses_native_identity_and_auth_support_stays_scoped(self):
        from monitor.jd_readonly_probe import browser_options
        launch,context=browser_options(True,'chrome')
        self.assertEqual(launch['channel'],'chrome');self.assertFalse(launch['headless'])
        self.assertNotIn('user_agent',context);self.assertNotIn('is_mobile',context)
        self.assertTrue(allowed('POST','https://jra.jd.com/jsTk.do',login=True))
        self.assertFalse(allowed('POST','https://jra.jd.com/pay',login=True))
        self.assertFalse(allowed('POST','https://jra.jd.com/jsTk.do',login=False))

    def test_sms_request_is_processed_while_waiting_for_terminal(self):
        import threading
        from playwright.sync_api import sync_playwright
        from monitor.jd_readonly_probe import wait_for_login
        requested=threading.Event()
        with sync_playwright() as p:
            b=p.chromium.launch(headless=True,args=['--no-sandbox'])
            page=b.new_page()
            def sms(route):
                requested.set();route.fulfill(status=200,body='{}',headers={'Access-Control-Allow-Origin':'*'})
            page.route('https://passport.jd.com/local-test-sms',sms)
            page.goto('about:blank')
            page.evaluate("setTimeout(()=>fetch('https://passport.jd.com/local-test-sms'),50)")
            wait_for_login(page,lambda:requested.wait(timeout=3))
            self.assertTrue(requested.is_set(),'SMS fetch stalled while terminal waited')
            b.close()


class JDLoginAllowlistTests(unittest.TestCase):
    def test_reported_captcha_and_security_dependencies_are_available_only_for_login(self):
        for host in ('jcap.m.jd.com','jcapmonitor.m.jd.com','sso.jd.com','cactus.jd.com','sgm-taihang.jd.com','h5speed.m.jd.com'):
            with self.subTest(host=host):
                self.assertTrue(allowed('POST','https://'+host+'/check',login=True))
                self.assertFalse(allowed('POST','https://'+host+'/check',login=False))
                self.assertFalse(allowed('POST','https://'+host+'.evil.example/check',login=True))
        self.assertTrue(allowed('GET','https://storage.360buyimg.com/config.json','fetch',True))
        self.assertTrue(allowed('GET','https://qdsdk.jd.com/pageid/config.json','xhr',True))

    def test_generic_gateway_does_not_allow_unknown_or_conflicting_functions(self):
        from monitor.jd_readonly_probe import gateway_function_id
        base='https://api.m.jd.com/'
        for name in ('h5Config','getCustomCtrl','wsgw_getinfo'):
            self.assertTrue(allowed('POST',base+'?functionId='+name,login=True,post_data='functionId='+name))
        for url,data in [(base,None),(base+'?functionId=submitOrder',None),
                         (base+'?functionId=h5Config&functionId=buy',None),
                         (base+'?functionId=h5Config','functionId=buy'),
                         (base+'pay?functionId=h5Config',None),
                         ('https://api.m.jd.com.evil/?functionId=h5Config',None)]:
            with self.subTest(url=url):self.assertFalse(allowed('POST',url,login=True,post_data=data))
        self.assertIsNone(gateway_function_id(base+'?functionId=13800000000'))
        self.assertNotIn('13800000000',json.dumps(response_info({'err_code':23,'phone':'13800000000','message':'private'})))
        self.assertNotIn('654321',json.dumps(response_info({'code':'654321','err_code':23})))


class JDGoldCallbackTests(unittest.TestCase):
    def test_gold_sso_callback_is_login_only_and_read_only(self):
        callback='https://plogin.jdjygold.com/callback?token=do-not-record'
        self.assertTrue(allowed('GET',callback,'document',True))
        self.assertFalse(allowed('GET',callback,'document',False))
        self.assertFalse(allowed('POST',callback,'document',True))
        self.assertFalse(allowed('GET','https://plogin.jdjygold.com.evil/callback','document',True))
        self.assertFalse(allowed('POST','https://api.jdjygold.com/gw2/generic/6440/h5/m/submitOrder',login=True))
        self.assertNotIn('do-not-record',safe_path(callback))

    def test_login_redirect_reaches_gold_business_before_account_query(self):
        from playwright.sync_api import sync_playwright
        from monitor.jd_readonly_probe import BANKS
        seen=[];blocked=[]
        with sync_playwright() as p:
            b=p.chromium.launch(headless=True,args=['--no-sandbox']);context=b.new_context()
            def route(r):
                q=r.request
                if not allowed(q.method,q.url,q.resource_type,True,q.post_data):
                    blocked.append(q.url);r.abort();return
                seen.append(q.url.split('?')[0])
                if q.url.startswith('https://plogin.m.jd.com/'):
                    r.fulfill(status=200,content_type='text/html',body="<script>setTimeout(()=>location.href='https://plogin.jdjygold.com/callback?token=private-code',20)</script>")
                elif q.url.startswith('https://plogin.jdjygold.com/'):
                    r.fulfill(status=200,content_type='text/html',body="<script>setTimeout(()=>location.href='"+BANKS['zheshang']['url']+"',20)</script>")
                elif q.url.startswith('https://m.jdjygold.com/'):
                    r.fulfill(status=200,content_type='text/html',body='<html>Gold account</html>')
                else:
                    r.fulfill(status=200,content_type='application/json',headers={'Access-Control-Allow-Origin':'*'},body='{"success":true,"resultCode":"0"}')
            context.route('**/*',route)
            page=context.new_page();page.goto('https://plogin.m.jd.com/local-test-login',wait_until='load')
            page.wait_for_url(BANKS['zheshang']['url'],timeout=5000)
            result=page.evaluate("async()=>{const r=await fetch('https://api.jdjygold.com/gw2/generic/6440/h5/m/queryStallForGold');return r.json()}")
            self.assertTrue(result['success'])
            self.assertEqual(blocked,[])
            self.assertEqual(seen[:3],['https://plogin.m.jd.com/local-test-login','https://plogin.jdjygold.com/callback',BANKS['zheshang']['url'].split('?')[0]])
            context.close();b.close()


class JDPostLoginReadTests(unittest.TestCase):
    def test_readonly_details_available_but_mutations_and_unknown_gateways_stay_blocked(self):
        reads=('/gw2/generic/CfGoldWeb/h5/m/cfQueryGoldProductInfo',
               '/gw2/generic/jrm/h5/m/queryProductInfo',
               '/gw2/generic/jrm/h5/m/queryStdAssetsBalV2',
               '/gw/generic/hj/h5/m/queryAccountAssetsV2',
               '/gw2/generic/jrm/h5/m/getTradePeriodStd',
               '/gw2/generic/hj/h5/m/cfQueryConditionWaittingNum',
               '/gw2/generic/CfGoldWeb/newh5/m/cfQueryLastTrade',
               '/gw2/generic/hj/h5/m/cfGetPriceTrendChart',
               '/gw/generic/aladdin/h5/m/getPageMultiDataForH5')
        for path in reads:self.assertTrue(allowed('POST','https://api.jdjygold.com'+path,login=True))
        for name in ('submitOrder','cfBuyGold','cfSellGold','cancelCondition','signAgreement','res-ms'):
            self.assertFalse(allowed('POST','https://api.jdjygold.com/gw2/generic/hj/h5/m/'+name,login=True))
        self.assertFalse(allowed('POST','https://api.jdjygold.com/gw2/generic/bge/h5/m/res-ms',login=True))
        self.assertFalse(allowed('POST','https://ms.jr.jd.com/submit',login=True))

    def test_capabilities_do_not_export_balances_or_claim_connected(self):
        from monitor.jd_readonly_probe import account_evidence,summarize_evidence
        payload={'resultData':{'datas':{'login':True,'goldStockStat':{'totalGram':123.456,'availableGram':100,'frozenGram':23.456},'goldProductInfo':{'productSku':1961543816}}}}
        evidence=account_evidence(payload)
        self.assertTrue(evidence['login_confirmed']);self.assertTrue(evidence['holdings_fields_observed'])
        self.assertNotIn('123.456',json.dumps(evidence));self.assertNotIn('1961543816',json.dumps(evidence))
        quote=account_evidence({'resultData':{'data':{'buyPrice':'830.62','sellPrice':'827.3','productSku':'1961543816'}}})
        merged=summarize_evidence([{'account_evidence':evidence},{'account_evidence':quote}])
        self.assertTrue(merged['quote_fields_observed']);self.assertTrue(merged['login_confirmed'])
        self.assertIsNone(summarize_evidence([])['login_confirmed'])
