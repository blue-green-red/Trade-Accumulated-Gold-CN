"""Offline regression tests: no live market, AI subscription, or notification calls."""
import base64
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import replace, asdict
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from unittest.mock import patch, Mock

from monitor.config import Settings, BJ, load_env_file
from monitor.store import Store, utc_now, iso
from monitor.controls import control, set_enabled, view_active
from monitor.accounts import Accounts
from monitor.ai import AIAnalyst, MarketView, consensus_view, long_edge
from monitor.paper import PaperBook
from monitor.position_strategy import PositionState, Order, apply_fill
from monitor.strategy import build_plan
from monitor.history import Bar
from monitor.web import dashboard, Handler, ThreadingHTTPServer
from monitor.__main__ import Monitor
from monitor.market import Market
from monitor.execution import execution_quote
from monitor.web import hourly_prices


def view(up=.7, down=.1, **extra):
    return dict(bias_short="bullish", bias_long="bullish", confidence="medium",
                p_up_3d=up, p_down_3d=down, p_up_3m=up, p_down_3m=down,
                hard_risk_event=False, hard_risk_note="", drivers=[], upcoming_events=[],
                summary="test", _ts=utc_now().isoformat(), _control_generation=0, **extra)


def snap(price=1000):
    return {"ts": utc_now(), "market_open": True, "xauusd": price * 4,
            "bank_est": {"zheshang": price}, "ratings": {}, "signals": {},
            "execution_quote": {"price": price, "source": "jd_latest_price_api", "quote_at": utc_now().isoformat(),
                                "fetched_at": utc_now().isoformat(), "product_sku": "test-jd", "url": "https://api.jdjygold.com/test"}}


def plan(v=None):
    return {"ok": True, "action": "buy_on_dip", "ratio_bank": "zheshang", "gate": "block",
            "ignore_gate": True, "gate_text": "test", "summary": "test", "decision": v or view(),
            "buy": {"valid": True, "zone_cny": [995, 1005], "stop_cny": 990,
                    "t1_cny": 1040, "t2_cny": 1080, "breakeven_cny": 1004.02, "rr1": 2.56}}


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), ai_trading_mode="probability", db_path=Path(self.tmp.name) / "test.db",
                                ai_analysts=("claude", "codex"), account_budget=100000,
                                paper_grams=10, sell_fee_rate=.004, spread_cny=0,
                                ai_backend="api", anthropic_api_key="",
                                codex_cli="missing-codex", claude_cli="missing-claude")
        self.store = Store(self.settings.db_path)

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def analyst(self, name="claude"):
        return AIAnalyst(self.settings, self.store, "codex", analyst=name)

    def test_accounts_and_dashboard_are_isolated(self):
        monitor = Monitor(self.settings, True)
        try:
            d = dashboard(self.settings)
            self.assertEqual(len(d["leaderboard"]), 4)
            self.assertEqual(len(d["short"]), 3)
            self.assertEqual([m["budget"] for m in d["comparison"]["models"]], [200000, 200000])
            self.assertEqual([m["return"] for m in d["comparison"]["models"]], [0, 0])
            set_enabled(self.store.conn, "claude", False)
            self.assertTrue(control(self.store.conn, "codex")["enabled"])
            self.assertFalse(dashboard(self.settings)["model_controls"][0]["enabled"])
        finally:
            monitor.store.conn.close()

    def test_switch_survives_restart_and_old_views_are_invalidated(self):
        old = view()
        set_enabled(self.store.conn, "claude", False)
        set_enabled(self.store.conn, "claude", True)
        self.assertFalse(view_active(self.store.conn, "claude", old))
        other = Store(self.settings.db_path)
        try:
            self.assertEqual(control(other.conn, "claude")["generation"], 2)
            self.assertTrue(view_active(other.conn, "codex", old))
        finally:
            other.conn.close()

    def test_disabled_model_does_not_schedule_or_analyze(self):
        a = self.analyst()
        set_enabled(self.store.conn, "claude", False)
        a.request_refresh("event", 0)
        a._run_codex = Mock(side_effect=AssertionError("must not call AI"))
        self.assertIsNone(a.due())
        self.assertIsNone(a.analyze({}, "manual"))
        a._run_codex.assert_not_called()

    def test_reenable_requests_fresh_view_and_failure_backoff(self):
        a = self.analyst()
        set_enabled(self.store.conn, "claude", False)
        set_enabled(self.store.conn, "claude", True)
        self.assertEqual(a.due(), "模型重新开启")
        a._failed_at = utc_now()
        self.assertIsNone(a.due())

    def test_cancelled_analysis_cannot_publish_after_fast_reenable(self):
        a = self.analyst()
        def result(*args):
            set_enabled(self.store.conn, "claude", False)
            set_enabled(self.store.conn, "claude", True)
            return "notes", [], MarketView.model_validate(view())
        a._run_codex = result
        self.assertIsNone(a.analyze({"xauusd": 4000}, "test"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM ai_views").fetchone()[0], 0)

    def test_latest_views_keep_analyst_and_trace(self):
        a = self.analyst()
        a._run_codex = lambda *args: ("notes", [], MarketView.model_validate(view()))
        a.analyze({"xauusd": 4000}, "test", run_id="same-context")
        result = a.latest(1)
        self.assertEqual(result["_analyst"], "claude")
        self.assertEqual(result["_run_id"], "same-context")
        self.assertIsInstance(result["_view_id"], int)
        set_enabled(self.store.conn, "claude", False)
        self.assertIsNone(a.latest(1))

    def test_cli_process_is_cancelled_on_switch(self):
        a = self.analyst()
        started, errors = threading.Event(), []
        import subprocess
        original = subprocess.Popen
        def start(*args, **kwargs):
            proc = original(*args, **kwargs)
            started.set()
            return proc
        def worker():
            try:
                a._run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                               text=True, timeout=35, stdin=subprocess.DEVNULL)
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch("monitor.ai.subprocess.Popen", side_effect=start):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(3))
            set_enabled(self.store.conn, "claude", False)
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_short_models_have_separate_positions_and_paused_risk(self):
        a = PaperBook(self.settings, self.store, "claude:short_ai", "Claude")
        b = PaperBook(self.settings, self.store, "codex:short_ai", "Codex")
        a.on_tick(snap(), plan())
        b.on_tick(snap(), {"ok": False})
        self.assertIsNotNone(self.store.open_trade("zheshang", a.strategy))
        self.assertIsNone(self.store.open_trade("zheshang", b.strategy))
        set_enabled(self.store.conn, "claude", False)
        a.on_tick(snap(1040), plan())
        self.assertEqual(self.store.open_trade("zheshang", a.strategy)["t1_hit"], 1)
        a.on_tick(snap(1080), plan())
        self.assertIsNone(self.store.open_trade("zheshang", a.strategy))
        self.assertEqual(self.store.trade_stats("zheshang", a.strategy)["trades"], 1)
        a.on_tick(snap(), plan())
        self.assertIsNone(self.store.open_trade("zheshang", a.strategy))
        b.on_tick(snap(), plan())
        self.assertIsNotNone(self.store.open_trade("zheshang", b.strategy))

    def test_actual_fill_rr_and_cash_limit(self):
        a = PaperBook(self.settings, self.store, "claude:short_ai", "Claude")
        p = plan()
        p["buy"]["t1_cny"] = 1010  # Advertised plan RR is high, actual net reward is inadequate.
        self.assertEqual(a.on_tick(snap(), p), [])
        self.assertIsNone(self.store.open_trade("zheshang", a.strategy))
        self.store.conn.execute("UPDATE paper_accounts SET budget=500 WHERE strategy=?", (a.strategy,))
        self.store.conn.commit()
        a.on_tick(snap(), plan())
        self.assertAlmostEqual(self.store.open_trade("zheshang", a.strategy)["grams"], .5)
        self.assertAlmostEqual(self.store.paper_balance("zheshang", a.strategy, 1000, .004, 0)["cash"], 0)

    def test_short_fills_use_direct_jd_price_and_trace_every_partial_exit(self):
        book = PaperBook(self.settings, self.store, "claude:short_ai", "Claude")
        s = snap(1000)
        s["bank_est"]["zheshang"] = 1200
        book.on_tick(s, plan())
        self.assertEqual(self.store.open_trade("zheshang", book.strategy)["entry_cny"], 1000)
        book.on_tick(snap(1040), plan())
        book.on_tick(snap(1080), plan())
        fills = self.store.conn.execute("SELECT * FROM paper_fills ORDER BY id").fetchall()
        self.assertEqual([r["price"] for r in fills], [1000, 1040, 1080])
        self.assertEqual([r["grams"] for r in fills], [10, 5, 5])
        self.assertTrue(all(json.loads(r["quote_json"])["source"] == "jd_latest_price_api" for r in fills))

    def test_missing_stale_or_invalid_jd_quote_never_falls_back_to_estimate(self):
        book = PaperBook(self.settings, self.store, "claude:short_ai", "Claude")
        for kind in ("missing", "stale", "nan", "wrong_source"):
            s = snap()
            if kind == "missing": s.pop("execution_quote")
            elif kind == "stale": s["execution_quote"]["quote_at"] = (utc_now() - timedelta(seconds=121)).isoformat()
            elif kind == "nan": s["execution_quote"]["price"] = float("nan")
            else: s["execution_quote"]["source"] = "estimated"
            self.assertIsNone(execution_quote(s))
            self.assertEqual(book.on_tick(s, plan()), [])
        self.assertIsNone(self.store.open_trade("zheshang", book.strategy))
        book.on_tick(snap(), plan())
        bad = snap(980)
        bad.pop("execution_quote")
        book.on_tick(bad, plan())
        self.assertIsNotNone(self.store.open_trade("zheshang", book.strategy))

    def test_jd_market_quote_is_validated_and_keeps_last_mark_on_failure(self):
        market = Market(self.settings, self.store)
        quote = dict(snap()["execution_quote"], price=901)
        with patch("monitor.market.fetch_tv_snapshot", return_value={"status": "error", "error": "offline"}), patch("monitor.market.fetch_spot_gold", return_value={"status": "ok", "price": 4000}), patch("monitor.market.fetch_jd_latest_price", return_value=quote):
            good = market.snapshot()
        self.assertEqual(good["execution_quote"]["price"], 901)
        with patch("monitor.market.fetch_tv_snapshot", return_value={"status": "error", "error": "offline"}), patch("monitor.market.fetch_spot_gold", return_value={"status": "ok", "price": 4000}), patch("monitor.market.fetch_jd_latest_price", side_effect=RuntimeError("offline")):
            bad = market.snapshot()
        self.assertIsNone(bad["execution_quote"])
        self.assertEqual(bad["jd_last_quote"]["price"], 901)

    def test_daily_fills_use_jd_price(self):
        accounts = Accounts(self.settings, self.store, ["hold"])
        now = datetime(2026, 10, 16, 10, tzinfo=BJ)
        bars = [Bar(date(2026, 10, 15) - timedelta(days=209-i), 4000, 4000, 7, 900, 900) for i in range(210)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        s = snap(900)
        s["bank_est"]["zheshang"] = 1100
        with patch("monitor.accounts.datetime", Clock), patch.object(accounts.history, "refresh_if_stale"), patch.object(accounts.history, "bars", return_value=bars):
            accounts.run_daily(s)
        row = self.store.conn.execute("SELECT * FROM account_fills").fetchone()
        self.assertEqual(row["price"], 900)
        self.assertAlmostEqual(row["grams"], 100000 / 900)
        self.assertEqual(json.loads(row["decision_json"])["execution_quote"]["price"], 900)

    def test_daily_quote_expiring_during_history_fetch_does_not_fill(self):
        accounts = Accounts(self.settings, self.store, ["hold"])
        now = datetime(2026, 10, 16, 10, tzinfo=BJ)
        bars = [Bar(date(2026, 10, 15) - timedelta(days=209-i), 4000, 4000, 7, 900, 900) for i in range(210)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        s = snap(900)
        def expire(): s["execution_quote"]["quote_at"] = (utc_now() - timedelta(seconds=121)).isoformat()
        with patch("monitor.accounts.datetime", Clock), patch.object(accounts.history, "refresh_if_stale", side_effect=expire), patch.object(accounts.history, "bars", return_value=bars):
            accounts.run_daily(s)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM account_fills").fetchone()[0], 0)

    def test_curve_preserves_old_marks_and_uses_jd_after_cutover(self):
        self.store.set_state("jd_execution_since", "2026-10-16T00:00:00+00:00")
        for ts, jd, estimate in (("2026-10-15T23:00:00+00:00", 800, 850), ("2026-10-16T01:00:00+00:00", 900, 950)):
            s = snap()
            s.update(ts=datetime.fromisoformat(ts), jd_price=jd, bank_est={"zheshang": estimate})
            self.store.add_tick(s)
        prices = hourly_prices(self.store.conn, "zheshang", "2026-10-15T00:00:00+00:00")
        self.assertEqual([r["p"] for r in prices], [850, 900])

    def test_ai_exit_records_its_own_new_decision(self):
        a = PaperBook(self.settings, self.store, "claude:short_ai", "Claude")
        a.on_tick(snap(), plan())
        exit_plan = dict(plan(view(.1, .8, _view_id=42, _analyst="claude")), force_exit=True)
        a.on_tick(snap(1020), exit_plan)
        row = self.store.last_closed_trade("zheshang", a.strategy)
        self.assertEqual(row["exit_cny"], 1020)
        self.assertEqual(json.loads(row["close_decision_json"])["view"]["_view_id"], 42)

    def test_daily_models_use_their_own_views(self):
        accounts = Accounts(self.settings, self.store, ["position_ai"])
        now = datetime(2026, 10, 15, 10, tzinfo=BJ)
        bars = [Bar(date(2026, 10, 14) - timedelta(days=209 - i), 4000, 4000, 7, 900, 900) for i in range(210)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        with patch("monitor.accounts.datetime", Clock), patch.object(accounts.history, "refresh_if_stale"), patch.object(accounts.history, "bars", return_value=bars):
            accounts.run_daily(snap(900), {"claude": view(.8, .1), "codex": view(.1, .7)})
        self.assertGreater(accounts._load("claude:position_ai").grams, 0)
        self.assertEqual(accounts._load("codex:position_ai").grams, 0)
        row = self.store.conn.execute("SELECT decision_json FROM account_fills LIMIT 1").fetchone()
        self.assertEqual(json.loads(row[0])["analyst"], "claude")

    def test_retired_dca_keys_cannot_recreate_accounts(self):
        settings = replace(self.settings, strategies=("hold", "dca", "dca_smart", "position", "dca_smart_ai", "position_ai", "short", "short_ai"))
        monitor = Monitor(settings, True)
        try:
            self.assertEqual({r[0] for r in self.store.conn.execute("SELECT strategy FROM accounts")},
                             {"hold", "position", "claude:position_ai", "codex:position_ai"})
            d = dashboard(settings)
            self.assertEqual(len(d["leaderboard"]) + len(d["short"]), 7)
            self.assertEqual([len(m["accounts"]) for m in d["comparison"]["models"]], [2, 2])
        finally:
            monitor.store.conn.close()

    def test_paused_existing_daily_position_still_stops_loss(self):
        accounts = Accounts(self.settings, self.store, ["position_ai"])
        state = PositionState(100000, 90000, grams=10, cost=10000, start_index=200)
        accounts._save("claude:position_ai", state)
        set_enabled(self.store.conn, "claude", False)
        now = datetime(2026, 10, 15, 10, tzinfo=BJ)
        bars = [Bar(date(2026, 10, 14) - timedelta(days=209 - i), 3400, 3400, 7, 850, 850) for i in range(210)]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        with patch("monitor.accounts.datetime", Clock), patch.object(accounts.history, "refresh_if_stale"), patch.object(accounts.history, "bars", return_value=bars):
            accounts.run_daily(snap(850), {"claude": view(), "codex": None})
        self.assertEqual(accounts._load("claude:position_ai").grams, 0)
        self.assertGreater(accounts._load("claude:position_ai").cash, 90000)

    def test_complete_daily_cycle_counts_partial_exits_once(self):
        accounts = Accounts(self.settings, self.store, ["position_ai"])
        key = "claude:position_ai"
        accounts.record_cycle(key, "buy", 10, 1000, 0, 0, 10)
        accounts.record_cycle(key, "sell", 5, 1040, 20.8, 10, 5)
        self.assertIsNone(self.store.conn.execute("SELECT pnl FROM trade_cycles").fetchone()[0])
        accounts.record_cycle(key, "sell", 5, 1080, 21.6, 5, 0)
        self.store.conn.commit()
        row = self.store.conn.execute("SELECT pnl FROM trade_cycles").fetchone()
        self.assertAlmostEqual(row[0], 557.6)
        stats = dashboard(self.settings)["comparison"]["models"][0]
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["win_rate"], 1)

    def test_legacy_accounts_are_not_duplicated_or_compared(self):
        self.store.conn.execute("CREATE TABLE accounts(strategy TEXT PRIMARY KEY,name TEXT,budget REAL,started_at TEXT,state_json TEXT,last_run_day TEXT)")
        state = PositionState(100000, 90000, grams=10, cost=10000, start_index=200)
        self.store.conn.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?)", ("position_ai", "old", 100000, iso(utc_now()), json.dumps(asdict(state)), None))
        self.store.conn.commit()
        accounts = Accounts(self.settings, self.store, ["position_ai"])
        self.assertEqual(accounts._load("position_ai").grams, 10)
        self.assertEqual(accounts._load("claude:position_ai").grams, 0)
        Accounts(self.settings, self.store, ["position_ai"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 3)
        cmp = dashboard(self.settings)["comparison"]["models"]
        self.assertTrue(all(len(m["accounts"]) == 1 for m in cmp))

    def test_consensus_probabilities_do_not_keep_first_model(self):
        bearish = dict(view(.1, .7), bias_short="bearish", bias_long="bearish")
        c = consensus_view({"claude": view(.7, .1), "codex": bearish})
        self.assertEqual(c["bias_long"], "neutral")
        self.assertAlmostEqual(long_edge(c), 0)

    def test_support_being_tested_can_enter_zone(self):
        s = dict(snap(), day_high=4100, day_low=3900, day_open=3998, ratings={"30m": .7, "1h": .7, "4h": .7, "1d": .7},
                 ratios={"zheshang": {"ratio": .25}}, tv={"ema20_1h": 3998, "sma20_1d": 3999, "pivot_m_r1": 4100})
        p = build_plan(s, axis_m={"axis": "偏多"}, direction="bull", spread_cny=0, sell_fee_rate=.004)
        self.assertLessEqual(p["buy"]["zone"][0], s["xauusd"])
        self.assertGreaterEqual(p["buy"]["zone"][1], s["xauusd"])
        self.assertTrue(p["buy"]["valid"])

    def test_expired_bank_ratio_is_not_used(self):
        market = Market(self.settings, self.store)
        market.ratios = {"zheshang": {"ratio": .25, "ts": iso(utc_now() - timedelta(hours=7))}}
        with patch("monitor.market.fetch_tv_snapshot", return_value={"status": "error", "error": "offline"}), patch("monitor.market.fetch_spot_gold", return_value={"status": "ok", "price": 4000}), patch("monitor.market.fetch_jd_latest_price", return_value={}):
            self.assertEqual(market.snapshot()["bank_est"], {})

    def test_weekly_report_runs_when_market_closed(self):
        monitor = Monitor(self.settings, True)
        try:
            with patch("monitor.__main__.trading_session", return_value={"is_open":False}), patch.object(monitor.market, "refresh_fx_if_due"), patch.object(monitor.market, "snapshot", return_value=dict(snap(), market_open=False)), patch.object(monitor, "poll_jin10_if_due"), patch.object(monitor, "weekly_report_if_due", return_value=[{"key": "weekly"}]):
                _, _, events = monitor.tick()
            self.assertEqual(events, [{"key": "weekly"}])
        finally:
            monitor.store.conn.close()

    def test_ai_runs_serially_with_equal_context_and_one_run_id(self):
        monitor = Monitor(self.settings, True)
        calls, active_calls, peak = [], 0, 0
        context = {"snapshot": [4000], "news": ["shared"]}
        def analyze(name):
            def run(ctx, trigger, store, run_id):
                nonlocal active_calls, peak
                active_calls += 1
                peak = max(peak, active_calls)
                other = "codex" if name == "claude" else "claude"
                if name == "claude":
                    self.assertTrue(store.get_state(f"ai_runtime:{other}")["queued"])
                calls.append((name, deepcopy(ctx), run_id))
                ctx["news"].append("private mutation")
                active_calls -= 1
                return view(_analyst=name)
            return run
        monitor.analysts = {name: Mock(analyze=analyze(name)) for name in ("claude", "codex")}
        try:
            with patch.object(monitor, "ai_context", return_value=context):
                monitor._ai_worker(snap(), {}, {"claude": "test", "codex": "test"})
            self.assertEqual([c[0] for c in calls], ["claude", "codex"])
            self.assertEqual(calls[0][1], calls[1][1])
            self.assertEqual(calls[0][2], calls[1][2])
            self.assertEqual(peak, 1)
            self.assertEqual(context["news"], ["shared"])
        finally:
            monitor.store.conn.close()

    def test_queued_model_disabled_before_its_turn_is_skipped(self):
        monitor = Monitor(self.settings, True)
        def first(ctx, trigger, store, run_id):
            set_enabled(store.conn, "codex", False)
            return view(_analyst="claude")
        second = Mock()
        monitor.analysts = {"claude": Mock(analyze=first), "codex": second}
        try:
            with patch.object(monitor, "ai_context", return_value={}):
                monitor._ai_worker(snap(), {}, {"claude": "test", "codex": "test"})
            second.analyze.assert_not_called()
            self.assertFalse(monitor.store.get_state("ai_runtime:codex")["queued"])
        finally:
            monitor.store.conn.close()

    def test_analysis_order_alternates(self):
        monitor = Monitor(self.settings, True)
        monitor.analysts = {name: Mock(due=Mock(return_value="test")) for name in ("claude", "codex")}
        try:
            with patch("monitor.__main__.threading.Thread") as thread:
                thread.return_value.is_alive.return_value = False
                monitor.run_ai_if_due(snap())
                first = list(thread.call_args.kwargs["args"][2])
                monitor.run_ai_if_due(snap())
                second = list(thread.call_args.kwargs["args"][2])
            self.assertEqual(first, ["claude", "codex"])
            self.assertEqual(second, ["codex", "claude"])
        finally:
            monitor.store.conn.close()

    def test_forced_bank_calibration_cannot_overlap_ai(self):
        monitor = Monitor(self.settings, True)
        try:
            with patch.object(monitor.market, "refresh_fx_if_due"), patch.object(monitor.market, "snapshot", return_value=snap()), patch.object(monitor, "ai_running", return_value=True), patch.object(monitor.market, "calibrate_banks") as banks, patch.object(monitor, "poll_jin10_if_due"), patch.object(monitor, "run_ai_if_due", return_value=[]), patch.object(monitor.accounts, "run_daily", return_value=[]), patch.object(monitor, "refresh_macro_if_due"), patch.object(monitor, "digest_if_due", return_value=[]), patch.object(monitor, "weekly_report_if_due", return_value=[]):
                monitor.tick(force_bank=True)
            banks.assert_not_called()
        finally:
            monitor.store.conn.close()

    def test_env_inline_comments_and_quoted_values(self):
        import os
        path = Path(self.tmp.name) / "example.env"
        path.write_text('GOLD_TEST_TOKEN=abc # comment\nGOLD_TEST_PASSWORD="abc # literal"\nGOLD_TEST_HASH=abc#def\n')
        with patch.dict(os.environ, {}, clear=True):
            load_env_file(path)
            self.assertEqual(os.environ["GOLD_TEST_TOKEN"], "abc")
            self.assertEqual(os.environ["GOLD_TEST_PASSWORD"], "abc # literal")
            self.assertEqual(os.environ["GOLD_TEST_HASH"], "abc#def")

    def test_control_http_auth_origin_validation_and_persistence(self):
        class TestHandler(Handler):
            settings = self.settings
            password = "test-password-123"
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        auth = "Basic " + base64.b64encode(b"user:test-password-123").decode()
        def post(body, headers):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("POST", "/api/models/claude", json.dumps(body), headers)
                result = conn.getresponse()
                code = result.status
                result.read()
                return code
            finally:
                conn.close()
        try:
            headers = {"Content-Type": "application/json", "X-Gold-Control": "1", "Authorization": auth}
            self.assertEqual(post({"enabled": False}, {}), 401)
            self.assertEqual(post({"enabled": False}, dict(headers, Origin="https://evil.example")), 403)
            self.assertEqual(post({"enabled": "false"}, headers), 400)
            self.assertEqual(post({"enabled": False}, headers), 200)
            self.assertFalse(control(self.store.conn, "claude")["enabled"])
            self.assertTrue(control(self.store.conn, "codex")["enabled"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
