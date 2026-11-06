"""Synthetic real-account view: decision comparison and post-order allocation on mobile."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch


class LiveRiskBrowserTests(unittest.TestCase):
    def test_risk_comparison_and_order_effects_are_clear_on_mobile_and_desktop(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        from test_jd_live import payload,advice
        from test_live_risk import scenario
        from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,set_enabled_banks,model_input
        from monitor.jd_live_advice import save
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':True}),local_dashboard() as (f,url),sync_playwright() as p:
            c=f.m.store.conn;ensure_schema(c);set_enabled_banks(c,['zheshang'])
            now=datetime.now(timezone.utc)
            data=parse_account('zheshang',payload());data.update(total_grams=200,sellable_grams=200,frozen_grams=0,
                cash_cny=700000,cash_source='jd_electronic_account_api',
                quote={'buy_price_cny':875,'sell_price_cny':875,'quoted_at':now.isoformat()},pending_order_count=0,
                pending_orders={'records':[],'total_count':0,'complete':True},coverage={'pending_orders':'available'},
                component_times={'pending_orders':now.isoformat()})
            record_success(c,'zheshang',data,now.isoformat())
            expiry=(now+timedelta(days=1)).isoformat()
            value=advice();value.update(action='hold',grams=None,price_cny=875,price_condition='reference',
                limit_buy_reviews=[{'decision':'needs_review','entry_price_cny':850.,'target_price_cny':900.,'risk_exit_cny':820.,
                    'grams':10.,'expires_at':expiry,'basis':'独立候选，不默认使用旧压力价作为新增退出。',
                    'reason':'价格单无法区分数据公布后的两种相反情景，暂不提前挂出。'}],
                downside_scenarios=[scenario()],suggested_orders=[
                    {'kind':'take_profit_stop_loss','grams':100,'price_cny':900,'loss_price_cny':840,
                     'expires_at':expiry,'reason':'测试不一致方案，应提示核对。'},
                    {'kind':'limit_buy','grams':10,'price_cny':850,'expires_at':expiry,'reason':'独立限价方案。'}])
            save(c,'codex',999,{'zheshang':value},model_input(c,now),now);c.commit()
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':390,'height':844});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            try:
                page.goto(url+'/#strategies/live/zheshang/advice')
                risk=page.locator('[data-live-risk]');expect(risk).to_contain_text('20.0%')
                expect(risk).to_contain_text('34,972.00');expect(risk).to_contain_text('874,300.00')
                review=page.locator('[data-downside-review]');expect(review).to_contain_text('压力测试')
                entry=page.locator('[data-limit-buy-review]');expect(entry).to_contain_text('仍需复评，不建议提前挂出')
                expect(entry).to_contain_text('850.00');expect(entry).to_contain_text('820.00')
                expect(entry).to_contain_text('+5.46%');expect(entry).to_contain_text('−3.92%')
                expect(entry).to_contain_text('20.5%')
                expect(review).to_contain_text('倾向继续持有');expect(review).to_contain_text('加仓')
                expect(review).to_contain_text('减仓或退出');expect(review).to_contain_text('−0.80%')
                expect(page.locator('.live-advice')).to_contain_text('部分建议需核对')
                expect(page.locator('.live-advice')).to_contain_text('组合止损与下跌情景选择不一致')
                expect(page.locator('.live-advice')).to_contain_text('止损卖出后黄金仓位约 9.7%')
                for width in (390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.goto(url+'/#strategies/live/zheshang/holdings')
                expect(page.locator('[data-live-risk]')).to_contain_text('当前账户仓位与风险')
                self.assertEqual(errors,[])
            finally:browser.close()
