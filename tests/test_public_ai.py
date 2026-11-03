"""One model round, eleven public scenarios, a fail-closed anonymous boundary."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitor.ai import AIAnalyst, FourAccountResult, strict_schema
from monitor.config import Settings
from monitor.controls import set_enabled
from monitor.public_ai import (SCHEMA, POLICY, PROMPT, PublicContent, latest,
                               previous_analysis, save, validate_content, scenario_metrics)
from monitor.store import Store
from monitor.web import Handler, ThreadingHTTPServer

NOW = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)


def content():
    return {
        "analysis": {"outlook": "短期震荡，关注宏观变化。", "change_summary": "本轮重新评估市场方向。",
                     "drivers": ["金十快讯 11-03：美元波动影响黄金。"], "risk": "突发消息可能改变方向。",
                     "watch_conditions": []},
        "decision_basis": {"position_rationale": "暂无可证实的收益风险优势，维持各档现状；较高仓位承担更多波动，减仓另有费用。",
                           "entry_scenario": None, "no_entry_reason": "公开输入尚不能支持新的目标及退出假设。",
                           "no_conditions_reason": "事件公布后的方向未知，先复评再决定，不预设挂单。",
                           "review_triggers": [{"kind": "time", "at": (NOW + timedelta(hours=2)).isoformat(),
                                                "event": None, "condition": None, "price_cny": None}]},
        "recommendations": [{"position_pct": p, "action": "wait" if p == 0 else "hold",
                             "target_position_pct": p, "reason": "暂维持仓位，等待更清晰的市场信号。",
                             "invalidation": "市场方向改变时复评。", "conditions": []} for p in range(0, 101, 10)]}


def quote(price=850, at=NOW):
    return {"source": "jd_latest_price_api", "price": price, "quote_at": at.isoformat(), "fetched_at": at.isoformat()}


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), db_path=Path(self.tmp.name) / "public.db", ai_analysts=("claude", "codex"))
        self.store = Store(self.settings.db_path)
        self.store.conn.executescript(SCHEMA)
        self.context = {"snapshot_at": NOW.isoformat(), "jd_accumulation_quote": quote(),
                        "trading_accounts": {"accounts": {"high_full": {"key": "codex:high_full", "cash": 123456.78}}},
                        "live_readonly_accounts": {"accounts": {"zheshang": {"total_grams": 456.78,
                                                  "average_cost_cny": 1101.23, "pending_orders": {"records": [{"order_key": "private-order-1234", "price_cny": 881.23}]}}}}}
        self.store.set_state("web_snapshot", {"execution_quote": quote()})
        self.open = patch("monitor.trading_hours.trading_session", return_value={"is_open": True})
        self.open.start()
        self.addCleanup(self.open.stop)

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def publish(self, value=None, **kwargs):
        status = save(self.store.conn, "codex", 0, kwargs.pop("view_id", 1), NOW.isoformat(), NOW.isoformat(),
                      value if value is not None else content(), self.context, **kwargs)
        self.store.conn.commit()
        return status

    def read(self, **kwargs):
        return latest(self.store.conn, self.settings, now=kwargs.pop("now", NOW), **kwargs)

    def test_complete_sorted_scenarios_and_high_risk_policy(self):
        value = content()
        value["recommendations"].reverse()
        self.assertEqual(self.publish(value), "ready")
        result = self.read()
        self.assertEqual(result["status"], "current")
        self.assertEqual([r["position_pct"] for r in result["recommendations"]], list(range(0, 101, 10)))
        self.assertEqual(result["policy"]["risk"], "high")
        self.assertEqual(POLICY["trade_risk_pct"], 4)
        self.assertIn("不得为补字段强行买入或生成条件单", PROMPT)

    def test_duplicate_missing_and_unknown_fields_rejected(self):
        for mutate in (lambda d: d["recommendations"].pop(),
                       lambda d: d["recommendations"][0].update(position_pct=10),
                       lambda d: d.update(cash_cny=123456.78),
                       lambda d: d["analysis"].update(account_id="private")):
            d = content(); mutate(d)
            with self.subTest(value=d):
                with self.assertRaises(ValueError): validate_content(d, self.context)

    def test_position_action_and_conditional_targets_validated(self):
        d = content(); r = d["recommendations"][0]
        for target in (-10, 0, 101):
            r.update(action="buy", target_position_pct=target)
            with self.assertRaises(ValueError): validate_content(d, self.context)
        r.update(action="buy", target_position_pct=20)
        r["conditions"] = [{"action": "sell", "condition": "at_or_below", "price_cny": 830,
                             "target_position_pct": 10}]
        self.assertEqual(validate_content(d, self.context)["recommendations"][0]["target_position_pct"], 20)
        r["conditions"][0]["target_position_pct"] = 30
        with self.assertRaises(ValueError): validate_content(d, self.context)

    def test_private_prose_identifiers_balances_and_prices_block_publication(self):
        for text in ("真实账户应减仓", "模拟盘已获利", "codex:high_full", "private-order-1234",
                     "现金123456.78元", "持有456.78克", "参考价1101.23", "目标881.23",
                     "余额123456.78", "联络13812345678", "联系someone@example.test"):
            d = content(); d["analysis"]["outlook"] = text
            self.assertEqual(save(self.store.conn, "codex", 0, len(text) * 1000 + sum(map(ord, text)),
                                  NOW.isoformat(), NOW.isoformat(), d, self.context), "unavailable", text)

    def test_public_market_price_also_in_private_context_is_allowed(self):
        self.context["live_readonly_accounts"]["accounts"]["zheshang"]["average_cost_cny"] = 850
        d = content(); d["analysis"]["outlook"] = "公共京东参考价850元/克。"
        self.assertEqual(self.publish(d), "ready")

    def test_private_extra_fields_never_read_or_returned_and_no_legacy_fallback(self):
        self.store.conn.execute("CREATE TABLE ai_views(view_json TEXT)")
        self.store.conn.execute("INSERT INTO ai_views VALUES(?)", (json.dumps({"summary": "PRIVATE ACCOUNT SUMMARY"}),))
        self.assertIsNone(self.read()["analysis"])
        self.publish()
        self.store.set_state("private_secret", {"cash": "PRIVATE ACCOUNT BALANCE"})
        text = json.dumps(self.read(), ensure_ascii=False)
        for marker in ("PRIVATE", "trading_accounts", "live_readonly", "view_id", "123456.78", "order_key"):
            self.assertNotIn(marker, text)
        # A new invalid public result hides the prior valid result.
        save(self.store.conn, "codex", 0, 2, NOW.isoformat(), NOW.isoformat(), None, self.context)
        self.assertIsNone(self.read()["analysis"])

    def test_position_selection_only_reads_and_validates_query(self):
        self.publish()
        before = self.store.conn.total_changes
        for p in range(0, 101, 10):
            self.assertEqual(self.read(position=p)["recommendations"][0]["position_pct"], p)
        self.assertEqual(self.store.conn.total_changes, before)
        for invalid in (-1, 1, 10.0, True, "10"):
            with self.assertRaises(ValueError): self.read(position=invalid)

    def test_expiry_quote_move_missing_quote_and_closed_state(self):
        self.publish()
        self.assertEqual(self.read(now=NOW + timedelta(hours=4))["reason"], "expired")
        self.store.set_state("web_snapshot", {"execution_quote": quote(853.4)})
        self.assertEqual(self.read()["reason"], "price_changed")
        self.store.set_state("web_snapshot", {"execution_quote": quote(853.399)})
        self.assertEqual(self.read()["status"], "current")
        self.store.set_state("web_snapshot", {})
        self.assertEqual(self.read()["reason"], "quote_unavailable")
        with patch("monitor.trading_hours.trading_session", return_value={"is_open": False}):
            self.assertEqual(self.read()["reason"], "market_closed")

    def test_input_quote_source_and_freshness_and_invalidated_round(self):
        d = content(); d["recommendations"][0]["conditions"] = [
            {"action": "buy", "condition": "at_or_below", "price_cny": 830, "target_position_pct": 20}]
        for q in (None, {**quote(), "source": "private_bank"}, quote(at=NOW - timedelta(minutes=3))):
            self.context["jd_accumulation_quote"] = q
            self.assertEqual(self.publish(d, view_id=100 + len(str(q))), "unavailable")
        self.assertEqual(self.publish(content(), view_id=3), "ready")
        self.publish(content(), view_id=4, invalidated=True)
        self.assertIsNone(self.read()["analysis"])

    def test_model_disable_reenable_hides_old_generation_and_history_is_scoped(self):
        self.publish()
        self.assertIsNotNone(previous_analysis(self.store.conn, "codex", 0))
        self.assertIsNone(previous_analysis(self.store.conn, "claude", 0))
        set_enabled(self.store.conn, "codex", False)
        self.assertIsNone(self.read()["analysis"])
        set_enabled(self.store.conn, "codex", True)
        self.assertIsNone(self.read()["analysis"])
        self.assertIsNone(previous_analysis(self.store.conn, "codex", 2))

    def test_corrupt_public_row_fails_closed(self):
        self.publish()
        value = content(); value["private_accounts"] = {"cash": 42}
        self.store.conn.execute("UPDATE public_ai_reports SET content_json=?", (json.dumps(value),))
        self.assertIsNone(self.read()["analysis"])

    def test_empty_entry_and_all_empty_orders_are_valid_with_shared_reasons(self):
        self.assertEqual(self.publish(), "ready")
        self.assertTrue(all(not r["conditions"] for r in self.read()["recommendations"]))
        self.assertIsNone(self.read()["scenario_metrics"])
        for key in ("no_entry_reason", "no_conditions_reason"):
            d = content(); d["decision_basis"][key] = None
            with self.assertRaises(ValueError): validate_content(d, self.context, require_basis=True)

    def test_new_publications_require_basis_but_legacy_reports_remain_readable(self):
        d = content(); d.pop("decision_basis")
        self.assertEqual(self.publish(d), "unavailable")
        self.publish(view_id=2)
        self.store.conn.execute("UPDATE public_ai_reports SET content_json=? WHERE view_id=2", (json.dumps(d),))
        result = self.read()
        self.assertEqual(result["schema_version"], 1)
        self.assertIsNone(result["decision_basis"])
        self.assertIsNotNone(result["analysis"])

    def test_program_computes_fee_adjusted_scenarios_and_position_weight(self):
        d = content()
        d["decision_basis"]["entry_scenario"] = {"entry_price_cny": 850, "target_price_cny": 880,
            "exit_price_cny": 820, "basis": "公共日线情景；假设值，不是买入指令。"}
        d["decision_basis"]["no_entry_reason"] = None
        self.publish(d)
        result = self.read(position=40)
        metrics = result["scenario_metrics"]
        self.assertAlmostEqual(metrics["net_upside_pct"], (880 * .996 / 850 - 1) * 100, places=3)
        self.assertAlmostEqual(metrics["net_downside_pct"], (820 * .996 / 850 - 1) * 100, places=3)
        self.assertAlmostEqual(metrics["at_target_position"]["net_downside_pct"], (820 * .996 / 850 - 1) * 40, places=3)
        self.assertEqual(result["recommendations"][0]["action"], "hold")
        d["decision_basis"]["entry_scenario"]["target_price_cny"] = 851
        m = scenario_metrics(d, {"sell_fee_rate": .004})
        self.assertLess(m["net_upside_pct"], 0)
        self.assertIsNone(m["reward_risk_ratio"])
        for target, stop in ((840, 820), (880, 860)):
            d["decision_basis"]["entry_scenario"].update(target_price_cny=target, exit_price_cny=stop)
            with self.assertRaises(ValueError): validate_content(d, self.context)

    def test_scenario_cannot_borrow_private_price_or_quote(self):
        d = content()
        d["decision_basis"]["entry_scenario"] = {"entry_price_cny": 850, "target_price_cny": 881.23,
            "exit_price_cny": 820, "basis": "假设情景"}
        d["decision_basis"]["position_rationale"] = "已有40%可保留反弹参与，更高仓位增加风险。当前情景目标+2.19%、退出-3.48%，补偿不足。卖费参考0.4%。"
        d["recommendations"][0]["reason"] = "候选情景收益风险比0.63，暂不入场。"
        self.assertEqual(self.publish(d), "ready")
        result = self.read()
        self.assertIsNone(result["decision_basis"]["entry_scenario"])
        self.assertIn("无法核验", result["decision_basis"]["no_entry_reason"])
        self.assertNotIn("881.23", json.dumps(result))
        for n in ("2.19", "3.48", "0.63"):
            self.assertNotIn(n, json.dumps(result))
        self.assertIn("已有40%", result["decision_basis"]["position_rationale"])
        self.assertIn("0.4%", result["decision_basis"]["position_rationale"])
        # Older public rows with the omission marker also lose unsupported math on read.
        stored = json.loads(self.store.conn.execute('SELECT content_json FROM public_ai_reports WHERE view_id=1').fetchone()[0])
        stored["decision_basis"]["position_rationale"] += "情景收益+2.19%，退出-3.48%。"
        self.store.conn.execute('UPDATE public_ai_reports SET content_json=? WHERE view_id=1',(json.dumps(stored),))
        self.assertNotIn("3.48", json.dumps(self.read()))
        d["recommendations"][0]["invalidation"] = "报价881.23时重估"
        self.assertEqual(self.publish(d, view_id=3), "unavailable")
        d["recommendations"][0]["invalidation"] = "按共用条件重估"
        d["decision_basis"]["entry_scenario"]["target_price_cny"] = 880
        self.context["jd_accumulation_quote"] = None
        self.assertEqual(self.publish(d, view_id=2), "unavailable")

    def test_price_time_and_named_event_triggers_are_structured_and_mark_review_due(self):
        d = content()
        d["decision_basis"]["review_triggers"] = [{"kind": "price", "condition": "at_or_below", "price_cny": 849,
                                                   "event": None, "at": None}]
        self.publish(d)
        self.assertEqual(self.read()["status"], "current")
        self.store.set_state("web_snapshot", {"execution_quote": quote(849)})
        self.assertEqual(self.read()["reason"], "review_due")
        self.publish(content(), view_id=2)
        self.store.set_state("web_snapshot", {"execution_quote": quote(at=NOW + timedelta(hours=2))})
        self.assertEqual(self.read(now=NOW + timedelta(hours=2))["reason"], "review_due")
        d["decision_basis"]["review_triggers"] = [{"kind": "event", "event": "美国职位空缺数据公布",
            "at": (NOW + timedelta(hours=3)).isoformat(), "condition": None, "price_cny": None}]
        validate_content(d, self.context)
        d["decision_basis"]["review_triggers"] = [{"kind": "time", "event": "即使未触价也复评", "at": (NOW + timedelta(hours=3)).isoformat(),
                                                   "condition": None, "price_cny": None}]
        validate_content(d, self.context)
        for bad in ({"kind": "price", "condition": None, "price_cny": 849},
                    {"kind": "event", "event": None}, {"kind": "time", "at": None},
                    {"kind": "time", "at": "2026-11-03T10:00:00"}):
            d["decision_basis"]["review_triggers"] = [{"event": None, "at": None, "price_cny": None, "condition": None, **bad}]
            with self.assertRaises(ValueError): validate_content(d, self.context)

    def test_http_anonymous_allowlist_auth_etag_and_invalid_queries(self):
        self.publish()
        settings = self.settings
        class Local(Handler):
            password = "test-private-password"
            def log_message(self, *args): pass
        Local.settings = settings
        server = ThreadingHTTPServer(("127.0.0.1", 0), Local)
        t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(path, **headers):
            c = http.client.HTTPConnection("127.0.0.1", server.server_port)
            c.request("GET", path, headers=headers); r = c.getresponse(); body = r.read()
            result = r.status, dict(r.getheaders()), body; c.close(); return result
        status, headers, body = request("/api/public/ai?position_pct=20")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "public, no-cache")
        self.assertEqual(request("/api/public/ai?position_pct=20", **{"If-None-Match": headers["ETag"]})[0], 304)
        for path in ("/api/dashboard", "/api/history", "/api/pages/ai", "/api/public/ai/private", "/"):
            self.assertEqual(request(path)[0], 401, path)
        for query in ("position_pct=15", "position_pct=", "position_pct=20&position_pct=30", "view=private", "analyst=bad"):
            self.assertEqual(request("/api/public/ai?" + query)[0], 400)


class MergedRoundTests(unittest.TestCase):
    def test_existing_codex_round_persists_both_outputs_with_one_model_call(self):
        from test_four_account import FourAccountTests
        f = FourAccountTests(); f.setUp()
        try:
            ctx = f.context(); value = f.value().model_dump(); value["public_content"] = content()
            f.a._run_codex = Mock(return_value=("private research", [], FourAccountResult.model_validate(value)))
            result = f.a.analyze(ctx, "test", run_id="public-merged-test")
            self.assertIsNotNone(result)
            self.assertEqual(f.a._run_codex.call_count, 1)
            self.assertEqual(len(result["account_decisions"]), 4)
            row = f.m.store.conn.execute("SELECT status,content_json FROM public_ai_reports").fetchone()
            self.assertEqual(row[0], "ready")
            self.assertEqual(len(json.loads(row[1])["recommendations"]), 11)
            self.assertEqual(f.m.store.conn.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0], 1)
            self.assertEqual(f.a.research_system().count("公众输出public_content"), 1)
            schema = strict_schema(f.a.result_type(ctx).model_json_schema())
            self.assertIn("public_content", schema["required"])
        finally: f.tearDown()

    def test_api_submits_public_and_private_in_same_response_without_second_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings(), db_path=Path(tmp) / "api.db", anthropic_api_key="", ai_max_searches=0)
            store = Store(settings.db_path)
            try:
                a = AIAnalyst(settings, store, "codex", "codex")
                from test_four_account import FourAccountTests
                f = FourAccountTests(); f.setUp()
                try:
                    a.settings = replace(settings, ai_trading_mode="four_account", ai_history_max_queries=0)
                    value = f.value().model_dump(); value["public_content"] = content()
                    a._create = Mock(return_value=SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
                        type="tool_use", name="submit_portfolio_decisions", input=value)]))
                    a._add_usage = Mock(); a._check_active = Mock()
                    _, _, parsed = a._run_api_autonomous(f.context(), {})
                    self.assertEqual(a._create.call_count, 1)
                    self.assertEqual(len(parsed.public_content.recommendations), 11)
                    self.assertEqual(len(parsed.account_decisions.model_dump()), 4)
                finally: f.tearDown()
            finally: store.conn.close()


class PublicBrowserTests(unittest.TestCase):
    def test_public_preview_lazy_loads_and_position_changes_do_not_make_requests(self):
        from playwright.sync_api import sync_playwright
        from test_dashboard_browser import local_dashboard
        with local_dashboard() as (f, url), sync_playwright() as p:
            now = datetime.now(timezone.utc).isoformat()
            preview = content()
            preview["decision_basis"]["entry_scenario"] = {"entry_price_cny": 850, "target_price_cny": 880,
                "exit_price_cny": 820, "basis": "公共行情假设情景，用于测试扣费比较。"}
            preview["decision_basis"]["no_entry_reason"] = None
            save(f.m.store.conn, "codex", 0, 999, now, now, preview, {"jd_accumulation_quote": quote(at=datetime.fromisoformat(now))})
            f.m.store.conn.commit()
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                requests, errors = [], []
                page.on("request", lambda r: requests.append(r.url))
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(url + "/#ai/research")
                summary = page.get_by_text("公众内容预览 · 市场分析与11档仓位推荐", exact=True).first
                summary.wait_for()
                self.assertEqual(sum("/api/public/ai" in u for u in requests), 0)
                self.assertFalse(any("PublicAdvicePreview-" in u for u in requests))
                summary.click()
                selector = page.get_by_role("combobox", name="公众推荐当前仓位")
                selector.wait_for()
                self.assertEqual(selector.locator("option").count(), 11)
                self.assertEqual(sum("/api/public/ai" in u for u in requests), 1)
                selector.select_option("100")
                self.assertIn("100% → 100%", page.locator("#panel-ai").inner_text())
                selector.select_option("20")
                self.assertEqual(sum("/api/public/ai" in u for u in requests), 1)
                self.assertEqual(f.a._run_codex.call_count, 1)
                self.assertIn("高风险", page.locator("#panel-ai").inner_text())
                self.assertIn("仓位取舍", page.locator("#panel-ai").inner_text())
                self.assertIn("扣费目标收益", page.locator("#panel-ai").inner_text())
                self.assertIn("未推荐条件方案", page.locator("#panel-ai").inner_text())
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.evaluate("Date.now = () => new Date().getTime() + 86400000")
                page.get_by_text("已到期，等待下一轮分析", exact=True).wait_for()
                self.assertEqual(errors, [])
            finally: browser.close()
