"""AI 判断成绩单：每次 AI 判断到期后，与实际金价走势对照，并和「动量」机械基准比较；多个 AI 分别计分，并统计一致/分歧时的表现。

- 短期方向（bias_short）看 1、3 个交易日后；中长期方向（bias_long）看 20、60 个交易日后
- 命中：看多 → 涨幅 > 阈值；看空 → 跌幅 > 阈值；中性 → 涨跌在 ±阈值内
  短期阈值取卖出手续费 0.4%（方向判断只有超过交易成本才有用），20/60 日分别取 2%/4%
- 新判断入场 = 分析完成后 5 分钟内首个有效监控现货价；旧判断保留记录的价格；出场 = 第 N 个纽约交易日收盘（17:00 ET）前最后一笔现货价（监控程序 ticks）
  不用期货日线做出场价：GC=F 与现货有约 0.6% 基差，已超过短期阈值
- 动量基准：判断前最近 N 个交易日（短期 5 日、20 日周期 20 日、60 日周期 60 日）的涨跌按同一阈值给出方向；
  AI 若跑不赢它，说明方向判断没有价值
- 一致/分歧：同一轮（run_id 相同）两个 AI 都已到期时，比较「两者一致时」的命中率，以及分歧时谁对
纯读数据库，网页（只读连接）与命令行共用。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TICK_WINDOW = timedelta(hours=6)   # 收盘前 6 小时内必须有现货价，否则视为缺数据
ANALYST_NAMES = {"claude": "Claude", "codex": "Codex"}

HORIZONS = [
    {"key": "1d", "label": "1 个交易日", "field": "bias_short", "days": 1, "threshold": 0.004, "momentum_days": 5},
    {"key": "3d", "label": "3 个交易日", "field": "bias_short", "days": 3, "threshold": 0.004, "momentum_days": 5,
     "probs": ("p_up_3d", "p_down_3d"), "base_probs": (0.35, 0.35)},
    {"key": "20d", "label": "20 个交易日", "field": "bias_long", "days": 20, "threshold": 0.02, "momentum_days": 20},
    {"key": "60d", "label": "60 个交易日", "field": "bias_long", "days": 60, "threshold": 0.04, "momentum_days": 60,
     "probs": ("p_up_3m", "p_down_3m"), "base_probs": (0.30, 0.30)},
]


def call_from_return(ret: float, threshold: float) -> str:
    return "bullish" if ret > threshold else "bearish" if ret < -threshold else "neutral"


def is_hit(call: str, ret: float, threshold: float) -> bool:
    return call == call_from_return(ret, threshold)


def bar_close_utc(day: date) -> datetime:
    return datetime.combine(day, time(17, 0), NY).astimezone(timezone.utc)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def spot_at_close(conn: sqlite3.Connection, close_utc: datetime) -> Optional[float]:
    row = conn.execute(
        "SELECT xauusd FROM ticks WHERE market_open=1 AND xauusd IS NOT NULL AND ts <= ? AND ts >= ? "
        "ORDER BY ts DESC LIMIT 1",
        (close_utc.isoformat(timespec="seconds"), (close_utc - TICK_WINDOW).isoformat(timespec="seconds")),
    ).fetchone()
    return row[0] if row else None


def brier(p_up: float, p_down: float, ret: float, threshold: float) -> float:
    """三分类 Brier 分数（涨 / 跌 / 横盘），0 为完美，越低越准。"""
    actual = call_from_return(ret, threshold)
    probs = {"bullish": p_up, "bearish": p_down, "neutral": max(0.0, 1 - p_up - p_down)}
    return sum((p - (1.0 if k == actual else 0.0)) ** 2 for k, p in probs.items())


def _rate(hits: List[bool]) -> Optional[float]:
    return sum(hits) / len(hits) if hits else None


def _horizon_stats(rows: List[Dict[str, Any]], h: Dict[str, Any]) -> Dict[str, Any]:
    key = h["key"]
    done = [r["outcomes"][key] for r in rows if r["outcomes"][key]["status"] == "done"]
    directional = [o for o in done if o["call"] != "neutral"]
    with_base = [o for o in done if o["baseline"] is not None]
    by_call = {}
    for c in ("bullish", "bearish", "neutral"):
        group = [o["ret"] for o in done if o["call"] == c]
        by_call[c] = {"n": len(group), "mean_ret": sum(group) / len(group) if group else None}
    by_conf = {}
    for conf in ("low", "medium", "high"):
        group = [r["outcomes"][key] for r in rows if r["confidence"] == conf and r["outcomes"][key]["status"] == "done"]
        by_conf[conf] = {"n": len(group), "hit_rate": _rate([o["hit"] for o in group])}
    return {
        **{k: h[k] for k in ("key", "label", "field", "days", "threshold")},
        "done": len(done),
        "pending": sum(r["outcomes"][key]["status"] == "pending" for r in rows),
        "no_data": sum(r["outcomes"][key]["status"] == "no_data" for r in rows),
        "hit_rate": _rate([o["hit"] for o in done]),
        "directional_n": len(directional),
        "directional_hit_rate": _rate([o["hit"] for o in directional]),
        "baseline_n": len(with_base),
        "ai_hit_rate_vs_base": _rate([o["hit"] for o in with_base]),
        "baseline_hit_rate": _rate([o["baseline_hit"] for o in with_base]),
        "by_call": by_call, "by_confidence": by_conf,
        "brier_n": len([o for o in done if "brier" in o]),
        "brier": (sum(o["brier"] for o in done if "brier" in o) / len([o for o in done if "brier" in o]))
                 if any("brier" in o for o in done) else None,
        "brier_base": (sum(o["brier_base"] for o in done if "brier" in o) / len([o for o in done if "brier" in o]))
                      if any("brier" in o for o in done) else None,
    }


def evaluate(conn: sqlite3.Connection, now: Optional[datetime] = None, recent_limit: int = 20, view_ids: Optional[List[int]] = None, visible_models=None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not _table_exists(conn, "ai_views"):
        return {"enabled": False, "views": 0, "analysts": [], "agreement": [], "recent": []}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_views)")}
    analyst_col = "analyst" if "analyst" in cols else "'claude'"
    run_col = "run_id" if "run_id" in cols else "NULL"
    timing_cols = ", completed_at, snapshot_at, started_at" if "completed_at" in cols else ", NULL, NULL, NULL"
    visibility_clause=''
    visibility_args=[]
    if visible_models is not None:
        visibility_args=list(visible_models)
        visibility_clause=(' AND '+analyst_col+' IN ('+','.join('?' for _ in visibility_args)+')') if visibility_args else ' AND 0'
    views = conn.execute(
        f"SELECT ts, xauusd, view_json, {analyst_col}, {run_col}{timing_cols} FROM ai_views "
        "WHERE error IS NULL AND xauusd IS NOT NULL" + (" AND rowid IN (" + ",".join("?" for _ in view_ids) + ")" if view_ids else "") + visibility_clause + " ORDER BY ts", [*(view_ids or []),*visibility_args]).fetchall()
    bars: List[Tuple[date, float]] = []
    if _table_exists(conn, "daily_bars"):
        bars = [(date.fromisoformat(r[0]), r[1]) for r in conn.execute("SELECT day, xau_close FROM daily_bars ORDER BY day")]
    closes = [bar_close_utc(d) for d, _ in bars]

    rows: List[Dict[str, Any]] = []
    for ts, entry, view_json, analyst, run_id, completed_at, snapshot_at, started_at in views:
        t = datetime.fromisoformat(completed_at or ts)
        price_basis, entry_at = "legacy_snapshot", ts
        if completed_at:
            price_basis = "first_tick_after_completion"
            tick = conn.execute(
                "SELECT ts,xauusd FROM ticks WHERE market_open=1 AND xauusd>0 AND ts>=? AND ts<=? "
                "ORDER BY ts,rowid LIMIT 1",
                (completed_at, min(now, t + timedelta(minutes=5)).isoformat(timespec="seconds"))).fetchone()
            entry, entry_at = (tick[1], tick[0]) if tick else (None, None)
            if entry_at:
                t = datetime.fromisoformat(entry_at)
        view = json.loads(view_json)
        j = next((i for i, c in enumerate(closes) if c > t), len(closes))   # 判断之后第一个收盘的交易日
        row = {"ts": ts, "run_id": run_id or ts, "analyst": analyst, "entry": entry,
               "bias_short": view["bias_short"], "bias_long": view["bias_long"],
               "confidence": view["confidence"], "outcomes": {}, "price_basis": price_basis,
               "entry_at": entry_at, "snapshot_at": snapshot_at, "started_at": started_at,
               "completed_at": completed_at}
        for h in HORIZONS:
            if entry is None:
                waiting = now <= datetime.fromisoformat(completed_at) + timedelta(minutes=5)
                row["outcomes"][h["key"]] = {"status": "pending" if waiting else "no_data"}
                continue
            k = j + h["days"] - 1
            if k >= len(bars) or closes[k] > now:
                row["outcomes"][h["key"]] = {"status": "pending"}
                continue
            exit_price = spot_at_close(conn, closes[k])
            if exit_price is None:
                row["outcomes"][h["key"]] = {"status": "no_data"}
                continue
            ret = exit_price / entry - 1
            call = view[h["field"]]
            m = h["momentum_days"]
            baseline = None
            if j - 1 - m >= 0:   # 动量只用判断前已收盘的日线（期货收盘之间的比值，基差约掉）
                baseline = call_from_return(bars[j - 1][1] / bars[j - 1 - m][1] - 1, h["threshold"])
            row["outcomes"][h["key"]] = {
                "status": "done", "ret": ret, "call": call, "hit": is_hit(call, ret, h["threshold"]),
                "baseline": baseline, "baseline_hit": is_hit(baseline, ret, h["threshold"]) if baseline else None,
            }
            if h.get("probs") and h["probs"][0] in view:   # AI 给出了概率：计算概率准确度
                up, down = (float(view[k]) for k in h["probs"])
                row["outcomes"][h["key"]]["brier"] = brier(up, down, ret, h["threshold"])
                row["outcomes"][h["key"]]["brier_base"] = brier(*h["base_probs"], ret, h["threshold"])
        rows.append(row)

    analysts = []
    for name in sorted({r["analyst"] for r in rows}, key=lambda n: list(ANALYST_NAMES).index(n) if n in ANALYST_NAMES else 99):
        subset = [r for r in rows if r["analyst"] == name]
        analysts.append({"key": name, "name": ANALYST_NAMES.get(name, name), "views": len(subset),
                         "horizons": [_horizon_stats(subset, h) for h in HORIZONS]})

    # 同一轮两个 AI 的一致 / 分歧
    runs: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        runs[r["run_id"]][r["analyst"]] = r
    agreement = []
    for h in HORIZONS:
        key = h["key"]
        pairs = [rs for rs in runs.values() if len(rs) >= 2
                 and all(r["outcomes"][key]["status"] == "done" for r in rs.values())]
        agree = [rs for rs in pairs if len({r["outcomes"][key]["call"] for r in rs.values()}) == 1]
        disagree = [rs for rs in pairs if rs not in agree]
        wins: Dict[str, int] = defaultdict(int)
        for rs in disagree:
            for name, r in rs.items():
                wins[name] += r["outcomes"][key]["hit"]
        agreement.append({
            "key": key, "label": h["label"], "pairs": len(pairs),
            "agree_n": len(agree),
            "agree_hit_rate": _rate([next(iter(rs.values()))["outcomes"][key]["hit"] for rs in agree]),
            "disagree_n": len(disagree), "disagree_wins": dict(wins),
        })

    recent = []
    for run_id in sorted(runs, reverse=True)[:recent_limit]:
        rs = runs[run_id]
        recent.append({"ts": min(r["ts"] for r in rs.values()), "calls": {
            name: {k: r[k] for k in ("ts", "bias_short", "bias_long", "confidence", "outcomes", "price_basis", "entry", "entry_at", "snapshot_at", "started_at", "completed_at")} for name, r in rs.items()}})
    return {"enabled": True, "views": len(rows), "runs": len(runs), "analysts": analysts,
            "agreement": agreement, "recent": recent, "evaluated_at": now.isoformat(timespec="seconds")}


def format_summary(card: Dict[str, Any]) -> str:
    if not card.get("enabled") or not card.get("views"):
        return "**AI 判断成绩单**：暂无 AI 判断记录"
    lines = [f"**AI 判断成绩单**（{card['runs']} 轮、{card['views']} 次判断）"]
    for a in card["analysts"]:
        lines.append(f"\n{a['name']}（{a['views']} 次）")
        for h in a["horizons"]:
            if not h["done"]:
                status = "、".join(x for x in (f"待到期 {h['pending']} 次" if h["pending"] else "",
                                               f"缺现货价 {h['no_data']} 次" if h["no_data"] else "") if x)
                lines.append(f"- {h['label']}：{status or '无数据'}")
                continue
            base = (f"，同样本动量基准 {h['baseline_hit_rate']:.0%}（AI {h['ai_hit_rate_vs_base']:.0%}）"
                    if h["baseline_hit_rate"] is not None else "")
            lines.append(f"- {h['label']}（±{h['threshold']:.1%}）：已到期 {h['done']} 次，命中率 {h['hit_rate']:.0%}{base}")
    agreed = [g for g in card["agreement"] if g["pairs"]]
    if agreed:
        lines.append("\n两个 AI 一致 / 分歧")
        for g in agreed:
            rate = f"{g['agree_hit_rate']:.0%}" if g["agree_hit_rate"] is not None else "—"
            wins = "、".join(f"{ANALYST_NAMES.get(n, n)} 对 {c} 次" for n, c in g["disagree_wins"].items()) or "—"
            lines.append(f"- {g['label']}：一致 {g['agree_n']} 次（命中 {rate}），分歧 {g['disagree_n']} 次（{wins}）")
    return "\n".join(lines)
