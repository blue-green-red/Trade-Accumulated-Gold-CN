"""网页看板：读取 SQLite 展示行情、计划、AI 判断与模拟盘战绩，并提供模型开关。

  python3 -m monitor.web                    默认只监听 127.0.0.1:8080（经 SSH 隧道访问）
  GOLD_WEB_HOST=0.0.0.0 python3 -m monitor.web   对外监听，必须设置 GOLD_WEB_PASSWORD

纯标准库实现，独立进程运行；网页不联网取数，全部来自监控程序写入的数据库；模型开关接口只写持久化控制状态。
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import BANK_NAMES, Settings
from .scorecard import evaluate as evaluate_scorecard
from .strategies import REGISTRY
from .controls import control, set_enabled, identity, short_labels, daily_keys
from .performance import comparison, autonomous_comparison
from .autonomous import summaries as portfolio_summaries, enabled as autonomous_enabled, keys as portfolio_keys, exists as portfolios_exist
from .execution import EXECUTION_NAME, execution_quote
from .ai_events import read as read_events
from .cadence import usage as ai_run_usage

log = logging.getLogger("monitor.web")

STATIC = Path(__file__).resolve().parent / "static"
SERIES_ORDER = ["hold", "position", "short", "claude:position_ai", "codex:position_ai", "claude:short_ai", "codex:short_ai"] + [f"{m}:{p}_{s}" for m in ("claude","codex") for p in ("short","medium","long") for s in ("full","empty")]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def get_state(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def hourly_prices(conn: sqlite3.Connection, bank: str, since: str) -> List[Dict[str, Any]]:
    """积存金估值按小时取最后一笔，用于重建各账户净值曲线。"""
    column = "zheshang_est" if bank == "zheshang" else "minsheng_est"
    cutover = get_state(conn, "jd_execution_since")
    price_column = f"CASE WHEN ts >= ? THEN jd_price ELSE {column} END" if cutover else column
    params = (cutover, since, cutover) if cutover else (since,)
    rows = conn.execute(
        f"SELECT ts, {price_column} p FROM ticks WHERE rowid IN (SELECT MAX(rowid) FROM ticks WHERE ts >= ? "
        f"AND {price_column} IS NOT NULL GROUP BY substr(ts,1,13)) ORDER BY ts", params,
    ).fetchall()
    return [{"ts": r["ts"], "p": r["p"]} for r in rows]


def equity_curves(conn: sqlite3.Connection, settings: Settings) -> Dict[str, Any]:
    """按成交记录重建每个账户的持仓，再用每小时积存金估值算净值（按「现在全卖」扣手续费）。"""
    if not table_exists(conn, "accounts"):
        return {"series": [], "start": None}
    accounts = [a for a in conn.execute("SELECT * FROM accounts") if a["strategy"] in daily_keys(settings)]
    started = [a for a in accounts if json.loads(a["state_json"]).get("start_index", -1) >= 0]
    start = min((a["started_at"] for a in started), default=None)
    prices = hourly_prices(conn, settings.paper_bank, start) if start else []
    fee = settings.sell_fee_rate
    series = []
    for acct in started:
        fills = conn.execute("SELECT * FROM account_fills WHERE strategy=? ORDER BY ts", (acct["strategy"],)).fetchall()
        cash, grams, k, points = acct["budget"], 0.0, 0, []
        for pt in prices:
            if pt["ts"] < acct["started_at"]:
                continue
            while k < len(fills) and fills[k]["ts"] <= pt["ts"]:
                f = fills[k]
                if f["side"] == "buy":
                    cash -= f["grams"] * f["price"]
                    grams += f["grams"]
                else:
                    cash += f["grams"] * f["price"] - f["fee"]
                    grams -= f["grams"]
                k += 1
            value = cash + grams * pt["p"] * (1 - fee)
            points.append([pt["ts"], round(value / acct["budget"] - 1, 6)])
        series.append({"key": acct["strategy"], "name": acct["name"], "points": points})
    if table_exists(conn, "paper_equity"):
        for key, label in short_labels(settings).items():
            acct = conn.execute("SELECT * FROM paper_accounts WHERE strategy=?", (key,)).fetchone()
            if acct:
                points = [[r["ts"], round(r["value"] / acct["budget"] - 1, 6)] for r in conn.execute(
                    "SELECT ts,value FROM paper_equity WHERE strategy=? AND rowid IN "
                    "(SELECT MAX(rowid) FROM paper_equity WHERE strategy=? GROUP BY substr(ts,1,13)) ORDER BY ts", (key, key))]
                series.append({"key": key, "name": label, "points": points})
    if autonomous_enabled(settings) and portfolios_exist(conn):
        for key,label in portfolio_keys(settings).items():
            account = conn.execute("SELECT budget,started_at FROM ai_portfolios WHERE key=?",(key,)).fetchone()
            if account and account["started_at"]:
                points = [[r["ts"],round(r["value"]/account["budget"]-1,6)] for r in conn.execute(
                    "SELECT ts,value FROM ai_portfolio_equity WHERE account=? AND rowid IN (SELECT MAX(rowid) FROM ai_portfolio_equity WHERE account=? GROUP BY substr(ts,1,13)) ORDER BY ts",(key,key))]
                series.append({"key":key,"name":label,"points":points})
    series.sort(key=lambda s: SERIES_ORDER.index(s["key"]) if s["key"] in SERIES_ORDER else 99)
    return {"series": series, "start": start}


def leaderboard(conn: sqlite3.Connection, settings: Settings, price: Optional[float]) -> List[Dict[str, Any]]:
    if not table_exists(conn, "accounts"):
        return []
    fee = settings.sell_fee_rate
    board = []
    for acct in conn.execute("SELECT * FROM accounts").fetchall():
        if acct["strategy"] not in daily_keys(settings):
            continue
        state = json.loads(acct["state_json"])
        started = state.get("start_index", -1) >= 0
        cls = REGISTRY.get(acct["strategy"])
        grams, cash, cost = state["grams"], state["cash"], state["cost"]
        value = cash + grams * price * (1 - fee) if price else None
        values = [r["value"] for r in conn.execute(
            "SELECT value FROM account_equity WHERE strategy=? ORDER BY day", (acct["strategy"],)).fetchall()]
        if value is None:
            value = values[-1] if values else cash if grams <= 1e-9 else None
        peak, mdd = acct["budget"], 0.0
        for v in values + ([value] if value is not None else []):
            peak = max(peak, v)
            if peak:
                mdd = max(mdd, 1 - v / peak)
        f = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(fee),0) fee FROM account_fills WHERE strategy=?",
                         (acct["strategy"],)).fetchone()
        board.append({
            "key": acct["strategy"], "name": acct["name"] + ("（旧实验）" if acct["strategy"] in ("dca_smart_ai", "position_ai") and "旧实验" not in acct["name"] else ""), "started": started,
            "analyst": identity(acct["strategy"])[0],
            "paused": bool(identity(acct["strategy"])[0] and not control(conn, identity(acct["strategy"])[0])["enabled"]),
            "started_at": acct["started_at"] if started else None,
            "requires_ai": bool(cls and cls.requires_ai), "description": cls.description if cls else "",
            "horizon": cls.horizon if cls else "",
            "budget": acct["budget"], "value": value,
            "return": (value / acct["budget"] - 1) if value is not None else None, "max_drawdown": mdd,
            "grams": grams, "cash": cash, "avg_cost": cost / grams if grams > 1e-9 else None,
            "fills": f["c"], "fees": f["fee"],
        })
    board.sort(key=lambda b: (not b["started"], -(b["return"] or 0)))
    return board


SHORT_LABELS = {"short": "短线日内", "short_ai": "短线日内+AI"}


def short_paper(conn: sqlite3.Connection, bank: str, strategy: str, price: Optional[float], fee_rate: float, spread: float = 0, label: str = "") -> Dict[str, Any]:
    rows = conn.execute("SELECT * FROM paper_trades WHERE bank=? AND strategy=? ORDER BY id DESC LIMIT 100",
                        (bank, strategy)).fetchall()
    closed = [r["realized_cny"] for r in conn.execute(
        "SELECT realized_cny FROM paper_trades WHERE bank=? AND strategy=? AND status='closed'", (bank, strategy)).fetchall()]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p <= 0]
    open_row = conn.execute(
        "SELECT * FROM paper_trades WHERE bank=? AND strategy=? AND status='open' ORDER BY id DESC LIMIT 1",
        (bank, strategy)).fetchone()
    open_pos = None
    if open_row:
        unrealized = (price * (1 - fee_rate) - spread - open_row["entry_cny"]) * open_row["open_grams"] if price else None
        open_pos = {k: open_row[k] for k in ("id", "opened_at", "entry_cny", "grams", "open_grams", "stop_cny",
                                              "t1_cny", "t2_cny", "t1_hit", "realized_cny")}
        open_pos["unrealized"] = unrealized
    first_tick = conn.execute("SELECT MIN(ts) t FROM ticks").fetchone()["t"]
    acct = conn.execute("SELECT * FROM paper_accounts WHERE strategy=?", (strategy,)).fetchone() if table_exists(conn, "paper_accounts") else None
    budget = acct["budget"] if acct else None
    cash = budget + sum(closed) + (open_row["realized_cny"] - open_row["entry_cny"] * open_row["open_grams"] if open_row else 0) if acct else None
    value = cash + (open_row["open_grams"] * (price * (1 - fee_rate) - spread) if open_row else 0) if cash is not None and price is not None else None
    values = [r[0] for r in conn.execute("SELECT value FROM paper_equity WHERE strategy=? ORDER BY ts", (strategy,))] if table_exists(conn, "paper_equity") else []
    if value is None:
        value = values[-1] if values else cash if not open_row else None
    peak, mdd = budget or 0, 0
    for v in values + ([value] if value is not None else []):
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak) if peak else 0
    model = identity(strategy)[0]
    return {
        "budget": budget, "cash": cash, "value": value, "return": value / budget - 1 if value is not None and budget else None,
        "max_drawdown": mdd, "started_at": acct["started_at"] if acct else None,
        "analyst": model, "paused": bool(model and not control(conn, model)["enabled"]),
        "key": strategy, "label": label or SHORT_LABELS.get(strategy, strategy),
        "trades": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
        "total_pnl": round(sum(closed), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "open": open_pos, "running_since": first_tick,
        "recent": [{k: r[k] for k in ("id", "status", "opened_at", "entry_cny", "grams", "stop_cny", "t1_cny",
                                       "t2_cny", "realized_cny", "close_reason", "closed_at", "plan_json")}
                   | {"exit_cny": r["exit_cny"] if "exit_cny" in r.keys() else None,
                      "close_decision": json.loads(r["close_decision_json"]) if "close_decision_json" in r.keys() and r["close_decision_json"] else None}
                   for r in rows],
    }


def price_history(conn: sqlite3.Connection) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    day = conn.execute(
        "SELECT ts, xauusd FROM ticks WHERE rowid IN (SELECT MAX(rowid) FROM ticks WHERE ts >= ? AND market_open=1 "
        "GROUP BY substr(ts,1,15)) ORDER BY ts", ((now - timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchall()
    week = conn.execute(
        "SELECT ts, xauusd FROM ticks WHERE rowid IN (SELECT MAX(rowid) FROM ticks WHERE ts >= ? AND market_open=1 "
        "GROUP BY substr(ts,1,13)) ORDER BY ts", ((now - timedelta(days=7)).isoformat(timespec="seconds"),)).fetchall()
    return {"1d": [[r["ts"], r["xauusd"]] for r in day if r["xauusd"]],
            "7d": [[r["ts"], r["xauusd"]] for r in week if r["xauusd"]]}


ANALYST_NAMES = {"claude": "Claude", "codex": "Codex"}


def ai_section(conn: sqlite3.Connection, settings: Settings) -> Dict[str, Any]:
    if not table_exists(conn, "ai_views"):
        return {"enabled": False, "latest": None, "analysts": [], "history": []}
    has_analyst = "analyst" in {r[1] for r in conn.execute("PRAGMA table_info(ai_views)")}
    names = [n for n in settings.ai_analysts] if has_analyst else ["claude"]
    analysts = []
    for name in names:
        row = (conn.execute("SELECT * FROM ai_views WHERE error IS NULL AND analyst=? ORDER BY ts DESC LIMIT 1", (name,))
               if has_analyst else conn.execute("SELECT * FROM ai_views WHERE error IS NULL ORDER BY ts DESC LIMIT 1")).fetchone()
        if row:
            analysts.append({**json.loads(row["view_json"]), "analyst": name, "name": ANALYST_NAMES.get(name, name),
                             "ts": row["ts"], "model": row["model"], "sources": json.loads(row["sources_json"] or "[]")[:12]})
    history = conn.execute(f"SELECT ts, trigger, view_json, error{', analyst' if has_analyst else ''} FROM ai_views "
                           "ORDER BY ts DESC LIMIT 20").fetchall()

    def brief(r):
        v = json.loads(r["view_json"]) if r["view_json"] else {}
        return {"ts": r["ts"], "trigger": r["trigger"], "error": r["error"],
                "analyst": ANALYST_NAMES.get(r["analyst"], r["analyst"]) if has_analyst else "Claude",
                "bias_short": v.get("bias_short"), "bias_long": v.get("bias_long"), "confidence": v.get("confidence")}
    return {"enabled": True, "latest": analysts[0] if analysts else None, "analysts": analysts,
            "strategy_source": "independent", "history": [brief(r) for r in history]}


def jd_reference(conn, settings=None):
    """Read only; same freshness and quota states as model input, with no network calls."""
    from .jd_research import public_entry, quota_view
    now=datetime.now(timezone.utc)
    cap=(settings or Settings()).jd_research_max_requests_per_day
    quota=quota_view(conn,cap,now)
    if table_exists(conn,"jd_research_requests"):
        quota["recent_requests"]=[dict(r) for r in conn.execute("SELECT kind,endpoint,started_at,status,error_type FROM jd_research_requests ORDER BY id DESC LIMIT 10")]
    return {"quota":quota,**{kind:public_entry(conn,kind,now,quota) for kind in ("funds","orders")}}


def dashboard(settings: Settings) -> Dict[str, Any]:
    conn = connect(settings.db_path)
    try:
        snap = get_state(conn, "web_snapshot") or {}
        quote = execution_quote(snap)
        valuation_quote = quote or snap.get("jd_last_quote")
        price = valuation_quote["price"] if valuation_quote else None
        keys = daily_keys(settings)
        fills = conn.execute(
            f"SELECT * FROM account_fills WHERE strategy IN ({','.join('?' for _ in keys)}) ORDER BY ts DESC LIMIT 200",
            keys).fetchall() if keys and table_exists(conn, "account_fills") else []
        alerts = conn.execute("SELECT ts, title, delivered FROM alerts ORDER BY ts DESC LIMIT 20").fetchall()
        board = leaderboard(conn, settings, price)
        if autonomous_enabled(settings):
            board += portfolio_summaries(conn,settings,price)
        curves = equity_curves(conn, settings)
        shorts = [short_paper(conn, settings.paper_bank, key, price, settings.sell_fee_rate,
                              settings.spread_cny, label) for key, label in short_labels(settings).items()]
        if autonomous_enabled(settings) and portfolios_exist(conn):
            for r in conn.execute("SELECT f.*,v.view_json,v.run_id FROM ai_portfolio_fills f LEFT JOIN ai_views v ON v.rowid=f.view_id ORDER BY f.id DESC LIMIT 200"):
                view = json.loads(r["view_json"]) if r["view_json"] else {}
                action = (view.get("account_decisions") or {}).get(r["account"].split(":",1)[1])
                brief = {k:view[k] for k in ("p_up_3d","p_down_3d","p_up_3m","p_down_3m") if k in view}
                brief.update({"summary":r["reason"],"_analyst":identity(r["account"])[0],"_view_id":r["view_id"],"_run_id":r["run_id"]})
                fills.append({"strategy":r["account"],**{k:r[k] for k in ("ts","side","grams","price","fee","reason")},
                    "decision_json":json.dumps({"analyst":identity(r["account"])[0],"version":view.get("_trading_version","autonomous-six-v1"),"view":brief,
                        "order":action,"execution_quote":json.loads(r["quote_json"])})})
            fills.sort(key=lambda f:f["ts"],reverse=True)
        from .manual_ai import status as manual_status
        controls = []
        for name in settings.ai_analysts:
            if name not in ("claude", "codex"):
                continue
            status = get_state(conn, f"ai_status:{name}") or {}
            controls.append({"key": name, "name": name.title(), **control(conn, name), **status,
                             "runtime": get_state(conn, f"ai_runtime:{name}") or {}, "manual":manual_status(conn,name),
                             "runs_today": ai_run_usage(conn,name,datetime.now(timezone.utc)), "daily_cap": settings.ai_max_runs_per_day,
                             "urgent_reserve": settings.ai_urgent_reserve, "pending_events": len(read_events(conn, name)["pending"])})
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bank": settings.paper_bank, "bank_name": EXECUTION_NAME,
            "execution": {"name": EXECUTION_NAME, "quote": quote, "valuation_quote": valuation_quote,
                          "ready": quote is not None, "since": get_state(conn, "jd_execution_since")},
            "sell_fee_rate": settings.sell_fee_rate, "snapshot": snap,
            "leaderboard": board, "equity": curves, "short": shorts,
            "trading_mode": settings.ai_trading_mode,
            "cadence": {"review_minutes":settings.ai_short_review_minutes,"min_move_pct":settings.ai_periodic_min_move_pct,
                        "max_idle_minutes":settings.ai_review_max_idle_minutes},
            "model_controls": controls, "comparison": (autonomous_comparison(conn,settings,board,shorts,curves,price)
                if autonomous_enabled(settings) else comparison(conn, board, shorts, curves)),
            "autonomous": {"accounts": portfolio_summaries(conn,settings,price),
                "plans": [dict(r) | {"decision":json.loads(r["decision_json"])} for r in conn.execute("SELECT * FROM ai_plans ORDER BY id DESC LIMIT 100")]
                    if portfolios_exist(conn) else [],
                "recent_expired": [dict(r) | {"decision":json.loads(r["decision_json"])} for r in conn.execute(
                    "SELECT * FROM ai_plans WHERE (status='expired' OR (status='pending' AND julianday(expires_at)<=julianday(?)) OR "
                    "(status='executed' AND json_extract(decision_json,'$.action') IN ('hold','wait','cancel') AND julianday(expires_at)<=julianday(?))) "
                    "ORDER BY julianday(expires_at) DESC,id DESC LIMIT 6",(datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))]
                    if portfolios_exist(conn) else [],
                "fills": [dict(r) for r in conn.execute("SELECT * FROM ai_portfolio_fills ORDER BY id DESC LIMIT 200")]
                    if portfolios_exist(conn) else []},
            "prices": price_history(conn),
            "ai": ai_section(conn, settings),
            "scorecard": evaluate_scorecard(conn),
            "macro": get_state(conn, "web_macro"),
            "ai_inputs": get_state(conn, "ai_inputs"),
            "jd_research": jd_reference(conn,settings),
            "jin10": (get_state(conn, "jin10_flash") or [])[:30],
            "jin10_triage": get_state(conn, "jin10_triage") or {},
            "fills": [{k: r[k] for k in ("strategy", "ts", "side", "grams", "price", "fee", "reason")} | {"decision": json.loads(r["decision_json"]) if "decision_json" in r.keys() and r["decision_json"] else None} for r in fills],
            "alerts": [{"ts": r["ts"], "title": r["title"], "delivered": bool(r["delivered"])} for r in alerts],
            "strategy_names": {**{k: cls.name for k, cls in REGISTRY.items()},**portfolio_keys(settings)},
            "series_order": SERIES_ORDER,
        }
    finally:
        conn.close()


AUTH_FAIL_LIMIT = 10            # 同一 IP 在窗口内最多输错次数
AUTH_FAIL_WINDOW = 600          # 秒
_auth_failures: Dict[str, deque] = defaultdict(deque)
_auth_lock = threading.Lock()


def history_page(settings, query):
    """Read paged history, applying model/date/account filters before pagination."""
    kind=query.get("kind",["fills"])[0]
    offset=max(0,int(query.get("offset",["0"])[0]))
    limit=min(100,max(1,int(query.get("limit",["50"])[0])))
    model=query.get("model",["all"])[0]
    account=query.get("account",["all"])[0]
    days=query.get("days",["all"])[0]
    since=(datetime.now(timezone.utc)-timedelta(days=max(1,int(days)))).isoformat() if days!="all" else None
    sources=[]
    with closing(connect(settings.db_path)) as conn:
        if kind=="fills":
            if table_exists(conn,"account_fills"):
                keys=list(daily_keys(settings))
                if keys: sources.append(("SELECT strategy AS account,ts AS stamp,'rules' AS source,rowid AS id,* FROM account_fills WHERE strategy IN ("+','.join('?' for _ in keys)+")",keys))
            if table_exists(conn,"paper_fills"):
                keys=list(short_labels(settings))
                if keys: sources.append(("SELECT t.strategy AS account,f.ts AS stamp,'short' AS source,f.*,t.plan_json FROM paper_fills f JOIN paper_trades t ON t.id=f.trade_id WHERE t.strategy IN ("+','.join('?' for _ in keys)+")",keys))
            if portfolios_exist(conn): sources.append(("SELECT account,ts AS stamp,'ai' AS source,* FROM ai_portfolio_fills",[]))
        elif kind=="plans":
            if portfolios_exist(conn): sources.append(("SELECT account,created_at AS stamp,* FROM ai_plans",[]))
        elif kind=="analysis":
            if table_exists(conn,"ai_views"): sources.append(("SELECT analyst AS account,ts AS stamp,rowid AS id,* FROM ai_views",[]))
        elif kind=="cycles":
            if portfolios_exist(conn): sources.append(("SELECT account,COALESCE(closed_at,opened_at) AS stamp,* FROM ai_portfolio_cycles",[]))
            keys=list(short_labels(settings))
            if keys and table_exists(conn,"paper_trades"): sources.append(("SELECT strategy AS account,COALESCE(closed_at,opened_at) AS stamp,* FROM paper_trades WHERE strategy IN ("+','.join('?' for _ in keys)+")",keys))
        elif kind=="alerts": sources.append(("SELECT '' AS account,ts AS stamp,rowid AS id,* FROM alerts",[]))
        else: raise ValueError("unknown history kind")
        rows=[]
        for sql,params in sources:
            clauses=[]
            if model=="rules": clauses.append("account NOT LIKE '%:%'" if kind!="analysis" else "0")
            elif model!="all":
                clauses.append("(account=? OR account LIKE ?)");params+= [model,model+':%']
            if account!="all": clauses.append("account=?");params.append(account)
            if since: clauses.append("stamp>=?");params.append(since)
            where=" WHERE "+" AND ".join(clauses) if clauses else ""
            rows += [dict(r) for r in conn.execute("SELECT * FROM ("+sql+")"+where+" ORDER BY stamp DESC,id DESC,account DESC LIMIT ?",params+[offset+limit+1])]
        rows.sort(key=lambda r:(r["stamp"],r.get("id",0),r["account"]),reverse=True)
        page=rows[offset:offset+limit]
        outcomes={}
        if kind=="analysis" and page:
            score=evaluate_scorecard(conn,recent_limit=len(page),view_ids=[r["id"] for r in page])
            for run in score["recent"]:
                for name,call in run["calls"].items(): outcomes[(name,call.get("completed_at") or call["ts"])]=call["outcomes"]
        for r in page:
            if kind=="plans": r["decision"]=json.loads(r["decision_json"])
            if kind=="fills":
                r["strategy"]=r["account"]
                r["decision"]=json.loads(r["decision_json"]) if r.get("decision_json") else None
                if r.get("plan_json"):
                    plan=json.loads(r["plan_json"])
                    r["decision"]={"analyst":identity(r["account"])[0],"version":plan.get("version"),"view":plan.get("decision"),"execution_quote":json.loads(r["quote_json"]) if r.get("quote_json") else plan.get("execution_quote")}
                if r.get("view_id"):
                    v=conn.execute("SELECT view_json,run_id FROM ai_views WHERE rowid=?",(r["view_id"],)).fetchone()
                    view=json.loads(v["view_json"]) if v and v["view_json"] else {}
                    r["decision"]={"analyst":r["account"].split(':')[0],"version":view.get("_trading_version","autonomous-six-v1"),"view":{"summary":r["reason"],"_view_id":r["view_id"],"_run_id":v["run_id"] if v else None},"order":(view.get("account_decisions") or {}).get(r["account"].split(':')[1]),"execution_quote":json.loads(r["quote_json"])}
            if kind=="analysis":
                r["view"]=json.loads(r["view_json"]) if r.get("view_json") else {}
                r["outcomes"]=outcomes.get((r["analyst"],r.get("completed_at") or r["ts"]))
        return {"items":page,"offset":offset,"limit":limit,"has_more":len(rows)>offset+limit}


def _locked_out(ip: str) -> bool:
    now = time.monotonic()
    with _auth_lock:
        q = _auth_failures[ip]
        while q and now - q[0] > AUTH_FAIL_WINDOW:
            q.popleft()
        return len(q) >= AUTH_FAIL_LIMIT


def _record_failure(ip: str) -> None:
    with _auth_lock:
        _auth_failures[ip].append(time.monotonic())


class Handler(BaseHTTPRequestHandler):
    settings: Settings
    password: str
    server_version = "gold-dashboard"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given.encode(), self.password.encode())

    def _send(self, code: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        ip = self.client_address[0]
        if _locked_out(ip):
            self._send(429, b"too many attempts", "text/plain")
            return
        if not self._authorized():
            if self.headers.get("Authorization"):
                _record_failure(ip)
            self._send(401, b"authentication required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="gold-monitor", charset="UTF-8"'})
            return
        origin = self.headers.get("Origin")
        if (self.headers.get("X-Gold-Control") != "1" or
                self.headers.get("Content-Type", "").split(";")[0] != "application/json" or
                (origin and urlsplit(origin).netloc != self.headers.get("Host"))):
            self._send(403, b"invalid request origin", "text/plain")
            return
        manual_route=self.path.endswith("/analyze")
        name = self.path.removeprefix("/api/models/").removesuffix("/analyze")
        if self.path != f"/api/models/{name}"+("/analyze" if manual_route else "") or name not in self.settings.ai_analysts or name not in ("claude", "codex"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024:
                raise ValueError("invalid body length")
            data = json.loads(self.rfile.read(length))
            if manual_route and data!={}:raise ValueError("manual request body must be an empty object")
            if not manual_route and (not isinstance(data, dict) or set(data) != {"enabled"} or type(data["enabled"]) is not bool):
                raise ValueError("enabled must be boolean")
            # Explicit, authenticated control endpoints alone open writable connections.
            conn = sqlite3.connect(str(self.settings.db_path), timeout=10)
            try:
                if manual_route:
                    from .manual_ai import request
                    result=request(conn,self.settings,name)
                else:result = set_enabled(conn, name, data["enabled"])
            finally:
                conn.close()
            self._send(202 if manual_route else 200, json.dumps(result,ensure_ascii=False).encode(), "application/json")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(getattr(exc,"status",400), json.dumps({"error": str(exc)}).encode(), "application/json")
        except Exception:
            log.exception("model control failed")
            self._send(503, b'{"error":"model control unavailable"}', "application/json")

    def do_GET(self):
        ip = self.client_address[0]
        if _locked_out(ip):
            self._send(429, "尝试次数过多，请 10 分钟后再试".encode(), "text/plain; charset=utf-8")
            return
        if not self._authorized():
            if self.headers.get("Authorization"):
                _record_failure(ip)
                log.warning("密码错误：%s", ip)
            self._send(401, "需要密码".encode(), "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="gold-monitor", charset="UTF-8"'})
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/history":
            try:
                body=json.dumps(history_page(self.settings,parse_qs(urlsplit(self.path).query)),ensure_ascii=False,default=str).encode()
                self._send(200,body,"application/json; charset=utf-8")
            except (ValueError,TypeError):
                self._send(400,b'{"error":"invalid history query"}',"application/json")
            except Exception:
                log.exception("history failed")
                self._send(500,b'{"error":"history unavailable"}',"application/json")
        elif path == "/api/dashboard":
            try:
                body = json.dumps(dashboard(self.settings), ensure_ascii=False, default=str).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                log.exception("dashboard 失败")
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    host = os.environ.get("GOLD_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("GOLD_WEB_PORT", "8080"))
    password = os.environ.get("GOLD_WEB_PASSWORD", "").strip()
    if host not in ("127.0.0.1", "localhost", "::1") and len(password) < settings.web_min_password_length:
        log.error("对外监听 %s 时必须在 .env 设置至少 %d 位的 GOLD_WEB_PASSWORD，已拒绝启动", host, settings.web_min_password_length)
        return 1
    Handler.settings, Handler.password = settings, password
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("网页看板已启动：http://%s:%d %s", host, port, "（已启用密码）" if password else "（仅本机）")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
