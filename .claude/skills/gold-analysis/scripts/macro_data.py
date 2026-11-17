"""宏观数据（TradingView 公共接口，无需浏览器）：经济日历 + 美元指数/美债收益率/离岸人民币。

由代码直接取数，AI 只负责解读，不再联网搜索这些数字：更快，也避免搜到旧数据。
「硬事件」（议息、美联储主席讲话、非农、CPI、核心 PCE）由代码按日历判定，不交给模型猜。

用法: python macro_data.py   → 人类可读摘要 + JSON
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from net_proxy import apply_proxy

CALENDAR_URL = "https://economic-calendar.tradingview.com/events?from={start}&to={end}&countries={countries}"
SCAN_URL = "https://scanner.tradingview.com/global/scan"
TV_HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
BJ = ZoneInfo("Asia/Shanghai")

MACRO_TICKERS = {"TVC:DXY": "美元指数", "TVC:US10Y": "美国10年期国债收益率", "TVC:US02Y": "美国2年期国债收益率",
                 "FX_IDC:USDCNH": "离岸人民币 USD/CNH"}

# 一级（硬事件）：对金价可能造成剧烈波动、需要暂停加仓的美国日程
TIER1_PATTERNS = [r"Fed Interest Rate Decision", r"Fed Press Conference", r"FOMC Economic Projections",
                  r"Fed Chair .* Speech", r"Non Farm Payrolls", r"Unemployment Rate", r"^Inflation Rate",
                  r"^Core Inflation Rate", r"Core PCE Price Index"]
TIER2_PATTERNS = [r"FOMC Minutes", r"Industrial Production MoM", r"Beige Book", r"Retail Sales Control Group"]


def _get_json(url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, timeout: float = 15):
    req = urllib.request.Request(url, data=data, headers=headers or TV_HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def event_tier(event: Dict[str, Any]) -> int:
    """1 = 硬事件；2 = 其他重要数据；3 = 一般。"""
    if event.get("country") == "US" and any(re.search(p, event.get("title", "")) for p in TIER1_PATTERNS):
        return 1
    if event.get('country')=='US' and any(re.search(p,event.get('title',''),re.I) for p in TIER2_PATTERNS):return 2
    return 2 if (event.get("importance") or 0) >= 1 else 3


def fetch_calendar(days_ahead: int = 7, days_back: int = 1, countries: str = "US,CN") -> List[Dict[str, Any]]:
    """返回重要事件（TradingView importance ≥ 1 或一级事件），北京时间。"""
    now = datetime.now(timezone.utc)
    url = CALENDAR_URL.format(start=(now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                              end=(now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                              countries=countries)
    events = []
    for e in _get_json(url).get("result", []):
        tier = event_tier(e)
        if tier > 2:
            continue
        at = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        events.append({
            "time_utc": at.isoformat(), "time_bj": at.astimezone(BJ).strftime("%m-%d %H:%M"),
            "country": e.get("country"), "title": e.get("title"), "tier": tier,
            "actual": e.get("actual"), "forecast": e.get("forecast"), "previous": e.get("previous"),
            "released": e.get("actual") is not None,
        })
    return sorted(events, key=lambda x: x["time_utc"])


def hard_events_within(events: List[Dict[str, Any]], hours: float = 24) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return [e for e in events if e["tier"] == 1 and not e["released"]
            and now <= datetime.fromisoformat(e["time_utc"]) <= now + timedelta(hours=hours)]


def fetch_macro_quotes() -> Dict[str, Any]:
    body = json.dumps({"symbols": {"tickers": list(MACRO_TICKERS)}, "columns": ["close", "change"]}).encode()
    rows = _get_json(SCAN_URL, data=body, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})["data"]
    return {MACRO_TICKERS[r["s"]]: {"value": r["d"][0], "change_pct": round(r["d"][1], 3)} for r in rows}


def snapshot() -> Dict[str, Any]:
    """供 AI 上下文与 skill 使用；任一部分失败只记录错误，不抛出。"""
    out: Dict[str, Any] = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "errors": []}
    try:
        out["calendar"] = fetch_calendar()
        out["hard_events_24h"] = hard_events_within(out["calendar"])
    except Exception as exc:
        out["calendar"], out["hard_events_24h"] = None, None
        out["errors"].append(f"经济日历: {exc}")
    try:
        out["quotes"] = fetch_macro_quotes()
    except Exception as exc:
        out["quotes"] = None
        out["errors"].append(f"宏观行情: {exc}")
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    apply_proxy()
    snap = snapshot()
    print("=" * 50)
    for name, q in (snap.get("quotes") or {}).items():
        print(f"{name}: {q['value']}（{q['change_pct']:+.2f}%）")
    print("-" * 50)
    for e in snap.get("calendar") or []:
        mark = "🔴硬事件" if e["tier"] == 1 else "  重要  "
        result = f"实际 {e['actual']} / 预期 {e['forecast']} / 前值 {e['previous']}"
        print(f"{mark} {e['time_bj']} {e['country']} {e['title']} ｜ {result}")
    print("=" * 50)
    print("JSON:")
    print(json.dumps(snap, ensure_ascii=False, indent=2))
