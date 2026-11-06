"""Private mobile/desktop advice shows independently validated sections consistently."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch


class LiveAdvicePartsBrowserTests(unittest.TestCase):
    def test_partial_status_preserves_valid_combo_terms_and_condition_states(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        from test_jd_live import payload,advice
        from monitor.jd_live_readonly import ensure_schema,parse_account,record_success,set_enabled_banks,model_input
        from monitor.jd_live_advice import save
        with patch('monitor.trading_hours.trading_session',return_value={'is_open':True}),local_dashboard() as (f,url),sync_playwright() as p:
            conn=f.m.store.conn;ensure_schema(conn);set_enabled_banks(conn,['zheshang'])
            now=datetime.now(timezone.utc);key='a'*24
            order={'order_key':key,'action':'sell','kind':'take_profit_stop_loss','grams':5.,'price_cny':880.,
                   'loss_price_cny':830.,'price_condition':'none','status':'pending',
                   'created_at':(now-timedelta(minutes=2)).isoformat(),'expires_at':(now+timedelta(days=1)).isoformat()}
            data=parse_account('zheshang',payload());data.update(cash_cny=100.,
                quote={'buy_price_cny':855.,'sell_price_cny':855.,'quoted_at':now.isoformat()},
                pending_order_count=1,pending_orders={'records':[order],'total_count':1,'complete':True},
                coverage={'pending_orders':'available'},component_times={'pending_orders':now.isoformat()})
            record_success(conn,'zheshang',data,now.isoformat())
            value=advice();value.update(price_condition='reference',price_cny=855.,grams=2.,
                order_management=[{'order_key':key,'action':'replace','replacement':{'expires_at':(now+timedelta(hours=8)).isoformat()},'reason':'仅缩短期限，保留原价和数量'}],
                review_conditions=[{'condition':'at_or_below','price_cny':850.,'reason':'跌破后复评'},
                                   {'condition':'at_or_below','price_cny':840.,'order_key':'b'*24,'reason':'关联错误需核对'}])
            save(conn,'codex',999,{'zheshang':value},model_input(conn,now),now);conn.commit()
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':390,'height':844});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(url+'/#overview')
                expect(page.locator('#panel-overview')).to_contain_text('部分建议需核对')
                page.goto(url+'/#strategies/live')
                expect(page.locator('#panel-strategies')).to_contain_text('部分建议需核对')
                page.goto(url+'/#strategies/live/zheshang/advice')
                card=page.locator('.live-advice').first
                expect(card.locator('.live-advice-status')).to_have_text('部分建议需核对')
                expect(card).to_contain_text('等待你确认处理');expect(card).to_contain_text('止盈 ≥ 880.00 / 止损 ≤ 830.00')
                expect(card).to_contain_text('监控中');expect(card).to_contain_text('需核对')
                page.goto(url+'/#strategies/live/zheshang/orders')
                card=page.locator('.live-advice').first
                expect(card.locator('.live-advice-status')).to_have_text('部分建议需核对')
                expect(card).to_contain_text('等待你确认处理');expect(card).to_contain_text('监控中')
                for width in (390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(errors,[])
            finally:browser.close()
