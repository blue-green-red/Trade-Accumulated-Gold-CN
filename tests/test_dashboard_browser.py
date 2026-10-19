"""Browser integration using a temporary database and local server only."""
import importlib.util
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from monitor.config import Settings
from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, MarketView
from monitor.store import utc_now
from monitor.web import Handler, ThreadingHTTPServer
from monitor.controls import control


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class DashboardBrowserTests(unittest.TestCase):
    def test_render_compare_switch_and_reload(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings(), ai_trading_mode="probability", db_path=Path(tmp) / "test.db", ai_analysts=("claude", "codex"),
                               ai_backend="api", anthropic_api_key="", codex_cli="missing-codex", claude_cli="missing-claude")
            monitor = Monitor(settings, True)
            market = {"ts": utc_now(), "market_open": True, "xauusd": 4000, "change_pct": .3,
                      "day_high": 4100, "day_low": 3900, "day_open": 3998,
                      "fx": {"rate": 7.1234, "fetched_at": utc_now().isoformat()},
                      "bank_est": {"zheshang": 1000, "minsheng": 1001},
                      "ratings": {"30m": .6, "1h": .6, "4h": .6, "1d": .6},
                      "signals": {"30m": "买入", "1H": "买入", "4H": "买入"},
                      "execution_quote": {"price": 1000, "source": "jd_latest_price_api", "quote_at": utc_now().isoformat(),
                                          "fetched_at": utc_now().isoformat(), "product_sku": "test-jd"},
                      "ratios": {"zheshang": {"ratio": .25}}, "tv": {"ema20_1h": 3998, "sma20_1d": 3999}}
            views = {}
            for name in ("claude", "codex"):
                analyst = AIAnalyst(settings, monitor.store, "codex", analyst=name)
                data = MarketView(bias_short="bullish", bias_long="bullish", confidence="medium",
                                  p_up_3d=.7, p_down_3d=.1, p_up_3m=.7, p_down_3m=.1,
                                  hard_risk_event=False, hard_risk_note="", drivers=[], upcoming_events=[], summary="浏览器测试判断")
                analyst._run_codex = Mock(return_value=("test notes", [], data))
                analyst.analyze({"xauusd": 4000}, "test", run_id="test-round")
                views[name] = analyst.latest(1)
            cost = {"spread_cny": 0, "sell_fee_rate": .004, "ratio_bank": "zheshang"}
            plans = {name: monitor.build_ai_led_plan(market, v, cost) for name, v in views.items()}
            monitor.store.add_tick(market)
            for name in ("claude", "codex"):
                monitor.short_books[f"{name}:short_ai"].on_tick(market, plans[name])
            monitor.publish_for_web(market, plans["claude"], ai_plans=plans)
            monitor.store.set_state("ai_inputs", {"at": utc_now().isoformat(), "context_chars": 4200,
                "news_count": 18, "max_searches": 0, "macro_at": utc_now().isoformat(),
                "jd_research": {"funds": {"status": "fresh"}, "orders": {"status": "stale"}}})
            now = utc_now()
            monitor.store.set_state("web_macro", {"fetched_at": now.isoformat(),
                "quotes_fetched_at": now.isoformat(), "calendar_fetched_at": now.isoformat(),
                "calendar_refresh_seconds": 60,
                "quotes": {"美元指数": {"value": 100.1, "change_pct": .3},
                    "美国10年期国债收益率": {"value": 4.123, "change_pct": -.2},
                    "美国2年期国债收益率": {"value": 3.456, "change_pct": -.1},
                    "离岸人民币 USD/CNH": {"value": 7.1245, "change_pct": .02}},
                "calendar": [{"time_utc": (now + timedelta(minutes=10)).isoformat(), "country": "US", "tier": 1,
                    "title": "Inflation Rate YoY", "released": False, "forecast": 2.5, "previous": 2.7},
                    {"time_utc": (now - timedelta(hours=1)).isoformat(), "country": "US", "tier": 2,
                    "title": "Retail Sales MoM", "released": True, "actual": 0, "forecast": .3, "previous": .1}]})
            monitor.store.set_state("jin10_flash", [
                {"id": "important", "important": True, "time_bj": "2026-10-16 10:00:00", "text": "重要黄金消息测试"},
                {"id": "normal", "important": False, "time_bj": "2026-10-16 09:59:00", "text": "普通黄金消息测试"}])
            for kind, row in (("funds", {"publishTime": now.timestamp() * 1000,
                "longPositionRatio": 60, "shortPositionRatio": 40, "tradingVolume": 2000000}),
                ("orders", {"time": now.timestamp(), "price": 900,
                    "levels": [{"price": 899, "ol": 3.2, "os": 1.1}]})):
                monitor.store.set_state("jd_research:" + kind, {"fetched_at": now.isoformat(),
                    "raw": {"success": True, "code": "0000", "data": [json.dumps(row)]}})
            class LocalHandler(Handler):
                password = ""
                def log_message(self, *args): pass
            LocalHandler.settings = settings
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        page.get_by_role("tab", name="AI", exact=True).click()
                        page.get_by_role("switch", name="Claude 操盘").wait_for()
                        page.get_by_role("tab", name="操盘对比", exact=True).click()
                        page.get_by_text("同类策略对比", exact=True).wait_for()
                        self.assertEqual(page.locator("#panel-compare tbody tr").count(), 4)
                        self.assertEqual(page.locator("#panel-compare").get_by_text("AI 定投", exact=True).count(), 0)
                        page.get_by_text("3 个对照账户", exact=True).wait_for()
                        page.get_by_role("tab", name="AI", exact=True).click()
                        page.get_by_text("本轮分析效率", exact=True).first.click()
                        self.assertIn("总耗时", page.locator("#panel-ai").inner_text())
                        self.assertIn("Token", page.locator("#panel-ai").inner_text())
                        page.get_by_role("switch", name="Claude 操盘").click()
                        page.wait_for_function("document.querySelector('#panel-ai [aria-label=\"Claude 操盘\"]').getAttribute('aria-checked') === 'false'")
                        self.assertFalse(control(monitor.store.conn, "claude")["enabled"])
                        self.assertTrue(page.get_by_role("button",name="Claude 立即分析并操盘",exact=True).is_disabled())
                        self.assertTrue(control(monitor.store.conn, "codex")["enabled"])
                        page.reload()
                        page.get_by_role("switch", name="Claude 操盘").wait_for()
                        self.assertEqual(page.get_by_role("switch", name="Claude 操盘").get_attribute("aria-checked"), "false")
                        page.get_by_role("tab", name="AI", exact=True).click()
                        page.get_by_text("模型已关闭；以上为历史判断，持仓仅保留风控", exact=True).wait_for()
                        page.get_by_text("本轮分析输入详情", exact=True).click()
                        page.get_by_text("共同分析数据", exact=True).wait_for()
                        self.assertIn("行情快照", page.locator("#panel-ai").inner_text())
                        self.assertIn("快讯 18 条", page.locator("#panel-ai").inner_text())
                        self.assertIn("挂单 来源数据过期", page.locator("#panel-ai").inner_text())
                        page.get_by_role("tab", name="AI", exact=True).click()
                        page.get_by_role("switch", name="Claude 操盘").click()
                        page.get_by_text("已重新开启，等待新判断；以上判断不用于开仓", exact=True).wait_for()
                        page.get_by_role("tab", name="行情", exact=True).click()
                        market_panel = page.locator("#panel-market")
                        market_panel.get_by_role("heading", name="宏观指标", exact=True).wait_for()
                        self.assertIn("美国 CPI 年率", market_panel.inner_text())
                        self.assertNotIn("美国零售销售月率", market_panel.inner_text())
                        market_panel.get_by_role("button", name="已公布", exact=True).click()
                        self.assertIn("实际 0", market_panel.inner_text())
                        market_panel.get_by_role("button", name="待公布", exact=True).click()
                        market_panel.get_by_role("button", name="只看重要", exact=True).click()
                        self.assertIn("重要黄金消息测试", market_panel.inner_text())
                        self.assertNotIn("普通黄金消息测试", market_panel.inner_text())
                        self.assertEqual(market_panel.get_by_text("参考数据可用", exact=True).count(), 2)
                        self.assertIn("多单 60.0%", market_panel.inner_text())
                        self.assertIn("价格数据积累中", page.locator("#price-chart").inner_text())
                        self.assertEqual(market_panel.locator(".cluster-row").first.locator(":scope > div").count(), 2)
                        page.evaluate("window.scrollTo(0,0)")
                        page.screenshot(path="/tmp/gold-market-desktop.png", full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        page.evaluate("window.scrollTo(0,0)")
                        page.screenshot(path="/tmp/gold-market-mobile.png", full_page=True)
                        page.evaluate("renderMarket({...DATA, macro:{...DATA.macro,quotes_fetched_at:'2000-01-01T00:00:00Z',refresh_errors:{calendar:'TimeoutError'}}})")
                        self.assertIn("更新延迟", market_panel.inner_text())
                        self.assertIn("刷新失败 · 保留旧值", market_panel.inner_text())
                        page.evaluate("renderMarket({...DATA,macro:{},jd_research:{},jin10:[]})")
                        self.assertIn("日历尚未获取", market_panel.inner_text())
                        self.assertIn("暂无可用资金事件", market_panel.inner_text())
                        page.evaluate("renderMarket({...DATA,jd_research:{funds:{status:'quota_exhausted',error_type:'ProviderDailyQuota'},orders:{status:'quota_exhausted'},quota:{used_today:10,daily_cap:10,status:'quota_exhausted',retry_at:'2026-10-17T00:00:00Z',recent_requests:[{kind:'funds',started_at:'2026-10-16T00:00:00Z',status:'quota_exhausted',error_type:'ProviderDailyQuota'}]}}})")
                        self.assertIn("京东额度耗尽",market_panel.inner_text())
                        self.assertIn("今日 10/10 次",market_panel.inner_text())
                        self.assertIn("本地重试时间",market_panel.inner_text())
                        market_panel.get_by_text("最近接口请求",exact=True).click()
                        self.assertIn("供应商报告当日额度耗尽",market_panel.inner_text())
                        page.evaluate("renderMarket({...DATA,jd_research:{funds:{status:'unavailable',error_type:'TimeoutError'},orders:{status:'cache_stale'}}})")
                        self.assertIn("获取失败",market_panel.inner_text())
                        self.assertIn("缓存待刷新",market_panel.inner_text())
                        self.assertIn("接口请求超时",market_panel.inner_text())
                        page.evaluate("renderMarket(DATA)")
                        page.set_viewport_size({"width": 1280, "height": 900})
                        page.get_by_role("tab", name="操盘记录", exact=True).click()
                        page.get_by_role("combobox", name="筛选模型").select_option("claude")
                        page.get_by_role("combobox", name="筛选时间").select_option("7")
                        page.get_by_text("查看决策依据", exact=True).first.wait_for()
                        self.assertGreater(page.get_by_text("查看决策依据", exact=True).count(), 0)
                        page.get_by_role("tab", name="账户", exact=True).click()
                        page.get_by_role("button", name="全部 7 个账户", exact=True).click()
                        page.get_by_role("button", name="对照组", exact=True).click()
                        page.get_by_role("button", name="AI 账户", exact=True).click()
                        page.get_by_role("tab", name="操盘对比", exact=True).click()
                        page.screenshot(path="/tmp/gold-ai-comparison-desktop.png", full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.wait_for_timeout(250)
                        self.assertTrue(page.locator("#panel-compare .compare-mobile").is_visible())
                        self.assertFalse(page.locator("#panel-compare .compare-desktop").is_visible())
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        page.screenshot(path="/tmp/gold-ai-comparison-mobile.png", full_page=True)
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                monitor.store.conn.close()

    def test_six_account_actions_filters_and_comparison_mobile(self):
        from playwright.sync_api import sync_playwright
        from test_autonomous import response, snapshot
        with tempfile.TemporaryDirectory() as tmp:
            settings=replace(Settings(),ai_trading_mode="six_account",db_path=Path(tmp)/"six.db",anthropic_api_key="",
                             claude_cli="missing-claude",codex_cli="missing-codex",clawx_api_key="")
            monitor=Monitor(settings,True)
            for name in settings.ai_analysts: monitor.store.set_state("ai_status:"+name,{"available":True})
            snap=snapshot()
            monitor.portfolios.on_tick(snap)
            analyst=AIAnalyst(settings,monitor.store,"codex",analyst="codex")
            value=response(short_empty=dict(action="buy",amount_cny=100001,stop_loss_cny=950),
                medium_empty=dict(action="buy",execution="limit",amount_cny=10000,limit_price_cny=990,stop_loss_cny=950),
                short_full=dict(action="sell",grams=10))
            analyst._run_codex=Mock(return_value=("notes",[],value))
            analyst.analyze({"snapshot_at":utc_now().isoformat(),"xauusd":4000,
                "trading_accounts":monitor.portfolios.input("codex",snap["execution_quote"])},"test")
            monitor.portfolios.on_tick(snap)
            monitor.publish_for_web(snap,None)
            class SixHandler(Handler):
                password=""
                def log_message(self,*args): pass
            SixHandler.settings=settings
            server=ThreadingHTTPServer(("127.0.0.1",0),SixHandler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,args=["--no-sandbox"])
                    try:
                        page=browser.new_page(viewport={"width":1280,"height":900})
                        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}/#ai")
                        page.get_by_role("heading",name="六账户自主操盘",exact=True).wait_for()
                        manual=page.get_by_role("button",name="Codex 立即分析并操盘",exact=True)
                        manual.click()
                        page.wait_for_function("DATA.model_controls.find(c=>c.key==='codex').manual.status==='queued'")
                        self.assertTrue(page.get_by_role("button",name="Codex 立即分析并操盘",exact=True).is_disabled())
                        self.assertEqual(analyst._run_codex.call_count,1)
                        page.reload()
                        page.get_by_role("heading",name="六账户自主操盘",exact=True).wait_for()
                        self.assertIn("手动请求已排队",page.locator("#panel-ai").inner_text())

                        panel=page.locator("#panel-ai")
                        panel.get_by_role("combobox",name="账户模型",exact=True).select_option("codex")
                        self.assertEqual(panel.get_by_role("heading",name="Codex ·",exact=False).count(),6)
                        self.assertIn("已拒绝",panel.inner_text())
                        self.assertIn("等待条件",panel.inner_text())
                        self.assertIn("未设置止损",panel.inner_text())
                        self.assertIn("可接受成交价",panel.inner_text())
                        page.evaluate("renderAI({...DATA,ai:{...DATA.ai,analysts:DATA.ai.analysts.map(v=>({...v,_price_guard:{invalidated_at:'2026-10-19T00:00:00Z',reason:'测试：分析期间行情异动'}}))}})")
                        self.assertIn("测试：分析期间行情异动",panel.inner_text())
                        self.assertIn("已有持仓保护继续有效",panel.inner_text())
                        page.evaluate("renderAI(DATA)")
                        panel.get_by_role("combobox",name="账户周期",exact=True).select_option("short")
                        panel.get_by_role("combobox",name="初始仓位",exact=True).select_option("empty")
                        self.assertEqual(panel.get_by_role("heading",name="Codex ·",exact=False).count(),1)
                        self.assertIn("金额 100,001",panel.inner_text())
                        self.assertNotIn("当前仓位",panel.inner_text())
                        page.evaluate("window.scrollTo(0,0)")
                        page.screenshot(path="/tmp/gold-six-desktop.png",full_page=True)
                        page.get_by_role("tab",name="操盘对比",exact=True).click()
                        self.assertEqual(page.locator("#panel-compare tbody tr").count(),12)
                        self.assertIn("600,000",page.locator("#panel-compare").inner_text())
                        page.set_viewport_size({"width":390,"height":844})
                        self.assertTrue(page.locator("#panel-compare .compare-mobile").is_visible())
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        page.screenshot(path="/tmp/gold-six-comparison-mobile.png",full_page=True)
                        page.get_by_role("tab",name="AI",exact=True).click()
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        page.screenshot(path="/tmp/gold-six-mobile.png",full_page=True)
                        page.get_by_role("tab",name="操盘记录",exact=True).click()
                        page.locator("#panel-records").get_by_text("实验初始化",exact=False).first.wait_for()
                        self.assertIn("实验初始化",page.locator("#panel-records").inner_text())
                        records=page.locator("#panel-records")
                        records.get_by_role("button",name="AI 决策",exact=True).click()
                        records.get_by_text("已拒绝",exact=False).first.wait_for()
                        self.assertIn("AI 操盘决策",records.inner_text())
                        self.assertIn("可接受成交价",records.inner_text())
                        self.assertIn("已拒绝",records.inner_text())
                        self.assertIn("等待条件",records.inner_text())
                        records.get_by_role("combobox",name="筛选模型",exact=True).select_option("codex")
                        records.get_by_text("已拒绝",exact=False).first.wait_for()
                        self.assertNotIn("Claude ·",records.inner_text())
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        details=records.locator("details").first
                        details.locator("summary").click()
                        self.assertTrue(details.evaluate("n=>n.open"))
                        page.evaluate("refreshHistory()")
                        page.wait_for_function("!historyBusy")
                        self.assertTrue(records.locator("details").first.evaluate("n=>n.open"))
                        page.get_by_role("tab",name="账户",exact=True).click()
                        assets=page.locator("#panel-strategies")
                        assets.get_by_role("combobox",name="资产模型",exact=True).select_option("codex")
                        assets.get_by_role("combobox",name="资产周期",exact=True).select_option("short")
                        assets.get_by_role("combobox",name="资产起点",exact=True).select_option("empty")
                        self.assertEqual(assets.locator("details.strategy").count(),1)
                        assets.locator("details.strategy summary").click()
                        self.assertIn("现金 100,000",assets.inner_text())
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                        self.assertEqual(errors,[])
                    finally: browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(3);monitor.store.conn.close()


if __name__ == "__main__":
    unittest.main()
