"""Self-contained synthetic WeChat detail cards; never access a user account or provider."""
import importlib.util
import os
from pathlib import Path
import unittest

from monitor.analysis_notice import fields
from monitor.jd_live_notice import body
from monitor.jd_live_risk import annotate
from monitor.notification_html import message
from monitor.notify import wechat_text
from test_analysis_notice import view


def examples():
    summary={'notification_topic':'codex:analysis',**fields(view(),'Codex')}
    account={'bank':'zheshang','fetched_at':'2026-11-12T05:58:00Z',
        'quote':{'quoted_at':'2026-11-12T05:59:00Z'},'pending_orders':{'status':'fresh','records':[]},
        'protection_state':{'status':'none'}}
    current={'id':1,'created_at':'2026-11-12T06:00:00Z','expires_at':'2026-11-12T10:00:00Z'}
    value={'action':'hold','price_condition':'reference','input_position_pct':20,'input_quote_cny':875,
        'risk_note':'以下均为合成测试情景，需本人确认；不保证成交。','reason':'保留现仓，等待新的已核实事实。',
        'suggested_orders':[{'kind':'take_profit_stop_loss','grams':20,'price_cny':900,'loss_price_cny':840,
            'expires_at':'2026-11-13T13:00:00Z','reason':'合成方案覆盖已确认现仓。'}],
        'review_conditions':[{'condition':'at_or_below','price_cny':840}],
        'order_management':[],'limit_buy_reviews':[]}
    source={'status':'fresh','cash_cny':70000,'total_grams':20,'average_cost_cny':860,'sell_fee_rate':.004,
        'quote':{'status':'fresh','sell_price_cny':875}}
    annotate(value,source)
    real={'notification_topic':'codex:live_zheshang','title':'💡Codex账户建议｜浙商｜持有·条件单建议',
          'body':body(account,current,value)}
    return {'analysis':summary,'real':real}


@unittest.skipUnless(importlib.util.find_spec('playwright'),'Playwright is optional')
class NotificationLayoutBrowserTests(unittest.TestCase):
    def test_cards_fit_mobile_and_desktop_with_complete_trade_parameters_and_no_external_resources(self):
        from playwright.sync_api import sync_playwright,expect
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                page=browser.new_page();requests=[];errors=[]
                page.on('request',lambda request:requests.append(request.url));page.on('pageerror',lambda error:errors.append(str(error)))
                for name,e in examples().items():
                    content=wechat_text(message(e['title'],[e]),html=True)
                    page.set_content('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+content)
                    self.assertEqual(page.locator('h1').count(),1)
                    self.assertNotIn('Codex',page.locator('body').inner_text());self.assertNotIn('##',page.locator('body').inner_text())
                    if name=='analysis':
                        expect(page.locator('main')).to_contain_text('3个交易日：涨 21% · 跌 65%')
                        expect(page.locator('main')).to_contain_text('3个月：涨 31% · 跌 32%')
                        self.assertNotIn('置信',page.locator('body').inner_text())
                    if name=='real':
                        self.assertIn('卖出20克\n止盈 ≥ 900元/克\n止损 ≤ 840元/克\n截止11-13 21:00',page.locator('main').inner_text())
                        expect(page.locator('main')).to_contain_text('本笔扣费盈亏：+728.00元')
                        expect(page.locator('main')).to_contain_text('本笔扣费盈亏：-467.20元')
                        expect(page.locator('main')).to_contain_text('整户相对分析时估值变化：-697.20元')
                        expect(page.locator('main')).to_contain_text('未发现已挂组合止盈止损单')
                        expect(page.locator('main')).to_contain_text('到价只触发重新分析')
                    for width in (360,390,1280):
                        page.set_viewport_size({'width':width,'height':900})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(name,width))
                        self.assertLess(page.locator('header').bounding_box()['y'],page.locator('main').bounding_box()['y'])
                        if os.environ.get('GOLD_NOTICE_PREVIEW_DIR'):
                            page.screenshot(path=str(Path(os.environ['GOLD_NOTICE_PREVIEW_DIR'])/(name+'-'+str(width)+'.png')),full_page=True)
                self.assertEqual(requests,[]);self.assertEqual(errors,[])
            finally:browser.close()
