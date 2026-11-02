import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.config import Settings
from monitor.store import Store
from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,record_failure,accounts,model_input,PRODUCTS,declare_full_position
from monitor.jd_live_advice import LiveAdviceSet,save
from monitor.jd_live_sync import ReadonlyClient,QUERY_PATHS,refresh,request_refresh
from monitor.controls import set_enabled

NOW=datetime.now(timezone.utc)
def payload(bank='zheshang'):
    sku,name,_=PRODUCTS[bank]
    return {'success':True,'resultCode':0,'resultData':{'datas':{'login':True,
        'goldProductInfo':{'productSku':int(sku),'productName':name,'sellRate':.4},
        'goldStockStat':{'productSku':sku,'totalGram':12.5,'availableGram':10,'frozenGram':2.5,
            'avgCostPrice':1100,'totalIncome':-2000,'jrid':'private-id','goldAccount':'private-account'}}}}
def advice():return {'action':'sell','grams':5,'price_condition':'at_or_above','price_cny':850,
                     'valid_for_minutes':60,'reason':'测试建议','risk_note':'需要用户自行确认'}

class LiveAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db');self.c=self.store.conn;ensure_schema(self.c)
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def test_identity_numeric_boundary_and_private_fields(self):
        for bank in PRODUCTS:
            result=parse_account(bank,payload(bank));self.assertEqual(result['sell_fee_rate'],.004)
            self.assertIsNone(result['cash_cny']);self.assertIsNone(result['orders'])
            self.assertNotIn('private',json.dumps(result))
        bad=payload();bad['resultData']['datas']['login']=False
        with self.assertRaises(PermissionError):parse_account('zheshang',bad)
        with self.assertRaises(ValueError):parse_account('minsheng',payload())
        for value in (float('nan'),float('inf'),-1,True):
            bad=payload();bad['resultData']['datas']['goldStockStat']['totalGram']=value
            with self.assertRaises(ValueError):parse_account('zheshang',bad)
        bad=payload();bad['resultData']['datas']['goldStockStat']['availableGram']=20
        with self.assertRaises(ValueError):parse_account('zheshang',bad)
    def test_failed_refresh_keeps_last_good_and_does_not_fake_zero(self):
        record_success(self.c,'zheshang',parse_account('zheshang',payload()),NOW.isoformat())
        self.assertTrue(accounts(self.c,NOW)[0]['connected'])
        self.assertEqual(accounts(self.c,NOW+timedelta(minutes=31))[0]['status'],'stale')
        record_failure(self.c,'zheshang','login_required',NOW.isoformat())
        row=accounts(self.c,NOW)[0]
        self.assertEqual(row['total_grams'],12.5);self.assertFalse(row['connected'])
        self.assertIsNone(row['cash_cny']);self.assertIsNone(accounts(self.c,NOW)[1]['total_grams'])
        self.assertEqual(model_input(self.c,NOW)['accounts']['zheshang']['status'],'login_required')

    def test_holding_pnl_uses_bank_quote_and_cost_not_cumulative_income(self):
        raw=payload();raw['resultData']['datas']['goldStockStat']['newAvgPrice']=1090
        data=parse_account('zheshang',raw)
        self.assertEqual(data['average_cost_cny'],1090)
        self.assertIsNone(data['holding_pnl_cny'])
        data.update(holding_pnl_cny=999999,quote={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':NOW.isoformat()})
        record_success(self.c,'zheshang',data,NOW.isoformat())
        row=accounts(self.c,NOW)[0]
        self.assertEqual(row['holding_pnl_cny'],-3000)
    def test_advice_expiry_account_revision_and_model_switch(self):
        data=parse_account('zheshang',payload());record_success(self.c,'zheshang',data,NOW.isoformat())
        inputs=model_input(self.c,NOW);value=LiveAdviceSet(zheshang=advice()).model_dump()
        save(self.c,'codex',1,value,inputs,NOW);self.c.commit()
        row=accounts(self.c,NOW)[0];self.assertEqual(row['advice'][0]['status'],'current')
        self.assertEqual(accounts(self.c,NOW+timedelta(minutes=61))[0]['advice'][0]['status'],'expired')
        data['total_grams']=15;record_success(self.c,'zheshang',data,NOW.isoformat())
        self.assertEqual(accounts(self.c,NOW)[0]['advice'][0]['status'],'account_changed')
        set_enabled(self.c,'codex',False);self.assertEqual(accounts(self.c,NOW)[0]['advice'],[])
        set_enabled(self.c,'codex',True);self.assertEqual(accounts(self.c,NOW)[0]['advice'],[])
    def test_invalid_sell_is_marked_for_manual_review_not_executed(self):
        record_success(self.c,'zheshang',parse_account('zheshang',payload()),NOW.isoformat())
        value=advice();value['grams']=100
        save(self.c,'codex',1,{'zheshang':value},model_input(self.c,NOW),NOW);self.c.commit()
        row=accounts(self.c,NOW)[0]
        self.assertIn('超过',row['advice'][0]['checks'][0]);self.assertFalse(row['order_submission_enabled'])
        self.assertEqual(self.c.execute('select count(*) from paper_trades').fetchone()[0],0)
    def test_refresh_read_only_routes_and_optional_failures(self):
        client=Mock()
        def query(op,bank):
            if op in ('indexData','indexDataStd'):return payload(bank)
            raise ValueError('component unavailable')
        client.query.side_effect=query;refresh(self.store,client)
        for row in accounts(self.c):
            self.assertTrue(row['connected']);self.assertIsNone(row['pending_order_count'])
        methods={c.args[0] for c in client.query.call_args_list}
        self.assertEqual(methods,{'indexData','indexDataStd','quote','recent_trade','pending_count','pending_orders','order_records','holding_details','closed_positions','cash_balance'})
        http=ReadonlyClient();http.session=Mock()
        http.session.post.return_value.status_code=200
        http.session.post.return_value.json.return_value=payload()
        http.query('indexDataStd','zheshang')
        call=http.session.post.call_args
        self.assertFalse(call.kwargs['allow_redirects']);self.assertIn('/indexDataStd',call.args[0])
        with self.assertRaises(KeyError):http.query('submitOrder','zheshang')
        self.assertEqual(http.session.post.call_count,1)
    def test_refresh_requests_coalesce(self):
        self.assertEqual(request_refresh(self.c),request_refresh(self.c))

    def test_user_declared_cash_is_not_api_cash_and_expires_on_trade_change(self):
        data=parse_account('zheshang',payload());record_success(self.c,'zheshang',data,NOW.isoformat())
        self.assertIsNone(accounts(self.c,NOW)[0]['cash_cny'])
        declare_full_position(self.c,'zheshang',NOW.isoformat())
        row=accounts(self.c,NOW)[0]
        self.assertEqual(row['cash_cny'],0);self.assertEqual(row['position_pct'],100)
        self.assertEqual(row['cash_source'],'user_declared_full_position')
        self.assertEqual(model_input(self.c,NOW)['risk_profile']['key'],'high')
        original=copy.deepcopy(data);data['quote']={'buy_price_cny':850,'sell_price_cny':850,'quoted_at':NOW.isoformat()}
        record_success(self.c,'zheshang',data,NOW.isoformat())
        self.assertEqual(accounts(self.c,NOW)[0]['cash_cny'],0)
        data['total_grams']=10;data['sellable_grams']=7.5;record_success(self.c,'zheshang',data,NOW.isoformat())
        self.assertIsNone(accounts(self.c,NOW)[0]['cash_cny'])
        record_success(self.c,'zheshang',original,NOW.isoformat())
        self.assertIsNone(accounts(self.c,NOW)[0]['cash_cny'])

class LiveModelIntegrationTests(unittest.TestCase):
    @patch('monitor.execution.trading_session',return_value={'is_open':True})
    def test_existing_analysis_round_saves_separate_manual_advice(self,_session):
        import test_risk_accounts as fixtures
        from monitor.ai import RiskAccountResult
        from test_autonomous import response
        fixture=fixtures.RiskAccountTests();fixture.setUp()
        try:
            c=fixture.m.store.conn;record_success(c,'zheshang',parse_account('zheshang',payload()))
            context={'snapshot_at':fixture.snap['ts'].isoformat(),'xauusd':4000,
                'trading_accounts':fixture.m.portfolios.input('codex',fixture.snap['execution_quote']),
                'live_readonly_accounts':model_input(c)}
            base=response().model_dump();base.update(account_decisions=fixture.decisions(),live_account_advice={'zheshang':advice()})
            result=RiskAccountResult(**base)
            before=c.execute('select count(*) from ai_portfolio_fills').fetchone()[0]
            fixture.a._run_codex=Mock(return_value=('notes',[],result))
            output=fixture.a.analyze(context,'test',run_id='live-test')
            self.assertIsNotNone(output);self.assertEqual(len(output['account_decisions']),6)
            self.assertEqual(c.execute('select count(*) from jd_live_advice').fetchone()[0],1)
            self.assertEqual(c.execute('select count(*) from ai_portfolio_fills').fetchone()[0],before)
            self.assertFalse(accounts(c)[0]['order_submission_enabled'])
            sent=fixture.a._run_codex.call_args.args[0]
            self.assertIn('live_readonly_accounts',sent)
            self.assertNotIn('private',json.dumps(sent['live_readonly_accounts']))
        finally:fixture.tearDown()

class LiveDashboardTests(unittest.TestCase):
    @patch('monitor.execution.trading_session',return_value={'is_open':True})
    def test_mobile_data_advice_and_refresh_queue(self,_session):
        import threading
        import test_risk_accounts as fixtures
        from monitor.web import Handler,ThreadingHTTPServer
        from playwright.sync_api import sync_playwright
        f=fixtures.RiskAccountTests();f.setUp()
        class Local(Handler):
            password=''
            def log_message(self,*args):pass
        Local.settings=f.settings
        server=ThreadingHTTPServer(('127.0.0.1',0),Local)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            c=f.m.store.conn
            for bank in PRODUCTS:
                data=parse_account(bank,payload(bank));data['pending_order_count']=0 if bank=='zheshang' else None
                record_success(c,bank,data)
            save(c,'codex',1,{'zheshang':advice()},model_input(c));c.commit()
            set_enabled(c,'claude',False);save(c,'claude',2,{'zheshang':advice()},model_input(c));c.commit()
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
                try:
                    page=browser.new_page(viewport={'width':390,'height':844});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#strategies')
                    page.get_by_role('heading',name='京东真实账户 · 只读接入',exact=True).wait_for()
                    text=page.locator('#panel-strategies').inner_text()
                    self.assertIn('12.5000 克',text);self.assertIn('成本均价',text)
                    self.assertIn('待处理条件单 0 笔',text);self.assertIn('待处理条件单 未知',text)
                    self.assertIn('Codex · 卖出建议',text);self.assertNotIn('Claude · 卖出建议',text)
                    self.assertNotIn('需要重新登录京东',text)
                    self.assertEqual(page.get_by_role('link',name='下载电脑端只读助手',exact=True).count(),0)
                    self.assertNotIn('private-account',text)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.evaluate("""()=>{
                      const a=DATA.live_readonly[0],o={order_key:'browser-unit',action:'sell',status:'pending',grams:4.98,price_cny:850.4,price_condition:'at_or_above',expires_at:new Date(Date.now()+86400000).toISOString()};
                      a.pending_orders={complete:true,status:'fresh',records:[o]};a.pending_order_count=1;
                      a.advice[0].execution_progress={status:'pending',match:'similar_parameters',orders:[o]};
                      a.quote={buy_price_cny:836,sell_price_cny:836,quoted_at:new Date().toISOString(),status:'fresh'};
                      a.advice[0].order_management=[{order_key:o.order_key,action:'replace',state:'current',input_order:o,effective_replacement:{grams:4.5,price_cny:848,price_condition:'at_or_above',expires_at:o.expires_at},reason:'按现有单调整参数',checks:[]}];
                      a.advice[0].review_conditions=[{condition:'at_or_below',price_cny:830.59,quote_side:'sell',order_key:o.order_key,reason:'达到阈值重新评估现有单',monitoring:{status:'queued',last_quote_at:new Date().toISOString(),last_price_cny:830.5,triggered_at:new Date().toISOString()}}];
                      a.review_status=[{analyst:'codex',status:'queued',reasons:['真实条件单数量变化']}];
                      renderStrategies(DATA);
                    }""")
                    text=page.locator('#panel-strategies').inner_text()
                    self.assertIn('京东条件单明细 · 1 笔',text);self.assertIn('发现相近挂单，参数有调整',text)
                    self.assertIn('待重新评估',text);self.assertIn('条件单有效至',text)
                    self.assertIn('建议改单',text);self.assertIn('修改后：卖出4.5000 克',text)
                    self.assertIn('价格触发复评',text);self.assertIn('已触发 · 待复评',text)
                    self.assertIn('不自动撤改或下单',text)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.set_viewport_size({'width':1280,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.set_viewport_size({'width':390,'height':844})
                    status=page.evaluate("async()=> (await fetch('/api/live-readonly/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status")
                    self.assertEqual(status,403)
                    page.get_by_role('button',name='刷新真实账户',exact=True).click()
                    page.get_by_role('button',name='更新中…',exact=True).wait_for()
                    self.assertEqual(f.m.store.get_state('jd_live_refresh')['status'],'queued')
                    self.assertEqual(errors,[])
                    page.get_by_role('button',name='账户视图：模拟账户',exact=True).click()
                    self.assertEqual(page.locator('#panel-strategies details.asset-account').count(),6)
                    page.get_by_role('tab',name='通知',exact=True).click()
                    self.assertIn('真实账户建议 · 人工执行',page.locator('#panel-notifications').inner_text())
                    self.assertEqual(page.get_by_role('heading',name='最近通知记录',exact=True).count(),1)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join();f.tearDown()

class LiveCashAndNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'live.db');self.c=self.store.conn;ensure_schema(self.c)
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def test_two_bank_cash_fields_and_api_precedence(self):
        from monitor.jd_live_sync import parse_cash
        samples={'zheshang':{'resultData':{'errorCode':'00000','datas':{'availBalance':'12.30','balance':'20.00'}}},
                 'minsheng':{'resultData':{'datas':{'code':'DS00000','availableBalance':'45.60','totalBalance':'50.00'}}}}
        for bank in PRODUCTS:
            data=parse_account(bank,payload(bank));record_success(self.c,bank,data)
            declare_full_position(self.c,bank)
            data.update(parse_cash(samples[bank],bank));record_success(self.c,bank,data)
        rows=accounts(self.c)
        self.assertEqual([r['cash_cny'] for r in rows],[12.3,45.6])
        self.assertTrue(all(r['cash_source']=='jd_electronic_account_api' for r in rows))
        self.assertEqual(model_input(self.c)['unknown'],['完整待成交委托列表','完整成交历史'])
        with self.assertRaises(ValueError):parse_cash(samples['zheshang'],'minsheng')
    def test_cash_change_queues_new_analysis(self):
        from monitor.ai_events import read
        data=parse_account('zheshang',payload());data.update(cash_cny=10,cash_source='jd_electronic_account_api')
        record_success(self.c,'zheshang',data);save(self.c,'codex',1,{'zheshang':advice()},model_input(self.c));self.c.commit()
        self.store.set_state('ai_status:codex',{'available':True})
        client=Mock()
        def query(op,bank):
            if op in ('indexData','indexDataStd'):return payload(bank)
            if op=='cash_balance':return {'resultData':{'errorCode':'00000','datas':{'availBalance':20}}} if bank=='zheshang' else {'resultData':{'datas':{'code':'DS00000','availableBalance':30}}}
            raise ValueError('not provided')
        client.query.side_effect=query;refresh(self.store,client)
        self.assertTrue(any(e['data']['bank']=='zheshang' for e in read(self.c,'codex')['pending']))
    def test_notification_topics_dedup_expiry_and_existing_channel(self):
        from monitor.jd_live_advice import notification_events
        from monitor.notification_control import visible_subscriptions,set_subscription,selected_events
        from monitor.__main__ import Monitor
        for bank in PRODUCTS:record_success(self.c,bank,parse_account(bank,payload(bank)))
        topics=[r for r in visible_subscriptions(self.c,'risk_six_account') if ':live_' in r['key']]
        self.assertEqual(len(topics),4);self.assertTrue(all(not r['enabled'] for r in topics))
        save(self.c,'codex',1,{'zheshang':advice(),'minsheng':advice()},model_input(self.c));self.c.commit()
        events=notification_events(self.c,'codex',1);self.assertEqual(len(events),2)
        self.assertTrue(all('程序未自动下单' in e['body'] for e in events))
        self.assertEqual(selected_events(self.c,events),[])
        for bank in PRODUCTS:set_subscription(self.c,'codex:live_'+bank,True)
        m=object.__new__(Monitor);m.settings=Settings();m.store=self.store;m.notifier=Mock();m.notifier.send.return_value=True
        m.dispatch(events);m.notifier.send.assert_called_once()
        self.assertEqual(notification_events(self.c,'codex',1),[])
        save(self.c,'codex',2,{'zheshang':advice(),'minsheng':advice()},model_input(self.c));self.c.commit()
        self.assertEqual(notification_events(self.c,'codex',2),[])
        changed=advice();changed['price_cny']=860
        save(self.c,'codex',3,{'zheshang':changed},model_input(self.c));self.c.commit()
        updated=notification_events(self.c,'codex',3);self.assertEqual(len(updated),1)
        self.c.execute("UPDATE jd_live_advice SET expires_at='2000-01-01T00:00:00+00:00'");self.c.commit()
        self.assertEqual(selected_events(self.c,updated),[])
    def test_history_redaction_and_time_normalization(self):
        from monitor.jd_live_sync import parse_history
        rows={'resultData':{'datas':{'totalCount':30,'holdingList':[{'orderId':'private-id','jrid':'private-user','createTime':'2026-11-02 09:00:00','buyPrice':'850','orderGram':10}]}}}
        value=parse_history(rows,'holding_details');text=json.dumps(value)
        self.assertNotIn('private',text);self.assertEqual(value['total_count'],30)
        self.assertEqual(value['records'][0]['time'],'2026-11-02T01:00:00+00:00')
