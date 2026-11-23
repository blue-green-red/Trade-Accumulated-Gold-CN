"""事件不丢失、实际值确认及预测起算价的离线回归测试。"""
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.ai import AIAnalyst, MarketView
from monitor.ai_events import acknowledge, enqueue, read
from monitor.config import Settings
from monitor.controls import set_enabled
from monitor.__main__ import Monitor
from monitor.scorecard import evaluate, bar_close_utc
from monitor.store import Store, utc_now


def result():
    return MarketView(bias_short="bullish", bias_long="bullish", confidence="medium",
        p_up_3d=.7, p_down_3d=.1, p_up_3m=.7, p_down_3m=.1,
        hard_risk_event=False, hard_risk_note="", drivers=[], upcoming_events=[], summary="test")


class EventTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), ai_trading_mode="probability", db_path=Path(self.tmp.name)/"test.db", ai_schedule_bj=(),
            ai_analysts=("claude", "codex"), ai_backend="api", anthropic_api_key="",
            claude_cli="missing-claude", codex_cli="missing-codex", clawx_api_key="")
        self.store = Store(self.settings.db_path)
        self.a = AIAnalyst(self.settings, self.store, "codex", analyst="codex")
        self.a._run_codex = Mock(return_value=("notes", [], result()))
        self.now = utc_now()

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def context(self, ids=()):
        return {"xauusd": 4000, "snapshot_at": self.now.isoformat(), "event_coverage": {"codex": list(ids)}}

    def test_cooldown_keeps_event_and_survives_restart(self):
        self.a.analyze(self.context(), "initial")
        self.a.request_refresh("new important event", 15, event_id="one")
        self.assertIsNone(self.a.due())
        restarted = AIAnalyst(self.settings, self.store, "codex", analyst="codex")
        with patch("monitor.ai.utc_now", return_value=self.now + timedelta(minutes=16)):
            self.assertEqual(restarted.due(), "new important event")
        self.assertEqual([e["id"] for e in read(self.store.conn, "codex")["pending"]], ["one"])

    def test_market_move_episode_survives_acknowledgment_but_rearms_on_reversal_and_reset(self):
        from monitor.alerts import AlertEngine
        from monitor.cadence import request_market_move

        self.store.add_tick({'ts': self.now - timedelta(minutes=30), 'xauusd': 100, 'market_open': True})
        engine = AlertEngine(self.settings, self.store)
        analysts = {'codex': self.a}

        def observe(minute, price):
            move = engine.measure_move({'xauusd': price, 'market_open': True})
            request_market_move(self.store, analysts, move, self.now + timedelta(minutes=minute))

        observe(0, 101)
        observe(1, 102)
        first, = read(self.store.conn, 'codex')['pending']
        self.assertEqual(first['data']['change_pct'], 1)
        self.assertEqual(first['data']['observed_xauusd'], 101)
        with self.store.conn:
            acknowledge(self.store.conn, 'codex', [first['id']])
        observe(2, 103)
        self.assertEqual(read(self.store.conn, 'codex')['pending'], [])

        observe(3, 99)
        observe(4, 100)
        observe(5, 101)
        down, next_up = read(self.store.conn, 'codex')['pending']
        self.assertTrue(down['id'].startswith('market-move:down:'))
        self.assertTrue(next_up['id'].startswith('market-move:up:'))
        self.assertNotEqual(first['id'], next_up['id'])

    def test_only_successfully_covered_events_are_acknowledged(self):
        self.a.request_refresh("first", 0, event_id="one", payload={"actual": 0})
        ctx = self.context(["one"])
        ctx['trigger_events'] = read(self.store.conn, 'codex')['pending']

        def backend(*args):
            self.a.request_refresh("arrived while analyzing", 0, event_id="two")
            return "notes", [], result()

        self.a._run_codex = Mock(side_effect=backend)
        self.a.analyze(ctx, "first", run_id='covered-one')
        sent = self.a._run_codex.call_args.args[0]
        self.assertNotIn('event_coverage', sent)
        self.assertEqual(sent['trigger_events'][0]['data']['actual'], 0)
        saved = self.store.get_state('ai_model_input:covered-one:codex')
        self.assertEqual(saved['event_coverage'], {'codex': ['one']})
        state = read(self.store.conn, "codex")
        self.assertEqual([e["id"] for e in state["pending"]], ["two"])
        self.assertIn("one", state["covered"])
        self.assertFalse(self.a.request_refresh("duplicate", 0, event_id="one"))
        self.assertEqual(self.a.due(), "arrived while analyzing")

    def test_worker_submits_only_each_models_own_trigger_payloads(self):
        m = self.monitor()
        self.addCleanup(m.store.conn.close)
        m.analysts = {name: Mock() for name in ('codex', 'claude')}
        for name, analyst in m.analysts.items():
            analyst.analyze.return_value = None
            enqueue(self.store.conn, name, '同编号的账户变化', 0, 'live-account:shared', {'origin': name})
            enqueue(self.store.conn, name, '本模型的复评', 0, f'live-move:{name}', {'origin': name})

        with patch.object(m, 'ai_context', return_value={'xauusd': 4000}), \
                patch('monitor.jd_live_preflight.prepare', return_value={'can_analyze': True, 'status': 'ready'}):
            m._ai_worker({}, {}, {'codex': '事件复评', 'claude': '事件复评'})

        for name, analyst in m.analysts.items():
            sent = analyst.analyze.call_args.args[0]
            ids = ['live-account:shared', f'live-move:{name}']
            self.assertEqual(sent['event_coverage'], {name: ids})
            self.assertEqual([e['id'] for e in sent['trigger_events']], ids)
            self.assertEqual([e['data']['origin'] for e in sent['trigger_events']], [name, name])

    def test_failure_and_switch_cancellation_do_not_acknowledge(self):
        self.a.request_refresh("first", 0, event_id="one")
        self.a._run_codex = Mock(side_effect=RuntimeError("offline failure"))
        self.assertIsNone(self.a.analyze(self.context(["one"]), "test"))
        self.assertEqual(len(read(self.store.conn, "codex")["pending"]), 1)
        def cancel(*args):
            set_enabled(self.store.conn, "codex", False)
            return "notes", [], result()
        self.a._run_codex = cancel
        self.assertIsNone(self.a.analyze(self.context(["one"]), "test"))
        self.assertEqual(len(read(self.store.conn, "codex")["pending"]), 1)

    def monitor(self):
        m = Monitor(self.settings, True)
        m.analysts = {"codex": self.a}
        return m

    def event(self, title="CPI", actual=None, minutes_ago=15):
        return {"title": title, "country": "US", "tier": 1, "time_utc": (self.now - timedelta(minutes=minutes_ago)).isoformat(),
                "time_bj": "10-16 20:30", "actual": actual, "released": actual is not None, "forecast": .2, "previous": .1}

    def save_calendar(self, events, stamp=None):
        self.store.set_state("web_macro", {"calendar_fetched_at": (stamp or self.now).isoformat(), "calendar": events})

    def test_calendar_waits_for_actual_zero_is_valid_and_deduplicated(self):
        m = self.monitor()
        try:
            self.save_calendar([self.event()])
            m.request_ai_after_data_release()
            self.assertFalse(read(self.store.conn, "codex")["pending"])
            self.save_calendar([self.event(actual=0)])
            m.request_ai_after_data_release()
            m.request_ai_after_data_release()
            queue = read(self.store.conn, "codex")["pending"]
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["data"]["actual"], 0)
        finally:
            m.store.conn.close()

    def test_same_time_partial_release_late_result_is_not_lost(self):
        m = self.monitor()
        try:
            first, second = self.event("CPI", .1, 90), self.event("Core CPI", None, 90)
            self.save_calendar([first, second])
            m.request_ai_after_data_release()
            ids = [e["id"] for e in read(self.store.conn, "codex")["pending"]]
            self.a.analyze(self.context(ids), "first actual")
            second["actual"] = .2
            self.save_calendar([first, second])
            m.request_ai_after_data_release()
            pending = read(self.store.conn, "codex")["pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["data"]["title"], "Core CPI")
            self.assertTrue(pending[0]["data"]["released"])
        finally:
            m.store.conn.close()

    def test_document_schedule_triggers_one_review_without_claiming_publication(self):
        m = self.monitor()
        try:
            self.save_calendar([self.event('FOMC Minutes', minutes_ago=0)])
            m.request_ai_after_data_release()
            self.assertFalse(read(self.store.conn, 'codex')['pending'])
            self.save_calendar([self.event('FOMC Minutes', minutes_ago=15)])
            m.request_ai_after_data_release();m.request_ai_after_data_release()
            queue=read(self.store.conn, 'codex')['pending']
            self.assertEqual(len(queue),1)
            self.assertFalse(queue[0]['data']['released'])
            self.assertFalse(queue[0]['data']['publication_confirmed'])
            self.assertIn('待核实',queue[0]['reason'])
        finally:
            m.store.conn.close()

    def test_stale_calendar_and_invalid_actual_do_not_trigger(self):
        m = self.monitor()
        try:
            self.save_calendar([self.event(actual=1)], self.now - timedelta(hours=2))
            m.request_ai_after_data_release()
            self.save_calendar([self.event(actual=float("nan")), self.event("other", "")])
            m.request_ai_after_data_release()
            self.assertFalse(read(self.store.conn, "codex")["pending"])
        finally:
            m.store.conn.close()

    def test_queued_second_model_keeps_events_arriving_after_shared_input(self):
        m = self.monitor()
        b = AIAnalyst(self.settings, self.store, "codex", analyst="claude")
        m.analysts["claude"] = b
        captured = []
        def first(ctx, *args):
            captured.append(deepcopy(ctx))
            self.a.request_refresh("arrived after shared snapshot", 0, event_id="new")
            return "notes", [], result()
        b._run_codex = first
        def second(ctx, *args):
            captured.append(deepcopy(ctx))
            return "notes", [], result()
        self.a._run_codex = second
        try:
            with patch.object(m, "ai_context", return_value={"xauusd": 4000}):
                m._ai_worker({}, {}, {"claude": "test", "codex": "test"})
            self.assertEqual(captured[0], captured[1])
            self.assertEqual([e["id"] for e in read(self.store.conn, "codex")["pending"]], ["new"])
        finally:
            m.store.conn.close()

    def test_news_is_queued_during_analysis_and_cooldown(self):
        m = self.monitor()
        self.a.analyze(self.context(), "recent")
        m._jin10_pending = [{"id": "flash", "text": "new important fact", "time_bj": "10-16 20:30"}]
        try:
            with patch.object(m, "ai_running", return_value=True):
                m.run_ai_if_due({})
            pending = read(self.store.conn, "codex")["pending"]
            self.assertEqual(pending[0]["id"], "jin10:flash")
            self.assertEqual(pending[0]["data"]["text"], "new important fact")
        finally:
            m.store.conn.close()

    def test_snapshot_start_and_completion_times_recorded_and_age_uses_snapshot(self):
        old = self.now - timedelta(hours=2)
        self.a.analyze({**self.context(), "snapshot_at": old.isoformat()}, "test")
        row = self.store.conn.execute("SELECT * FROM ai_views WHERE error IS NULL").fetchone()
        self.assertEqual(row["snapshot_at"], old.isoformat())
        self.assertLessEqual(row["started_at"], row["completed_at"])
        self.assertEqual(row["completed_at"], row["ts"])
        self.assertIsNone(self.a.latest(1))
        self.assertIsNotNone(self.a.latest(3))

    def grade_setup(self, with_tick=True, legacy=False):
        completed = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
        snapshot = completed - timedelta(minutes=10)
        self.store.conn.execute("CREATE TABLE daily_bars(day TEXT PRIMARY KEY,xau_close REAL)")
        self.store.conn.execute("INSERT INTO daily_bars VALUES('2026-10-12',4200)")
        self.store.conn.execute("INSERT INTO ai_views(ts,xauusd,view_json,analyst,completed_at,snapshot_at,started_at,run_id) VALUES(?,?,?,?,?,?,?,?)",
            (completed.isoformat(), 4000, result().model_dump_json(), "codex", None if legacy else completed.isoformat(), snapshot.isoformat(), snapshot.isoformat(), "test"))
        self.store.conn.execute("INSERT INTO ticks(ts,xauusd,market_open) VALUES(?,?,1)", (snapshot.isoformat(),4000))
        if with_tick:
            self.store.conn.execute("INSERT INTO ticks(ts,xauusd,market_open) VALUES(?,?,1)",((completed + timedelta(minutes=1)).isoformat(),4200))
        close = bar_close_utc(completed.date())
        self.store.conn.execute("INSERT INTO ticks(ts,xauusd,market_open) VALUES(?,?,1)",(close.isoformat(),4200))
        self.store.conn.commit()
        return completed, close

    def test_prediction_grade_excludes_move_during_analysis(self):
        completed, close = self.grade_setup()
        score = evaluate(self.store.conn, close + timedelta(minutes=1))
        call = score["recent"][0]["calls"]["codex"]
        self.assertEqual(call["entry"],4200)
        self.assertEqual(call["outcomes"]["1d"]["ret"],0)
        self.assertFalse(call["outcomes"]["1d"]["hit"])
        self.assertGreater(call["entry_at"],completed.isoformat())
        early = evaluate(self.store.conn, completed)["recent"][0]["calls"]["codex"]
        self.assertIsNone(early["entry"])

    def test_missing_post_completion_price_never_falls_back_to_old_snapshot(self):
        completed, close = self.grade_setup(with_tick=False)
        score = evaluate(self.store.conn, close + timedelta(minutes=1))
        call = score["recent"][0]["calls"]["codex"]
        self.assertIsNone(call["entry"])
        self.assertEqual(call["outcomes"]["1d"]["status"],"no_data")
        self.assertEqual(evaluate(self.store.conn, completed)["recent"][0]["calls"]["codex"]["outcomes"]["1d"]["status"],"pending")

    def test_legacy_grade_preserves_old_price(self):
        _, close = self.grade_setup(legacy=True)
        call = evaluate(self.store.conn, close + timedelta(minutes=1))["recent"][0]["calls"]["codex"]
        self.assertEqual(call["price_basis"],"legacy_snapshot")
        self.assertAlmostEqual(call["outcomes"]["1d"]["ret"],.05)


if __name__ == "__main__":
    unittest.main()
