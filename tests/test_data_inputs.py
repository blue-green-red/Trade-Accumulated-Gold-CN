"""Offline checks for shared data, cache boundaries and acquisition failures."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.config import Settings
from monitor.store import Store
from monitor.jd_research import snapshot, summarize
from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst


class DataTests(unittest.TestCase):
    def setUp(self):
        official=patch('monitor.official_calendar.supplement',return_value=([],[]));official.start();self.addCleanup(official.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "data.db"
        self.store = Store(self.path)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def response(self, kind, age=0):
        stamp = (self.now - timedelta(seconds=age)).timestamp()
        row = ({"publishTime": stamp * 1000, "longPositionRatio": 60,
                "shortPositionRatio": 40, "tradingVolume": 2000000} if kind == "funds" else
               {"time": stamp, "price": 900, "levels": json.dumps([
                   {"price": 895 + i, "ol": i, "os": 10 - i} for i in range(10)])})
        return {"success": True, "code": "0000", "data": [json.dumps(row)]}

    def test_cache_once_and_expiry(self):
        with patch("monitor.jd_research.fetch", side_effect=lambda endpoint, key: self.response("funds" if endpoint == "bomb" else "orders")) as fetch:
            first = snapshot(self.store, "private-test-key", self.now)
            second = snapshot(self.store, "private-test-key", self.now + timedelta(minutes=5))
            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(first["orders"]["long_clusters"]), 3)
            self.assertNotIn("private-test-key", json.dumps(first))
            snapshot(self.store, "private-test-key", self.now + timedelta(minutes=16))
            self.assertEqual(fetch.call_count, 4)

    def test_failure_cached_and_redacted(self):
        with patch("monitor.jd_research.fetch", side_effect=ValueError("secret-test-key")) as fetch:
            value = snapshot(self.store, "secret-test-key", self.now)
            snapshot(self.store, "secret-test-key", self.now)
            self.assertEqual(fetch.call_count, 2)
            self.assertNotIn("secret-test-key", json.dumps(value))
            self.assertEqual(value["orders"]["status"], "unavailable")

    def test_stale_future_and_nan_excluded(self):
        self.assertEqual(summarize("orders", self.response("orders", 3601), self.now)["status"], "stale")
        self.assertEqual(summarize("funds", self.response("funds", 86401), self.now)["status"], "stale")
        self.assertEqual(summarize("orders", self.response("orders", -600), self.now)["status"], "stale")
        with self.assertRaises(ValueError):
            summarize("orders", {"success": True, "code": "0000", "data": [{"time": self.now.timestamp(), "price": float("nan"), "levels": []}]}, self.now)

    def test_shared_context_reuses_macro_no_key(self):
        monitor = Monitor.__new__(Monitor)
        monitor.settings = replace(Settings(), db_path=self.path, clawx_api_key="", ai_max_searches=0)
        self.store.set_state("web_macro", {"fetched_at": self.now.isoformat(), "quotes": {"DXY": 100}, "calendar": []})
        with patch("macro_data.snapshot", side_effect=AssertionError("must use cache")):
            context = monitor.ai_context({"xauusd": 4000}, None, [{"text": "news"}])
        self.assertEqual(context["macro"]["quotes"]["DXY"], 100)
        self.assertEqual(context["jd_research"]["funds"]["status"], "unconfigured")
        self.assertEqual(self.store.get_state("ai_inputs")["news_count"], 1)
        self.assertNotIn("api_key", json.dumps(context))

    def test_zero_search_disables_codex_and_hides_credentials(self):
        analyst = AIAnalyst.__new__(AIAnalyst)
        analyst.settings = replace(Settings(), ai_max_searches=0)
        analyst.model = ""
        def run(cmd, **kwargs):
            self.assertNotIn("--search", cmd)
            self.assertIn('web_search="disabled"', cmd)
            self.assertNotIn("CLAWX_JR_API_KEY", kwargs["env"])
            raise RuntimeError("checked command")
        analyst._run_process = run
        with self.assertRaisesRegex(RuntimeError, "checked command"):
            analyst._run_codex({}, {})

    def test_zero_search_disables_claude_tools(self):
        analyst = AIAnalyst.__new__(AIAnalyst)
        analyst.settings = replace(Settings(), ai_max_searches=0)
        analyst.model = "test"
        def run(cmd, **kwargs):
            self.assertEqual(cmd[cmd.index("--tools") + 1], "")
            denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--permission-mode")]
            self.assertIn("WebSearch", denied)
            self.assertIn("WebFetch", denied)
            self.assertNotIn("CLAWX_JR_API_KEY", kwargs["env"])
            raise RuntimeError("checked command")
        analyst._run_process = run
        with self.assertRaisesRegex(RuntimeError, "checked command"):
            analyst._run_claude_code({}, {})

    def test_zero_search_api_omits_search_tool(self):
        from types import SimpleNamespace
        analyst = AIAnalyst.__new__(AIAnalyst)
        analyst.settings = replace(Settings(), ai_max_searches=0)
        analyst._check_active = Mock()
        analyst._add_usage = Mock()
        analyst._create = Mock(return_value=SimpleNamespace(stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="共同数据分析")]))
        notes, sources = analyst._research({}, {})
        self.assertEqual(notes, "共同数据分析")
        self.assertNotIn("tools", analyst._create.call_args.kwargs)

    def test_enabled_search_cli_flags_and_policy(self):
        analyst = AIAnalyst.__new__(AIAnalyst)
        analyst.analyst = 'codex'
        analyst.settings = replace(Settings(), ai_max_searches=2, db_path=self.path)
        analyst.model = "test"
        self.assertIn("最多搜索 2 次", analyst.research_system())
        self.assertIn("不必用满次数", analyst.research_system())
        def run_codex(cmd, **kwargs):
            self.assertIn("--search", cmd)
            self.assertNotIn('web_search="disabled"', cmd)
            raise RuntimeError("checked command")
        analyst._run_process = run_codex
        with self.assertRaisesRegex(RuntimeError, "checked command"):
            analyst._run_codex({}, {})
        def run_claude(cmd, **kwargs):
            available = cmd[cmd.index("--tools") + 1:cmd.index("--allowedTools")]
            self.assertIn("WebSearch", available)
            self.assertIn("WebFetch", available)
            raise RuntimeError("checked command")
        analyst._run_process = run_claude
        with self.assertRaisesRegex(RuntimeError, "checked command"):
            analyst._run_claude_code({}, {})

    def test_macro_feeds_refresh_independently(self):
        from monitor.macro_cache import refresh
        with patch("macro_data.fetch_macro_quotes", return_value={"DXY": 100}) as quotes, patch("macro_data.fetch_calendar", return_value=[]) as calendar:
            refresh(self.store, self.now)
            refresh(self.store, self.now + timedelta(seconds=60))
            self.assertEqual((quotes.call_count, calendar.call_count), (1, 1))
            refresh(self.store, self.now + timedelta(seconds=300))
            self.assertEqual((quotes.call_count, calendar.call_count), (2, 1))
            refresh(self.store, self.now + timedelta(seconds=1800))
            self.assertEqual((quotes.call_count, calendar.call_count), (3, 2))

    def test_calendar_accelerates_before_and_after_release(self):
        from monitor.macro_cache import refresh, calendar_interval
        event = {"time_utc": (self.now + timedelta(minutes=10)).isoformat(), "country": "US", "tier": 1}
        self.assertEqual(calendar_interval([event], self.now), 60)
        self.assertEqual(calendar_interval([event], self.now - timedelta(minutes=6)), 1800)
        self.assertEqual(calendar_interval([event], self.now + timedelta(minutes=41)), 1800)
        with patch("macro_data.fetch_macro_quotes", return_value={}) as quotes, patch("macro_data.fetch_calendar", return_value=[event]) as calendar:
            refresh(self.store, self.now)
            refresh(self.store, self.now + timedelta(seconds=60))
            self.assertEqual((quotes.call_count, calendar.call_count), (1, 2))
            event["released"] = True
            refresh(self.store, self.now + timedelta(minutes=11))
            self.assertEqual(calendar.call_count, 3)

    def test_macro_failure_preserves_success_time_and_retries_on_schedule(self):
        from monitor.macro_cache import refresh
        old = (self.now - timedelta(hours=2)).isoformat()
        self.store.set_state("web_macro", {"fetched_at": old, "quotes": {"DXY": 99}, "calendar": []})
        with patch("macro_data.fetch_macro_quotes", side_effect=TimeoutError) as quotes, patch("macro_data.fetch_calendar", return_value=[]):
            result = refresh(self.store, self.now)
            self.assertEqual(result["quotes"], {"DXY": 99})
            self.assertEqual(result["quotes_fetched_at"], old)
            refresh(self.store, self.now + timedelta(seconds=60))
            self.assertEqual(quotes.call_count, 1)
            monitor = Monitor.__new__(Monitor)
            monitor.settings = replace(Settings(), db_path=self.path, clawx_api_key="")
            with patch("monitor.__main__.utc_now", return_value=self.now):
                context = monitor.ai_context({"xauusd": 4000}, None)
            self.assertIsNone(context["macro"]["quotes"])
            self.assertEqual(context["macro"]["status"]["calendar"], "available")

    def test_jd_dashboard_is_read_only_and_excludes_stale_values(self):
        from monitor.web import jd_reference
        raw = self.response("orders", age=7200)
        self.store.set_state("jd_research:orders", {"fetched_at": self.now.isoformat(), "raw": raw})
        with patch("monitor.jd_research.fetch", side_effect=AssertionError("page must not fetch")):
            reference = jd_reference(self.store.conn)
        self.assertEqual(reference["orders"]["status"], "stale")
        self.assertNotIn("reference_price", reference["orders"])
        self.assertNotIn("raw", reference["orders"])
        self.assertEqual(reference["funds"]["status"], "not_fetched")

    def test_news_rule_triggers_both_without_ai_triage(self):
        monitor = Monitor.__new__(Monitor)
        monitor.store = self.store
        monitor.settings = replace(Settings(), jin10_ai_daily_cap=4)
        monitor._jin10_pending = [{"id": "n1", "text": "important event"}]
        models = {name: Mock() for name in ("claude", "codex")}
        for model in models.values():
            model.latest.return_value = None
        monitor.enabled_analysts = lambda: models
        monitor.start_jin10_triage({})
        for model in models.values():
            model.request_refresh.assert_called_once()
            model.triage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
