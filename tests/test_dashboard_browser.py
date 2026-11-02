"""Vue integration: real local HTTP/SQLite, no models, market requests or WeChat."""
import importlib.util
import json
import threading
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock,patch

from monitor.web import Handler,ThreadingHTTPServer,invalidate_dashboard,history_page
from monitor.store import utc_now
from monitor.ai import RiskAccountResult
from monitor.controls import control
from monitor.page_data import page_data
import test_risk_accounts as risk
import test_autonomous as autonomous

ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'frontend'/'dist' if (ROOT/'frontend'/'dist'/'index.html').is_file() else ROOT/'monitor'/'static'

@contextmanager
def local_dashboard():
    f=risk.RiskAccountTests();f.setUp()
    f.m.publish_for_web(f.snap,None)
    data=autonomous.response().model_dump()
    data['summary']='浏览器测试判断'
    data['account_decisions']=f.decisions(low_empty={'action':'buy','execution':'limit','amount_cny':2000,'limit_price_cny':990,'risk_exit_cny':950},high_full={'action':'sell','grams':1})
    f.a._run_codex=Mock(return_value=('test notes',[],RiskAccountResult.model_validate(data)))
    context={'snapshot_at':f.snap['ts'].isoformat(),'xauusd':4000,'trading_accounts':f.m.portfolios.input('codex',f.snap['execution_quote'])}
    f.a.analyze(context,'browser test');f.m.portfolios.on_tick(f.snap)
    class Local(Handler):
        password='';static_root=STATIC
        def log_message(self,*args):pass
    Local.settings=f.settings
    server=ThreadingHTTPServer(('127.0.0.1',0),Local)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield f,f'http://127.0.0.1:{server.server_port}'
    finally:server.shutdown();server.server_close();worker.join();f.tearDown()


@unittest.skipUnless(importlib.util.find_spec('playwright'),'Playwright is optional')
class DashboardBrowserTests(unittest.TestCase):
    def browser(self,p,width=390):return p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])

    def test_vue_pages_are_loaded_on_demand_and_cached_without_bulk_dashboard(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            b=self.browser(p);page=b.new_page(viewport={'width':390,'height':844});requests=[];errors=[]
            page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(url+'/#overview');page.locator('#panel-overview[data-page-ready="true"]').wait_for()
                self.assertTrue(page.evaluate('!!document.getElementById("app").__vue_app__'))
                api=[x.split('/api/')[1] for x in requests if '/api/' in x]
                self.assertEqual(api,['pages/overview']);self.assertFalse(any('/api/dashboard' in x for x in requests))
                self.assertEqual(page.locator('#panel-overview .market-quotes .quote-tile').count(),3)
                self.assertNotIn('期货',page.locator('#panel-overview .market-quotes').inner_text())
                self.assertFalse(any('MarketPage-' in x or 'Chart-' in x for x in requests))
                page.get_by_role('tab',name='行情',exact=True).click();page.locator('#panel-market[data-page-ready="true"]').wait_for()
                self.assertTrue(any('MarketPage-' in x for x in requests))
                page.get_by_role('tab',name='总览',exact=True).click();page.locator('#panel-overview').wait_for()
                self.assertEqual(sum('/api/pages/overview' in x for x in requests),1)
                for width in (390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    for label,key in [('账户','strategies'),('操盘对比','compare'),('AI','ai'),('行情','market'),('操盘记录','records'),('通知','notifications')]:
                        page.get_by_role('tab',name=label,exact=True).click();page.locator('#panel-'+key+'[data-page-ready="true"]').wait_for()
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),label)
                self.assertEqual(errors,[])
            finally:b.close()

    def test_account_filters_baselines_expiry_and_record_details(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            b=self.browser(p);page=b.new_page(viewport={'width':390,'height':844})
            try:
                page.goto(url+'/#strategies');page.locator('#panel-strategies details.asset-account').first.wait_for()
                self.assertEqual(page.locator('details.asset-account').count(),12)
                self.assertEqual(page.locator('.asset-benchmark').count(),12)
                page.get_by_role('combobox',name='资产模型').select_option('codex')
                page.get_by_role('combobox',name='风险场景').select_option('low')
                page.get_by_role('combobox',name='资产起点').select_option('empty')
                self.assertEqual(page.locator('details.asset-account').count(),1)
                detail=page.locator('details.asset-account');detail.locator('summary').click()
                page.get_by_role('button',name='刷新数据').click();page.wait_for_function("!document.getElementById('updated').textContent.includes('刷新中')")
                self.assertTrue(detail.evaluate('n=>n.open'))
                page.get_by_role('tab',name='操盘对比',exact=True).click();page.get_by_role('heading',name='六账户收益与基准').wait_for()
                self.assertIn('满仓持有不动',page.locator('.compare-mobile').inner_text());self.assertIn('空仓持币不动',page.locator('.compare-mobile').inner_text())
                self.assertEqual(page.locator('#panel-compare tbody tr').count(),12)
                page.get_by_role('tab',name='AI',exact=True).click();page.get_by_role('switch',name='Codex 操盘',exact=True).wait_for()
                page.get_by_role('combobox',name='账户模型').select_option('codex')
                self.assertIn('待成交',page.locator('#panel-ai').inner_text())
                page.evaluate('window.originalDateNow=Date.now;Date.now=()=>window.originalDateNow()+86400000')
                page.wait_for_timeout(1200)
                self.assertIn('委托已到期，不再执行',page.locator('#panel-ai').inner_text())
                self.assertNotIn('委托有效至',page.locator('#panel-ai').inner_text())
                page.evaluate('Date.now=window.originalDateNow')
                page.get_by_role('tab',name='操盘记录',exact=True).click();page.get_by_text('查看决策依据',exact=True).first.wait_for()
                page.get_by_role('combobox',name='筛选模型').select_option('codex')
                page.get_by_text('查看决策依据',exact=True).first.wait_for()
                page.get_by_role('button',name='AI 决策',exact=True).click();page.get_by_text('保护设置与决策详情',exact=True).first.wait_for()
                first=page.locator('#panel-records details').first;first.locator('summary').click()
                page.get_by_role('button',name='刷新数据').click();page.wait_for_timeout(200)
                self.assertTrue(first.evaluate('n=>n.open'))
                page.screenshot(path='/tmp/gold-vue-records-mobile.png',full_page=True)
            finally:b.close()

    def test_model_controls_hide_cached_pages_and_manual_request_only_queues(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            b=self.browser(p);page=b.new_page(viewport={'width':1280,'height':900})
            try:
                page.goto(url+'/#strategies');page.locator('details.asset-account').first.wait_for()
                before=f.m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0]
                page.get_by_role('tab',name='操盘记录',exact=True).click();page.locator('#panel-records .trade-record').first.wait_for()
                self.assertIn('Claude',page.locator('#panel-records').inner_text())
                page.get_by_role('tab',name='AI',exact=True).click();page.get_by_role('switch',name='Claude 操盘',exact=True).wait_for()
                page.get_by_role('switch',name='Claude 操盘',exact=True).click()
                page.wait_for_function("document.querySelector('[aria-label=\"Claude 操盘\"]')?.getAttribute('aria-checked')==='false'")
                self.assertFalse(control(f.m.store.conn,'claude')['enabled'])
                held=[]
                page.route('**/api/history?*',lambda route:held.append(route))
                page.get_by_role('tab',name='操盘记录',exact=True).click();page.locator('#panel-records').wait_for()
                page.wait_for_timeout(100)
                self.assertNotIn('Claude',page.locator('#panel-records').inner_text())
                self.assertEqual(len(held),1)
                held[0].fulfill(status=200,content_type='application/json',body=json.dumps(history_page(f.settings,{'kind':['fills'],'limit':['20'],'compact':['1']})))
                page.unroute('**/api/history?*')
                page.get_by_role('tab',name='账户',exact=True).click();page.locator('details.asset-account').first.wait_for()
                self.assertEqual(page.locator('details.asset-account').count(),6)
                self.assertNotIn('Claude',page.locator('#panel-strategies').inner_text())
                page.get_by_role('tab',name='操盘对比',exact=True).click();page.locator('#panel-compare tbody tr').first.wait_for()
                self.assertEqual(page.locator('#panel-compare tbody tr').count(),6)
                page.get_by_role('tab',name='AI',exact=True).click();page.get_by_role('button',name='Codex 立即分析并操盘',exact=True).click()
                page.wait_for_function("document.querySelector('[aria-label=\"Codex 立即分析并操盘\"]')?.textContent.includes('手动请求已排队')")
                self.assertEqual(f.m.store.get_state('ai_manual:codex')['status'],'queued')
                self.assertEqual(f.m.store.conn.execute('SELECT COUNT(*) FROM ai_portfolio_fills').fetchone()[0],before)
                page.reload();page.get_by_role('switch',name='Claude 操盘',exact=True).wait_for()
                self.assertEqual(page.get_by_role('switch',name='Claude 操盘',exact=True).get_attribute('aria-checked'),'false')
            finally:b.close()

    def test_notification_mutation_race_failure_and_reload(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            b=self.browser(p);page=b.new_page(viewport={'width':390,'height':844});held=[]
            try:
                page.goto(url+'/#notifications');toggle=page.get_by_role('switch',name='Codex · 低风险短线 通知',exact=True);toggle.wait_for()
                stale=page_data(f.settings,'notifications')
                page.route('**/api/pages/notifications',lambda route:held.append(route))
                page.get_by_role('button',name='刷新数据').click();page.wait_for_timeout(100);self.assertEqual(len(held),1)
                toggle.click();page.wait_for_function("document.querySelector('[aria-label=\"Codex · 低风险短线 通知\"]').getAttribute('aria-checked')==='true'")
                held[0].fulfill(status=200,content_type='application/json',body=json.dumps(stale));page.wait_for_timeout(100)
                self.assertEqual(toggle.get_attribute('aria-checked'),'true')
                page.unroute('**/api/pages/notifications');page.reload();toggle.wait_for();self.assertEqual(toggle.get_attribute('aria-checked'),'true')
                page.route('**/api/notifications',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"通知设置保存失败，请重试"}'))
                toggle.click();page.get_by_role('alert').filter(has_text='通知设置保存失败').wait_for()
                self.assertEqual(toggle.get_attribute('aria-checked'),'true');self.assertFalse(toggle.is_disabled())
                self.assertNotIn('行情与报告',page.locator('#panel-notifications').inner_text())
            finally:b.close()

    def test_market_calendar_masks_future_actual_and_restores_filters(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            now=utc_now()
            f.m.store.set_state('web_macro',{'fetched_at':now.isoformat(),'quotes_fetched_at':now.isoformat(),'calendar_fetched_at':now.isoformat(),
                'quotes':{'美元指数':{'value':100.1,'change_pct':.3}},'calendar':[
                {'time_utc':(now+timedelta(minutes=20)).isoformat(),'title':'Inflation Rate YoY','country':'US','tier':1,'actual':9876,'forecast':0,'previous':2,'released':True},
                {'time_utc':(now-timedelta(minutes=20)).isoformat(),'title':'Retail Sales MoM','country':'US','tier':2,'actual':0,'forecast':.2,'previous':.3,'released':True}]})
            f.m.store.set_state('jin10_flash',[{'id':'one','important':True,'time_bj':'测试时刻','text':'重要黄金消息测试'},{'id':'two','important':False,'time_bj':'测试时刻','text':'普通黄金消息测试'}])
            b=self.browser(p);page=b.new_page(viewport={'width':390,'height':844})
            try:
                page.goto(url+'/#market/calendar');page.get_by_role('heading',name='未来七天经济日历',exact=True).wait_for()
                page.get_by_role('button',name='近24小时及未来7天',exact=True).click()
                text=page.locator('#panel-market').inner_text();self.assertNotIn('9876',text);self.assertIn('CPI 年率',text)
                self.assertIn('下一项重要事件',text);self.assertNotIn('重要黄金消息测试',text)
                page.get_by_role('button',name='已公布',exact=True).click();self.assertIn('零售销售月率',page.locator('#panel-market').inner_text())
                page.get_by_role('tab',name='黄金快讯',exact=True).click();page.get_by_role('heading',name='黄金相关快讯').wait_for()
                self.assertIn('重要黄金消息测试',page.locator('#panel-market').inner_text());self.assertNotIn('普通黄金消息测试',page.locator('#panel-market').inner_text())
                page.get_by_role('button',name='全部快讯',exact=True).click();self.assertIn('普通黄金消息测试',page.locator('#panel-market').inner_text())
                page.get_by_role('tab',name='金价走势',exact=True).click();page.get_by_role('heading',name='价格走势').wait_for()
                self.assertNotIn('逐日采样明细与缺口',page.locator('#panel-market').inner_text())
                page.get_by_role('tab',name='总览',exact=True).click();page.get_by_role('tab',name='行情',exact=True).click()
                page.get_by_role('tab',name='经济日历',exact=True).click()
                self.assertEqual(page.get_by_role('button',name='已公布',exact=True).get_attribute('aria-pressed'),'true')
                page.get_by_role('tab',name='黄金快讯',exact=True).click()
                self.assertEqual(page.get_by_role('button',name='全部快讯',exact=True).get_attribute('aria-pressed'),'true')
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            finally:b.close()

    def test_chart_decimal_prices_touch_selection_and_responsive_axes(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            value=page_data(f.settings,'market')
            value['wave_evidence']['jd_daily']={'status':'available','days':[
                {'date_bj':'2026-10-30','last_price_cny':836.12},
                {'date_bj':'2026-10-31','last_price_cny':None},
                {'date_bj':'2026-11-02','last_price_cny':847.45}]}
            value['wave_evidence']['daily_reference']={'status':'available','last_day':'2026-10-30',
                'recent_sessions':[{'date':'2026-10-29','close_usd':3901.23},{'date':'2026-10-30','close_usd':3898.90}],
                'changes_pct':{'5d':1.25,'20d':2.34,'60d':3.45}}
            b=self.browser(p);context=b.new_context(viewport={'width':390,'height':844},has_touch=True)
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/api/pages/market?*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(value)))
            try:
                page.goto(url+'/#market');chart=page.locator('#panel-market .native-chart').first
                chart.get_by_role('status').wait_for()
                self.assertIn('847.45 元/克',chart.inner_text());self.assertNotIn('08:00',chart.inner_text())
                self.assertEqual(chart.locator('.chart-values').count(),0)
                self.assertGreater(chart.locator('.chart-point-value').count(),0)
                self.assertTrue(chart.locator('.chart-inline-tooltip').evaluate("e=>{const a=e.getBoundingClientRect(),b=e.parentElement.getBoundingClientRect();return a.x>=b.x&&a.right<=b.right+1&&a.y>=b.y&&a.bottom<=b.bottom}"))
                self.assertNotIn('逐日采样明细与缺口',page.locator('#panel-market').inner_text())
                svg=chart.locator('svg');chart.evaluate("e=>e.scrollIntoView({block:'center'})")
                page.wait_for_function("(()=>{const e=document.querySelector('#panel-market .native-chart svg');return Math.abs(e.viewBox.baseVal.width-e.getBoundingClientRect().width)<2})()")
                self.assertTrue(svg.evaluate("e=>Array.from(e.querySelectorAll('.chart-y-tick')).every(t=>t.getBBox().x>=0&&t.getBoundingClientRect().height>=9)"))
                self.assertTrue(all('.' in t for t in chart.locator('.chart-y-tick').all_text_contents()))
                self.assertEqual(len(chart.locator('polyline').get_attribute('points').split()),2)
                point=svg.evaluate("e=>{const r=e.getBoundingClientRect(),p=e.querySelector('polyline').points.getItem(0);return {x:r.x+p.x*r.width/e.viewBox.baseVal.width,y:r.y+p.y*r.height/e.viewBox.baseVal.height}}")
                page.touchscreen.tap(point['x'],point['y'])
                self.assertIn('836.12 元/克',chart.get_by_role('status').inner_text())
                svg.dispatch_event('pointerleave',{'pointerType':'touch'})
                self.assertIn('836.12 元/克',chart.get_by_role('status').inner_text())
                chart.screenshot(path='/tmp/gold-chart-price-mobile.png')
                page.get_by_role('tab',name='市场参考',exact=True).click();page.get_by_text('国际黄金期货历史参考',exact=True).click()
                self.assertIn('3,898.90 美元/盎司',chart.get_by_role('status').inner_text())
                self.assertNotIn('836.12',chart.get_by_role('status').inner_text())
                self.assertIn('5日 +1.25%',page.locator('#panel-market').inner_text())
                page.set_viewport_size({'width':1280,'height':900})
                page.wait_for_function("(()=>{const e=document.querySelector('#panel-market .native-chart svg');return e.viewBox.baseVal.width>700&&Math.abs(e.viewBox.baseVal.width-e.getBoundingClientRect().width)<2})()")
                self.assertTrue(svg.evaluate("e=>Array.from(e.querySelectorAll('.chart-y-tick')).every(t=>t.getBBox().x>=0)"))
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.get_by_role('tab',name='操盘对比',exact=True).click()
                compare=page.locator('#panel-compare .native-chart');compare.locator('svg').wait_for()
                compare.locator('svg').scroll_into_view_if_needed()
                box=compare.locator('svg').bounding_box();page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
                self.assertRegex(compare.get_by_role('status').inner_text(),r'\d+\.\d{2}%')
                self.assertEqual(errors,[])
            finally:context.close();b.close()


    def test_market_subpages_load_independently_restore_scroll_and_move_diagnostics(self):
        from playwright.sync_api import sync_playwright
        with local_dashboard() as (f,url),sync_playwright() as p:
            f.m.store.set_state('jin10_flash',[{'id':str(i),'important':i%2==0,'time_bj':'测试时刻','text':'黄金快讯测试 '+str(i)} for i in range(30)])
            b=self.browser(p);page=b.new_page(viewport={'width':390,'height':650});requests=[];errors=[]
            page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(url+'/#market/news');page.locator('[data-market-view="news"]').wait_for()
                self.assertEqual([r.split('/api/')[1] for r in requests if '/api/' in r],['pages/market?view=news'])
                self.assertFalse(any('PricePage-' in r or 'CalendarPage-' in r or 'ReferencePage-' in r or '/Chart-' in r for r in requests))
                page.get_by_role('button',name='全部快讯',exact=True).click()
                page.get_by_role('button',name='显示更多快讯 →',exact=True).click()
                self.assertEqual(page.locator('[data-market-view="news"] .list li').count(),16)
                page.evaluate('window.scrollTo(0,450)');page.wait_for_timeout(80);offset=page.evaluate('scrollY')
                page.get_by_role('tab',name='金价走势',exact=True).click();page.locator('[data-market-view="prices"]').wait_for()
                page.get_by_role('tab',name='黄金快讯',exact=True).click();page.locator('[data-market-view="news"]').wait_for()
                page.wait_for_function('y=>Math.abs(scrollY-y)<3',arg=offset)
                self.assertEqual(page.locator('[data-market-view="news"] .list li').count(),16)
                self.assertEqual(sum('/api/pages/market?view=news' in r for r in requests),1)
                page.get_by_role('tab',name='通知',exact=True).click();page.get_by_role('heading',name='微信操作通知').wait_for()
                page.get_by_role('tab',name='行情',exact=True).click();page.locator('[data-market-view="news"]').wait_for()
                page.wait_for_function('y=>Math.abs(scrollY-y)<3',arg=offset)
                page.get_by_role('tab',name='市场参考',exact=True).click();page.get_by_role('heading',name='宏观指标').wait_for()
                self.assertEqual(page.get_by_role('heading',name='京东参考接口状态').count(),0)
                self.assertFalse(any('/api/pages/diagnostics' in r for r in requests))
                page.get_by_role('button',name='接口状态与额度 →',exact=True).click()
                page.get_by_role('heading',name='京东参考接口状态').wait_for()
                self.assertTrue(page.locator('#system-diagnostics').evaluate('e=>e.open'))
                self.assertEqual(sum('/api/pages/diagnostics' in r for r in requests),1)
                self.assertEqual(errors,[])
                page.get_by_role('tab',name='行情',exact=True).click();page.locator('[data-market-view="reference"]').wait_for()
                for label,key in [('金价走势','prices'),('黄金快讯','news'),('经济日历','calendar'),('市场参考','reference')]:
                    page.get_by_role('tab',name=label,exact=True).click();page.locator('[data-market-view="'+key+'"]').wait_for()
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.get_by_role('tab',name='金价走势',exact=True).click();page.locator('[data-market-view="prices"]').wait_for()
                page.screenshot(path='/tmp/gold-market-subpages-mobile.png',full_page=True)
            finally:b.close()
