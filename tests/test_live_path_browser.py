"""Synthetic path and post-add coverage on the private mobile account page."""
import unittest
import json
from datetime import datetime,timedelta,timezone
from unittest.mock import patch


class LivePathBrowserTests(unittest.TestCase):
    def test_target_boundary_scope_and_uncovered_add_are_visible_without_overflow(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        from test_jd_live import payload,advice
        from test_live_risk import scenario
        from test_live_path import path
        from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,set_enabled_banks,model_input
        from monitor.jd_live_advice import save
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':True}),local_dashboard() as (f,url),sync_playwright() as p:
            c=f.m.store.conn;ensure_schema(c);set_enabled_banks(c,['zheshang']);now=datetime.now(timezone.utc)
            data=parse_account('zheshang',payload());data.update(total_grams=20,sellable_grams=20,frozen_grams=0,
                average_cost_cny=860,cash_cny=50000,cash_source='jd_electronic_account_api',pending_order_count=0,
                quote={'buy_price_cny':875,'sell_price_cny':875,'quoted_at':now.isoformat()},
                pending_orders={'records':[],'total_count':0,'complete':True},coverage={'pending_orders':'available'},
                component_times={'pending_orders':now.isoformat()})
            record_success(c,'zheshang',data,now.isoformat())
            v=advice();v.update(action='buy',grams=10,price_condition='reference',price_cny=875,position_path=path(),
                downside_scenarios=[scenario(840,'risk_limit','sell')],limit_buy_reviews=[],review_conditions=[],
                suggested_orders=[{'kind':'take_profit_stop_loss','grams':20,'price_cny':900,'loss_price_cny':840,
                    'expires_at':(now+timedelta(days=1)).isoformat(),'reason':'合成保护仅覆盖确认现仓。'}])
            save(c,'codex',999,{'zheshang':v},model_input(c,now),now);c.commit()
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page();page.clock.install();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url+'/#strategies/live/zheshang/advice')
                area=page.locator('[data-position-path]')
                comparison=page.locator('[data-participation-review]')
                expect(comparison).to_contain_text('对照规模为净资产1%和5%')
                comparison.locator('summary').click()
                expect(comparison).to_contain_text('追加对照（元）')
                for text in ('100.0%','840.00','当前这批仓位退出','若全部新增买入成交','保护不自动扩量','成交先后'):
                    expect(area).to_contain_text(text)
                expect(area).to_contain_text('30.0000 克');expect(area).to_contain_text('10.0000 克')
                for width in (360,390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                response=page.request.get(url+'/api/pages/strategies?view=live%2Fzheshang%2Fadvice').json()
                account=response['live_account'];entry=account['advice'][0]
                account['quote'].update(buy_price_cny=839,sell_price_cny=839)
                from monitor.participation_review import current,history
                earlier=(now-timedelta(minutes=10)).isoformat()
                entry['participation_review']={**current(50000,20,839,.004),'as_of':now.isoformat(),
                    'waiting':history({'created_at':earlier,'quote_at':earlier,'cash':50000,'grams':20,'price':875},2,839,.004,0,now.isoformat())}
                from monitor.jd_live_path import current_state
                current_state(entry,account)
                page.route('**/api/pages/strategies*',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(response)))
                page.reload()
                expect(page.locator('[data-position-path]')).to_contain_text('边界已触达 · 暂停追加')
                expect(page.locator('.live-advice-heading')).to_contain_text('买入已暂停')
                expect(page.locator('[data-participation-review]')).to_contain_text('价格变化贡献')
                expect(page.locator('[data-participation-review]')).to_contain_text('退出成本估计')
                self.assertEqual(errors,[])
                self.assertEqual(c.execute('SELECT COUNT(*) FROM jd_live_orders').fetchone()[0],0)
                page.clock.fast_forward(181000)
                expect(page.locator('[data-participation-review]')).to_contain_text('对照报价已过期')
            finally:browser.close()

    def test_simulation_detail_shows_whole_account_protection_and_reference_only_boundary(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        from monitor.ai import RiskAccountResult
        from unittest.mock import Mock
        import test_autonomous as fixtures
        with local_dashboard() as (f,url),sync_playwright() as p:
            snap=fixtures.snapshot()
            value=fixtures.response().model_dump()
            value['account_decisions']=f.decisions(high_empty={'action':'buy','execution':'limit','amount_cny':2000,
                'limit_price_cny':990,'risk_exit_cny':950,'position_path':{'target_position_pct':80,'boundary_source':'risk_exit',
                    'continuation':'合成分批方案，触参考边界暂停买单并重评，参考价不自动平仓。'}})
            f.a._run_codex=Mock(return_value=('test',[],RiskAccountResult.model_validate(value)))
            f.a.analyze({'snapshot_at':snap['ts'].isoformat(),'xauusd':4000,
                'trading_accounts':f.m.portfolios.input('codex',snap['execution_quote'])},'simulation-path-browser')
            f.m.portfolios.on_tick(snap)
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page();page.goto(url+'/#strategies/simulation/codex:high_empty')
                area=page.locator('[data-position-path]')
                comparison=page.locator('[data-participation-review]')
                expect(comparison).to_contain_text('±2%不是交易或复评阈值')
                comparison.locator('summary').click()
                expect(comparison).to_contain_text('追加对照（元）')
                for text in ('80.0%','950.00','模拟盘先执行','全仓','风险参考边界只暂停买单，不自动平仓'):
                    expect(area).to_contain_text(text)
                self.assertNotIn('京东委托',area.inner_text())
                for width in (360,390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            finally:browser.close()
