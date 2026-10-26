"""黄金积存金实时监控。

用法（在仓库根目录）:
  python3 -m monitor              常驻运行（systemd 托管）
  python3 -m monitor --once       跑一轮：京东接口取价 + 出计划，打印而不推送
  python3 -m monitor --dry-run    常驻运行但只打日志不推送
  python3 -m monitor --stats      查看模拟盘战绩
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List

from .config import BANK_NAMES, BJ, Settings  # 先导入：把 skill scripts 加入 sys.path

from net_proxy import apply_proxy

from .accounts import Accounts, format_leaderboard
from .ai import axis_m_from_view, format_view
from .alerts import AlertEngine, format_plan
from .scorecard import evaluate as evaluate_scorecard, format_summary as scorecard_summary
from .market import Market
from .notify import Notifier
from .paper import PaperBook
from .store import Store, utc_now
from .strategy import build_plan
from .controls import active, control, identity, short_labels
from .execution import execution_quote
from .trading_hours import trading_session
from .autonomous import keys as portfolio_keys, active_modes, policy_version, AutonomousPortfolio, VERSION as TRADING_VERSION, enabled as autonomous_enabled

log = logging.getLogger("monitor")

DEFAULT_AXIS_M = {"axis": "震荡", "note": "无有效 AI 消息面判断，默认震荡"}
DAILY_VIEW_MAX_AGE_HOURS = 18
JIN10_KEEP_HOURS = 36   # 数据库里保留多久的相关快讯（网页展示 + AI 上下文）   # 日线账户 09:05 运行时可用的 AI 判断最长时效（08:40 失败时可用前一晚 20:00 的）


class Monitor:
    def __init__(self, settings: Settings, dry_run: bool):
        self.settings = settings
        self.store = Store(settings.db_path)
        if settings.ai_trading_mode=="risk_six_account":
            from .risk_profiles import reset_experiment
            reset_experiment(self.store.conn)
        self.market = Market(settings, self.store)
        self.alerts = AlertEngine(settings, self.store)
        # 短线基准与每个模型的独立账户；关闭模型只停新开仓，持仓继续风控。
        self.short_books = {key: PaperBook(settings, self.store, key, label)
                            for key, label in short_labels(settings).items()}
        self.short_enabled = bool(self.short_books)
        # Retire the previous shared AI book without abandoning its open position.
        self.legacy_short = (PaperBook(settings, self.store, "short_ai", "旧实验 · 短线 AI")
                             if self.store.open_trade(settings.paper_bank, "short_ai") else None)
        daily = [k for k in settings.strategies if k not in ("short", "short_ai")]
        if autonomous_enabled(settings):
            daily = [k for k in daily if k in ("hold", "position")]
        self.accounts = Accounts(settings, self.store, daily)
        self.portfolios = AutonomousPortfolio(settings,self.store) if autonomous_enabled(settings) else None
        self.retired_short_books = {f"{m}:short_ai": PaperBook(settings,self.store,f"{m}:short_ai",f"旧实验 · {m.title()}")
            for m in settings.ai_analysts if self.portfolios and self.store.open_trade(settings.paper_bank,f"{m}:short_ai")}
        self.notifier = Notifier(settings, dry_run=dry_run)
        self._ai_thread: threading.Thread = None
        self._jin10_at = None
        self._jin10_seen: set = set()
        self._jin10_pending: List[Dict] = []
        self._ai_results: "queue.Queue[Dict[str, str]]" = queue.Queue()
        self.analysts: Dict[str, "AIAnalyst"] = {}
        from .ai import AIAnalyst, detect_analysts
        for name, backend in detect_analysts(settings):
            try:
                self.analysts[name] = AIAnalyst(settings, self.store, backend, analyst=name)
            except Exception as exc:
                log.warning("%s 初始化失败，跳过: %s", name, exc)
        for name in settings.ai_analysts:
            self.store.set_state(f"ai_status:{name}", {"available": name in self.analysts})
            self.store.set_state(f"ai_runtime:{name}", {"running": False})
            from .manual_ai import recover
            recover(self.store.conn,name)
        if self.analysts:
            log.info("AI 独立操盘已启用：%s", "、".join(a.model_label for a in self.analysts.values()))

    def enabled_analysts(self):
        return {name: a for name, a in self.analysts.items() if active(self.store.conn, name)}

    @property
    def ai(self) -> bool:
        return bool(self.analysts)

    def ai_running(self) -> bool:
        return bool(self._ai_thread and self._ai_thread.is_alive())

    def recent_jin10(self) -> List[Dict]:
        """主线程调用（AI 后台线程不能用这里的 SQLite 连接）：给 AI 的金十快讯——
        24 小时内金十标红的重要快讯（最多 12 条）+ 6 小时内其他相关快讯（最多 6 条），新→旧。"""
        now = datetime.now(BJ)
        day = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        six = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        flashes = sorted(self.store.get_state("jin10_flash", []), key=lambda x: x["time_bj"], reverse=True)
        chosen, seen, counts = [], set(), {True: 0, False: 0}
        for x in flashes:
            important = bool(x["important"])
            text_key = "".join(x["text"].split())
            if x["time_bj"] < (day if important else six) or text_key in seen:
                continue
            if counts[important] >= (12 if important else 6):
                continue
            seen.add(text_key)
            counts[important] += 1
            chosen.append({"time_bj": x["time_bj"], "important": important, "text": x["text"][:160]})
        return chosen

    def ai_context(self, snap, plan, jin10_recent=None) -> Dict:
        """给 AI 的上下文：行情 + 代码算出的点位 + 经济日历与宏观行情（均为程序直取，无需浏览器）。"""
        ctx = {
            "snapshot_at": (snap.get("ts") or utc_now()).isoformat(),
            "now_bj": datetime.now(BJ).strftime("%Y-%m-%d %H:%M"), "xauusd": snap["xauusd"],
            "change_pct": snap.get("change_pct"), "day_high": snap.get("day_high"), "day_low": snap.get("day_low"),
            "signals_tradingview": snap.get("signals"),
            "usdcny": (snap.get("fx") or {}).get("rate"),
            "jd_accumulation_quote": snap.get("execution_quote"),
            "jd_trading_session": trading_session(),
        }
        from .jd_research import snapshot as jd_snapshot
        from .wave_evidence import daily_reference, daily_indicators, jd_daily_samples, calendar_window
        store = Store(self.settings.db_path)
        try:
            macro = store.get_state("web_macro", {})
            now = snap.get("ts") or utc_now()
            ctx["daily_indicators"] = daily_indicators(snap,now)
            ctx["daily_trend_reference"] = daily_reference(store.conn,now)
            ctx["jd_daily_samples"] = jd_daily_samples(store.conn,now)
            from .macro_cache import age_seconds
            times = {kind: macro.get(kind + "_fetched_at", macro.get("fetched_at"))
                     for kind in ("quotes", "calendar")}
            valid = {kind: 0 <= age_seconds(stamp, utc_now()) < 3600 for kind, stamp in times.items()}
            selected_calendar=calendar_window(macro.get("calendar") if valid["calendar"] else [],now)
            calendar=selected_calendar.pop("events")
            ctx["macro"] = {"fetched_at": macro.get("fetched_at"),
                "quotes_fetched_at": times["quotes"], "calendar_fetched_at": times["calendar"],
                "quotes": macro.get("quotes") if valid["quotes"] else None,
                "calendar": calendar, "calendar_window":selected_calendar,
                "hard_events_24h": (macro.get("hard_events_24h") or [])[:8] if valid["calendar"] else [],
                "status": {kind: "available" if valid[kind] else "stale_or_missing" for kind in valid},
                "errors": macro.get("errors")}
            ctx["jd_research"] = jd_snapshot(store, self.settings.clawx_api_key, daily_cap=self.settings.jd_research_max_requests_per_day)
        finally:
            store.conn.close()
        ctx["jin10_flash_recent"] = jin10_recent or []
        if plan and plan.get("ok"):
            ctx["levels_by_code"] = {"resistance": [round(r["price"], 1) for r in plan["resistance"]],
                                     "support": [round(s["price"], 1) for s in plan["support"]],
                                     "intraday_pivot": round(plan["pivot"], 1)}
        audit = Store(self.settings.db_path)
        try:
            audit.set_state("ai_inputs", {"at": utc_now().isoformat(),
                "context_chars": len(json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))),
                "news_count": len(ctx["jin10_flash_recent"]), "max_searches": self.settings.ai_max_searches,
                "jd_research": ctx["jd_research"], "macro_at": ctx["macro"].get("fetched_at"),
                "wave_evidence": {"as_of":ctx["snapshot_at"],
                    "daily_reference":{"status":ctx["daily_trend_reference"]["status"],"last_day":ctx["daily_trend_reference"].get("last_day"),"sessions":len(ctx["daily_trend_reference"]["recent_sessions"])},
                    "daily_indicators":ctx["daily_indicators"],
                    "jd_daily":{"status":ctx["jd_daily_samples"]["status"],"days":len(ctx["jd_daily_samples"]["days"]),"missing_dates":ctx["jd_daily_samples"].get("missing_dates",[])},
                    "calendar":{"events":len(calendar),"status":ctx["macro"]["status"]["calendar"],**ctx["macro"]["calendar_window"]}}})
        finally:
            audit.conn.close()
        return ctx

    def run_ai_if_due(self, snap, force: bool = False) -> List[Dict[str, str]]:
        """AI 分析在后台线程跑（一次 2-3 分钟），监控主循环不等待；完成后的推送在下一轮取出。"""
        if not self.enabled_analysts():
            self._jin10_pending.clear()
            return []
        events: List[Dict[str, str]] = []
        while not self._ai_results.empty():
            events.append(self._ai_results.get_nowait())
        if self._jin10_pending:
            self.start_jin10_triage(snap)
        if self.ai_running():
            return events
        from .manual_ai import ready,TRIGGER
        due={}
        for name,analyst in self.enabled_analysts().items():
            if force:due[name]="手动"
            elif ready(self.store.conn,self.settings,name):
                due[name]=TRIGGER if snap.get("market_open") and execution_quote(snap) else None
            else:due[name]=analyst.due()
        due = {name: trigger for name, trigger in due.items() if trigger}
        if self.portfolios:
            due = {name:trigger for name,trigger in due.items() if self.store.conn.execute(
                "SELECT COUNT(*) FROM ai_portfolios WHERE model=? AND started_at IS NOT NULL AND key IN ("+",".join("?" for _ in portfolio_keys(self.settings))+")",(name,*portfolio_keys(self.settings))).fetchone()[0] == len(active_modes(self.settings))}
        if not due:
            return events
        if len(due) > 1:
            # Alternate the first analyst to avoid giving one model a permanent head start.
            previous = self.store.get_state("ai_first_analyst")
            order = sorted(due, key=lambda name: name == previous)
            due = {name: due[name] for name in order}
            self.store.set_state("ai_first_analyst", order[0])
        base_plan = build_plan(snap, axis_m=DEFAULT_AXIS_M, spread_cny=self.settings.spread_cny,
                               sell_fee_rate=self.settings.sell_fee_rate, ratio_bank=self.settings.paper_bank)
        self._ai_thread = threading.Thread(target=self._ai_worker, args=(dict(snap), base_plan, due, self.recent_jin10()),
                                           name="ai-analysis", daemon=True)
        self._ai_thread.start()
        return events

    # ── 金十快讯初筛 ─────────────────────────────────────────────────────
    def record_triage(self, store: Store, items: List[Dict], decision: str, reason: str, impact: str = "") -> None:
        log_map = store.get_state("jin10_triage", {})
        for x in items:
            log_map[x["id"]] = {"decision": decision, "reason": reason, "impact": impact, "at": utc_now().isoformat(timespec="seconds")}
        store.set_state("jin10_triage", dict(sorted(log_map.items(), key=lambda kv: kv[1]["at"])[-300:]))

    def start_jin10_triage(self, snap) -> None:
        """主线程：先做免费检查（每日上限、距上次分析间隔），通过后按规则触发，不额外调用 AI。"""
        pending, self._jin10_pending = self._jin10_pending, []
        today = datetime.now(BJ).strftime("%Y-%m-%d")
        if self.store.get_state("jin10_ai_triggers", {}).get(today, 0) >= self.settings.jin10_ai_daily_cap:
            self.record_triage(self.store, pending, "skip", f"今日快讯触发已达上限 {self.settings.jin10_ai_daily_cap} 次")
            return
        gap = self.settings.jin10_ai_gap_minutes
        enabled = self.enabled_analysts()
        if not enabled:
            return
        if not pending:
            return
        self.record_triage(self.store, pending, "trigger", "重要黄金相关快讯通过规则筛选，无 AI 初筛", "medium")
        caps = self.store.get_state("jin10_ai_triggers", {})
        self.store.set_state("jin10_ai_triggers", {today: caps.get(today, 0) + 1})
        for analyst in enabled.values():
            for item in pending:
                analyst.request_refresh(f"金十快讯：{item['text'][:30]}", min_gap_minutes=gap,
                    event_id="jin10:" + item["id"], payload={"source": "金十快讯", "time_bj": item.get("time_bj"),
                        "text": item["text"][:1000]})

    def _ai_worker(self, snap, base_plan, due: Dict[str, str], jin10_recent=None) -> None:
        """同一份上下文串行交给各分析师（同一 run_id），低配机器只运行一个 AI。"""
        from copy import deepcopy
        run_id = utc_now().isoformat(timespec="seconds")
        log.info("AI 消息面分析开始（%s，后台串行）", " → ".join(f"{n}:{t}" for n, t in due.items()))
        try:
            from .ai_events import read as read_events
            coverage_store = Store(self.settings.db_path)
            try:
                pending = {name: read_events(coverage_store.conn, name)["pending"] for name in due}
                portfolio_inputs = {name: self.portfolios.input(name, snap.get("execution_quote"),coverage_store,as_of=snap["ts"].isoformat())
                                    for name in due} if self.portfolios else {}
            finally:
                coverage_store.conn.close()
            context = dict(self.ai_context(snap, base_plan, jin10_recent))
            context["event_coverage"] = {name: [e["id"] for e in events] for name, events in pending.items()}
            shared = {e["id"]: e for events in pending.values() for e in events}
            context["trigger_events"] = [{k: e[k] for k in ("id", "reason", "observed_at", "data")} for e in shared.values()]
            archive = Store(self.settings.db_path)
            try:
                meta = archive.get_state("ai_inputs", {})
                sizes = {name:len(json.dumps({**context,"trading_accounts":inputs},ensure_ascii=False,separators=(",",":")))
                         for name,inputs in portfolio_inputs.items()}
                meta.update({"model_context_chars":sizes,"context_chars":max(sizes.values()) if sizes else len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
                             "trigger_events": len(shared), "event_coverage": context["event_coverage"]})
                archive.set_state("ai_inputs", meta)
                for name, inputs in portfolio_inputs.items():
                    archive.set_state(f"ai_account_input:{run_id}:{name}", inputs)
                archive.set_state(f"ai_input:{run_id}", {"policy": policy_version(self.settings) if portfolio_inputs else "shared-direct-v2",
                    "max_searches": self.settings.ai_max_searches, "context": context})
            finally:
                archive.conn.close()
        except Exception:
            log.exception("AI 上下文准备失败")
            for name in due:   # 记为失败：30 分钟后再试，避免每分钟重试
                self.analysts[name]._failed_at = utc_now()
            return

        def run_one(name: str):
            store = Store(self.settings.db_path)      # 每个线程自己的 SQLite 连接
            manual,result=None,None
            try:
                if not active(store.conn, name):
                    return name, None
                from .manual_ai import claim,TRIGGER
                if due[name]==TRIGGER:
                    manual=claim(store.conn,name,run_id)
                    if not manual:return name,None
                store.set_state(f"ai_runtime:{name}", {"running": True, "queued": False, "started_at": utc_now().isoformat()})
                model_context = deepcopy(context)
                if name in portfolio_inputs:
                    model_context["trading_accounts"] = deepcopy(portfolio_inputs[name])
                result=self.analysts[name].analyze(model_context, due[name], store=store, run_id=run_id)
                return name,result
            except Exception:
                log.exception("%s 分析线程异常", name)
                return name, None
            finally:
                if manual:
                    from .manual_ai import finish
                    finish(store.conn,name,manual,result)
                store.set_state(f"ai_runtime:{name}", {"running": False, "queued": False, "finished_at": utc_now().isoformat()})
                store.conn.close()

        status_store = Store(self.settings.db_path)
        try:
            for name in due:
                status_store.set_state(f"ai_runtime:{name}", {"running": False, "queued": True, "queued_at": run_id})
        finally:
            status_store.conn.close()
        views = dict(run_one(name) for name in due)
        done = {n: v for n, v in views.items() if v}
        if not done:
            return
        from .ai import ANALYST_NAMES
        title = "🧠" + "｜".join(f"{ANALYST_NAMES.get(n, n)}短期{axis_m_from_view(v)['axis']}" for n, v in done.items())
        parts = [f"## {ANALYST_NAMES.get(n, n)}\n\n{format_view(v)}" for n, v in done.items()]
        if len(done) >= 2:
            vs = list(done.values())
            agree = [f for f, label in (("bias_short", "短期"), ("bias_long", "中长期")) if vs[0][f] == vs[1][f]]
            parts.insert(0, "**两个 AI 判断**：" + ("、".join({"bias_short": "短期", "bias_long": "中长期"}[f] for f in agree) + "一致" if agree else "方向全部分歧")
                         + "；两个 AI 分别使用自己的判断操盘")
        self._ai_results.put({"key": f"ai:{run_id}", "title": title, "body": "\n\n---\n\n".join(parts)})

    def wait_for_ai(self) -> None:
        if self._ai_thread:
            self._ai_thread.join()

    def publish_for_web(self, snap, plan, plan_base=None, plan_source: str = "base", ai_plans=None) -> None:
        """网页端只读数据库：把本轮行情与计划存一份，网页不自己联网。"""
        keep = ("xauusd", "change_pct", "day_high", "day_low", "bank_est", "signals", "daily_indicators", "market_open", "jd_session", "international_market_open", "fx", "execution_quote", "jd_last_quote")
        payload = {k: snap.get(k) for k in keep}
        payload["ts"] = snap["ts"].isoformat()
        payload["plan"] = plan
        payload["plan_base"] = plan_base
        payload["plan_source"] = plan_source
        payload["ai_plans"] = ai_plans or {}
        self.store.set_state("web_snapshot", json.loads(json.dumps(payload, ensure_ascii=False, default=str)))

    def request_ai_after_data_release(self) -> None:
        """实际值到达后排队；同一时刻的多项结果各自去重，延迟公布可补齐。"""
        import hashlib
        import math
        from .macro_cache import age_seconds
        enabled = self.enabled_analysts()
        if not enabled:
            return
        macro = self.store.get_state("web_macro") or {}
        now = utc_now()
        stamp = macro.get("calendar_fetched_at", macro.get("fetched_at"))
        if not 0 <= age_seconds(stamp, now) < 3600:
            return
        delay = self.settings.ai_event_delay_minutes * 60
        for event in macro.get("calendar") or []:
            if event.get("country") != "US" or event.get("tier", 3) > 2:
                continue
            age = age_seconds(event.get("time_utc"), now)
            if not delay <= age <= 86400 or event.get("actual") is None or event.get("actual") == "":
                continue
            try:
                if isinstance(event["actual"], bool) or not math.isfinite(float(event["actual"])):
                    continue
            except (TypeError, ValueError):
                continue
            key = json.dumps([event["time_utc"], event["country"], event["title"]], ensure_ascii=False)
            event_id = "calendar:" + hashlib.sha256(key.encode()).hexdigest()[:24]
            for analyst in enabled.values():
                analyst.request_refresh(f"数据已公布 {event['time_bj']} {event['title']}", min_gap_minutes=15,
                    event_id=event_id, payload={**event, "released": True, "source": "经济日历"})

    def poll_jin10_if_due(self, market_open: bool) -> None:
        """金十快讯：每 2 分钟抓一次，存下与金价相关的条目；金十标红的重要快讯（10 分钟内）在交易时段触发 AI。"""
        if not self.settings.jin10_enabled:
            return
        if self._jin10_at and (utc_now() - self._jin10_at).total_seconds() < self.settings.jin10_poll_seconds:
            return
        self._jin10_at = utc_now()
        stored = self.store.get_state("jin10_flash", [])
        try:
            import jin10_flash
            # 翻页到上次抓到的位置；首次启动或停机后自动补齐最近 24 小时
            full = not self.store.get_state("jin10_backfilled")
            since = None if full else max((x["time_bj"] for x in stored), default=None)
            items = jin10_flash.fetch_since(since)
            if full:
                self.store.set_state("jin10_backfilled", True)
        except Exception as exc:
            log.warning("金十快讯抓取失败: %s", exc)
            return
        seen = self._jin10_seen | {x["id"] for x in stored}
        new = [x for x in items if x["id"] not in seen]
        self._jin10_seen.update(x["id"] for x in items)
        if len(self._jin10_seen) > 2000:
            self._jin10_seen = {x["id"] for x in items}
        related = [x for x in new if jin10_flash.is_gold_related(x)]
        if not related:
            return
        cutoff = (datetime.now(BJ) - timedelta(hours=JIN10_KEEP_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        merged = sorted({x["id"]: x for x in related + stored}.values(), key=lambda x: x["time_bj"], reverse=True)
        self.store.set_state("jin10_flash", [x for x in merged if x["time_bj"] >= cutoff])
        if len(related) > 20:
            log.info("金十快讯补抓：新增相关快讯 %d 条", len(related))

        # 触发候选：金十标红 + 严格规则 + 10 分钟内；交给 run_ai_if_due 做初筛
        now_bj = datetime.now(BJ)
        fresh = [x for x in related if jin10_flash.is_trigger_candidate(x)
                 and now_bj - datetime.strptime(x["time_bj"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ) <= timedelta(minutes=10)]
        if fresh and market_open and self.enabled_analysts():
            self._jin10_pending.extend(fresh)

    def refresh_macro_if_due(self) -> None:
        """宏观报价每 5 分钟；日历平时 30 分钟，重要公布前后每分钟。"""
        from .macro_cache import refresh
        try:
            refresh(self.store, utc_now())
        except Exception as exc:
            log.warning("宏观数据刷新失败: %s", exc)

    def build_ai_led_plan(self, snap, view, cost) -> Dict:
        """短线+AI：方向由 AI 的 3 日涨跌概率决定，规则只管止损与扣手续费后的 R:R；仓位随把握 0.5~1.5 倍。"""
        from .ai import view_probs
        p = view_probs(view)
        up, down = p["p_up_3d"], p["p_down_3d"]
        st = self.settings
        if up >= st.ai_short_entry_p and up - down >= st.ai_short_edge:
            direction, size = "bull", min(max(0.5 + (up - 0.4) * 5, 0.5), 1.5)   # 上涨概率 50%→1 倍，60% 及以上→1.5 倍
        elif down >= st.ai_short_exit_p:
            direction, size = "bear", 0.0
        else:
            direction, size = "neutral", 0.0
        plan = build_plan(snap, axis_m=axis_m_from_view(view), direction=direction,
                          ai_note=f"3 日涨 {up:.0%} / 跌 {down:.0%}", **cost)
        plan["size_mult"] = size
        plan["decision"] = view
        return plan

    def tick(self, force_bank: bool = False, force_ai: bool = False):
        events: List[Dict[str, str]] = []
        if self.portfolios:self.portfolios.expire_plans()
        self.market.refresh_fx_if_due()
        snap = self.market.snapshot()
        snap["jd_session"] = trading_session()
        snap["market_open"] = snap["jd_session"]["is_open"]
        if not snap["market_open"]:
            self.store.set_state("jd_session_closed", True)
        elif execution_quote(snap) and self.store.get_state("jd_session_closed", False):
            from .ai_events import enqueue
            for name in self.enabled_analysts():
                enqueue(self.store.conn,name,"京东开盘报价恢复",0,event_id="jd-open:"+utc_now().date().isoformat())
            self.store.set_state("jd_session_closed", False)
        if snap.get("xauusd") is None:
            log.warning("本轮取价全部失败: %s", snap["errors"])
            events += self.weekly_report_if_due(snap)
            return snap, None, events

        # JD direct quotes drive execution and conversion; no bank Chromium in the daemon.

        self.store.add_tick(snap)
        self.poll_jin10_if_due(snap["market_open"])
        if not snap["market_open"]:
            self.publish_for_web(snap, None)
            events += self.weekly_report_if_due(snap)
            return snap, None, events

        self.refresh_macro_if_due()
        if self.portfolios:
            events += self.portfolios.on_tick(snap)
        self.request_ai_after_data_release()
        if self.portfolios:
            from .cadence import request_periodic
            request_periodic(self.settings,self.store,self.enabled_analysts(),snap,utc_now())
        events += self.run_ai_if_due(snap, force=force_ai)
        cost = {"spread_cny": self.settings.spread_cny, "sell_fee_rate": self.settings.sell_fee_rate,
                "ratio_bank": self.settings.paper_bank}
        base_plan = build_plan(snap, axis_m=DEFAULT_AXIS_M, **cost)
        views = {name: a.latest(self.settings.ai_view_max_age_hours) for name, a in self.analysts.items()} if not self.portfolios else {}
        ai_plans = {name: self.build_ai_led_plan(snap, view, cost) for name, view in views.items() if view}
        plan = base_plan  # Shared alerts show the rule baseline; model plans are displayed separately.
        if self.short_enabled:
            alert_events = self.alerts.check(snap, plan)
            if self.ai and any(e["key"].startswith("move:") for e in alert_events):
                for analyst in self.enabled_analysts().values():
                    analyst.request_refresh(f"{int(self.settings.move_window_minutes)}分钟异动", min_gap_minutes=30)
            events += alert_events
            if "short" in self.short_books:
                events += self.short_books["short"].on_tick(snap, base_plan)
            for key, book in self.short_books.items():
                model, _ = identity(key)
                if model:
                    events += book.on_tick(snap, ai_plans.get(model) or {"ok": False})
        daily_views = {name: a.latest(DAILY_VIEW_MAX_AGE_HOURS) for name, a in self.analysts.items()} if not self.portfolios else {}
        for book in self.retired_short_books.values():
            events += book.on_tick(snap, {"ok": False})
        if self.legacy_short:
            events += self.legacy_short.on_tick(snap, {"ok": False})
        events += self.accounts.run_daily(snap, daily_views)
        self.publish_for_web(snap, plan, base_plan, "base", ai_plans)
        events += self.digest_if_due(snap, plan)
        events += self.weekly_report_if_due(snap)
        return snap, plan, events

    def leaderboard_text(self, snap) -> str:
        quote = execution_quote(snap) or snap.get("jd_last_quote")
        price = quote["price"] if quote else None
        short = {label: self.store.trade_stats(self.settings.paper_bank, key)
                 for key, label in short_labels(self.settings).items() if key in self.short_books}
        body = format_leaderboard(self.accounts.leaderboard(price), price, short)
        from .web import dashboard
        for model in dashboard(self.settings)["comparison"]["models"]:
            rate = f"{model['win_rate']:.0%}" if model["win_rate"] is not None else "—"
            ret = f"{model['return']:+.2%}" if model["return"] is not None else "—"
            body += f"\n\n**{model['name']} 独立操盘**：收益 {ret}，完整平仓 {model['trades']} 次，胜率 {rate}"
        return body

    def weekly_report_if_due(self, snap) -> List[Dict[str, str]]:
        now_bj = datetime.now(BJ)
        weekday, hour = self.settings.weekly_report_bj
        if now_bj.weekday() != weekday or now_bj.hour != hour:
            return []
        key = f"weekly:{now_bj:%Y-%m-%d}"
        if not self.alerts.cooled_down(key, 24 * 60):
            return []
        body = self.leaderboard_text(snap)
        if self.ai:
            body += "\n\n" + scorecard_summary(evaluate_scorecard(self.store.conn))
        return [{"key": key, "title": f"🏆模拟盘周报 {now_bj:%m-%d}", "body": body}]

    def digest_if_due(self, snap, plan) -> List[Dict[str, str]]:
        now_bj = datetime.now(BJ)
        if now_bj.hour not in self.settings.digest_hours_bj:
            return []
        key = f"digest:{now_bj:%Y-%m-%d}:{now_bj.hour}"
        if not self.alerts.cooled_down(key, 24 * 60):
            return []
        return [{"key": key, "title": f"📊黄金计划 {now_bj:%H:%M} XAUUSD {snap['xauusd']:.0f}",
                 "body": format_plan(snap, plan) + "\n\n---\n\n" + self.leaderboard_text(snap)}]

    def dispatch(self, events: List[Dict[str, str]]) -> None:
        """同一轮多条提醒合并为一条推送，节省额度。"""
        if not events:
            return
        from .notification_control import selected_events
        selected = selected_events(self.store.conn, events)
        delivered = False
        if selected:
            title = selected[0]["title"] if len(selected) == 1 else f"{selected[0]['title']} 等{len(selected)}条"
            body = "\n\n---\n\n".join(f"### {e['title']}\n\n{e['body']}" for e in selected)
            delivered = self.notifier.send(title, body)
        for e in events:
            self.store.add_alert(e["key"], e["title"], e["body"], delivered and e in selected)

    def run_forever(self) -> None:
        log.info("监控启动：推送通道=%s，每 %ss 一轮，模拟盘=%s", self.notifier.channel,
                 self.settings.tick_seconds, BANK_NAMES[self.settings.paper_bank])
        first = True
        while True:
            started = time.monotonic()
            try:
                snap, plan, events = self.tick()
                if first and plan:
                    events.insert(0, {"key": f"startup:{utc_now().isoformat()}", "title": "✅黄金监控已启动",
                                      "body": format_plan(snap, plan)})
                    first = False
                self.dispatch(events)
                if snap.get("xauusd"):
                    log.info("XAUUSD %.2f open=%s 事件=%d %s", snap["xauusd"], snap["market_open"], len(events),
                             plan["action"] if plan and plan.get("ok") else "")
            except Exception:
                log.exception("本轮异常，下一轮继续")
            time.sleep(max(1.0, self.settings.tick_seconds - (time.monotonic() - started)))


def print_stats(monitor: Monitor) -> None:
    bank = monitor.settings.paper_bank
    snap = monitor.market.snapshot()
    print(monitor.leaderboard_text(snap))
    print("\n各策略规则：")
    for b in monitor.accounts.leaderboard(None):
        print(f"- {b['name']}：{b['description']}")
    if monitor.ai:
        u = monitor.store.conn.execute(
            "SELECT COUNT(*) n, SUM(error IS NOT NULL) failed, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COALESCE(SUM(web_searches),0) s FROM ai_views WHERE ts >= date('now','-30 day')").fetchone()
        print("\n" + scorecard_summary(evaluate_scorecard(monitor.store.conn)))
        print(f"\nAI 分析（近 30 天）：{u['n']} 次（失败 {u['failed'] or 0}），输入 {u['i']:,} / 输出 {u['o']:,} tokens，联网搜索 {u['s']} 次")
    print("\n最近成交：")
    for f in monitor.store.conn.execute("SELECT * FROM account_fills ORDER BY ts DESC LIMIT 15").fetchall():
        print(f"  {f['ts'][:16]} [{f['strategy']}] {'买' if f['side'] == 'buy' else '卖'} {f['grams']:.2f}g @ {f['price']:.2f} ｜ {f['reason']}")
    for key, book in monitor.short_books.items():
        print(f"\n{book.label} 逐笔：")
        for t in monitor.store.all_trades(bank, key):
            print(f"#{t['id']} {t['status']:<6} {t['opened_at']} 入 {t['entry_cny']:.2f} × {t['grams']:g}g "
                  f"止损 {t['stop_cny']:.2f} T1 {t['t1_cny']:.2f} T2 {t['t2_cny']:.2f} "
                  f"已实现 {t['realized_cny']:+.2f} {t['close_reason'] or ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金积存金实时监控")
    parser.add_argument("--once", action="store_true", help="跑一轮并打印计划（不推送）")
    parser.add_argument("--dry-run", action="store_true", help="只打日志不推送")
    parser.add_argument("--stats", action="store_true", help="打印模拟盘战绩")
    parser.add_argument("--ai-now", action="store_true", help="配合 --once：立即跑一次 AI 消息面分析")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    apply_proxy()
    monitor = Monitor(Settings(), dry_run=args.dry_run or args.once)

    if args.stats:
        print_stats(monitor)
        return 0
    if args.once:
        if args.ai_now and not monitor.ai:
            print("AI 未启用：没有 ANTHROPIC_API_KEY，本机也找不到 claude 命令")
            return 1
        snap, plan, events = monitor.tick(force_bank=True, force_ai=args.ai_now)
        if args.ai_now:   # --once 时等后台分析完成，再跑一轮让策略用上新判断
            monitor.wait_for_ai()
            snap, plan, more = monitor.tick()
            events += more
        if plan:
            print(format_plan(snap, plan))
        else:
            print(f"休市或取价失败：market_open={snap.get('market_open')} errors={snap.get('errors')}")
        print(f"\n本轮触发事件 {len(events)} 条：")
        for e in events:
            body = e["body"] if e["key"].startswith("ai:") else e["body"][:120]
            print(f"- {e['title']}：{body}")
        return 0
    monitor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
