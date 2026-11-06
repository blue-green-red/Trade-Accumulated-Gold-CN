"""Fee attribution and pending-entry review are mobile screens loaded on demand."""
import unittest


class SimulationAttributionBrowserTests(unittest.TestCase):
    def test_attribution_reconciles_and_pending_review_is_visible_without_loading_it_on_overview(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        with local_dashboard() as (f,url),sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':390,'height':844});requests=[];errors=[]
            page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(url+'/#overview');page.locator('#panel-overview[data-page-ready="true"]').wait_for()
                self.assertFalse(any('PnlBreakdown-' in u for u in requests))
                page.goto(url+'/#strategies/simulation/codex:standard_full')
                page.get_by_role('heading',name='盈亏拆解',exact=True).wait_for()
                panel=page.locator('.pnl-breakdown')
                expect(panel).to_contain_text('预计卖出手续费 · 尚未支付')
                expect(panel).to_contain_text('−400.00 元')
                expect(panel).to_contain_text('已付手续费')
                page.goto(url+'/#strategies/simulation/codex:low_empty')
                expect(page.locator('#panel-strategies')).to_contain_text('限价买入等待复核')
                expect(page.locator('#panel-strategies')).to_contain_text('同一入场条件连续 1 轮')
                page.goto(url+'/#strategies/simulation')
                page.get_by_text('筛选账户的盈亏归因',exact=True).click()
                expect(page.locator('.pnl-breakdown')).to_contain_text('价差盈亏 · 未扣费用')
                page.get_by_role('button',name='成绩对比 →',exact=True).click()
                page.get_by_text('查看盈亏归因',exact=True).first.click()
                expect(page.locator('.pnl-breakdown').first).to_contain_text('累计净盈亏')
                for width in (390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(errors,[])
            finally:browser.close()
