"""行情快照：TradingView(价格/OHLC/多周期信号) + CNBC(交叉核验/休市判断) + JD API + 银行页面校准。

每分钟直取京东接口积存金报价，报价与抓取时间均须在 120 秒内。
常驻监控不启动 Chromium；calibrate_banks() 仅保留给显式调用的银行参考报价工具。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config  # noqa: F401  (确保 skill scripts 已加入 sys.path)
from .config import BANK_NAMES, Settings
from .store import Store, iso, utc_now
from .execution import execution_quote

import gold_calc
from fetch_bank_quotes import fetch_bank_quotes, fetch_jd_latest_price
from live_market_data import fetch_cny_rate, fetch_spot_gold
from tv_signals import fetch_tv_snapshot

log = logging.getLogger("monitor.market")

RATIO_MAX_AGE_HOURS = 6   # 页面直取的换算比超过此时长视为过期，改用 JD API 兜底


def parse_cnbc_time(raw: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None


class Market:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.fx: Optional[Dict[str, Any]] = store.get_state("fx")
        self.ratios: Dict[str, Dict[str, Any]] = store.get_state("ratios", {})
        self.last_fx_at: Optional[datetime] = None
        self.last_bank_at: Optional[datetime] = None
        self.last_jd_quote = store.get_state("jd_last_quote")

    # ── 低频数据 ─────────────────────────────────────────────────────────
    def refresh_fx_if_due(self) -> None:
        now = utc_now()
        if self.last_fx_at and now - self.last_fx_at < timedelta(minutes=self.settings.fx_refresh_minutes):
            return
        self.last_fx_at = now
        fx = fetch_cny_rate()
        if fx["status"] == "ok":
            self.fx = {"rate": fx["rate"], "source": fx["source"], "rate_date": fx.get("rate_date"), "fetched_at": fx["fetched_at"]}
            self.store.set_state("fx", self.fx)
        else:
            log.warning("汇率获取失败，沿用上次: %s", fx.get("error"))

    def bank_due(self) -> bool:
        return self.last_bank_at is None or utc_now() - self.last_bank_at >= timedelta(minutes=self.settings.bank_refresh_minutes)

    def calibrate_banks(self, xauusd: float) -> List[Dict[str, Any]]:
        """抓银行页面校准换算比；返回需要提醒的异常（溢价越界等）。整个程序只有这里启动 Chromium。"""
        quotes = fetch_bank_quotes()
        self.last_bank_at = utc_now()
        issues: List[Dict[str, Any]] = []
        fx_rate = self.fx["rate"] if self.fx else None
        for bank, result in quotes["banks"].items():
            if result.get("status") == "ok":
                price, source = result["price"], "page"
            elif result.get("fallback_price") is not None:
                current = self.ratios.get(bank)
                if current and current.get("source") == "page" and not self._ratio_expired(current):
                    log.info("%s 页面抓取失败，沿用 %s 的页面换算比", BANK_NAMES[bank], current["ts"])
                    continue
                price, source = result["fallback_price"], "jd_api"
            else:
                log.warning("%s 报价完全失败: %s", BANK_NAMES[bank], result.get("error"))
                continue

            ratio = gold_calc.conversion_ratio(price, xauusd)
            prem = gold_calc.premium(price, xauusd, fx_rate) if fx_rate else None
            status = gold_calc.premium_status(prem) if prem is not None else "unknown"
            self.store.add_bank_quote(bank, price, xauusd, ratio, prem, source)
            if status == "abort":
                # 溢价越界说明抓错了数据：不采用该换算比，并提醒人工核实
                issues.append({"bank": bank, "price": price, "premium": prem, "status": status})
                continue
            if status == "unusual":
                issues.append({"bank": bank, "price": price, "premium": prem, "status": status})
            self.ratios[bank] = {"ratio": ratio, "price": price, "xauusd": xauusd, "premium": prem,
                                 "source": source, "ts": iso(utc_now())}
        self.store.set_state("ratios", self.ratios)
        return issues

    @staticmethod
    def _ratio_expired(entry: Dict[str, Any]) -> bool:
        return utc_now() - datetime.fromisoformat(entry["ts"]) > timedelta(hours=RATIO_MAX_AGE_HOURS)

    # ── 每分钟快照 ───────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = {"ts": utc_now(), "errors": []}
        from .trading_hours import trading_session
        snap["jd_session"] = trading_session(snap["ts"])
        tv = fetch_tv_snapshot()
        cnbc = fetch_spot_gold()
        if not snap["jd_session"]["is_open"]:
            snap["jd_price"] = None
            snap["execution_quote"] = None
        else:
            try:
                jd = fetch_jd_latest_price()
                quote = {key: jd.get(key) for key in ("source", "url", "price", "quote_at", "fetched_at", "product_sku")}
                snap["execution_quote"] = execution_quote({"execution_quote": quote})
                snap["jd_price"] = quote["price"] if snap["execution_quote"] else None
                if snap["execution_quote"]:
                    self.last_jd_quote = quote
                    self.store.set_state("jd_last_quote", quote)
                else:
                    snap["errors"].append("京东积存金报价无效或超过 120 秒，暂停模拟成交")
            except Exception as exc:
                snap["jd_price"] = None
                snap["execution_quote"] = None
                snap["errors"].append(f"JD API: {exc}")
        snap["jd_last_quote"] = self.last_jd_quote

        if tv["status"] == "ok":
            snap.update({
                "xauusd": tv["price"], "day_high": tv["day_high"], "day_low": tv["day_low"],
                "day_open": tv["day_open"], "change_pct": tv["change_pct"],
                "ratings": {"30m": tv["rating_30m"], "1h": tv["rating_1h"], "4h": tv["rating_4h"],
                            "1d": tv["rating_1d"], "1w": tv["rating_1w"]},
                "signals": tv["signals"], "tv": tv, "price_source": "tradingview",
            })
        else:
            snap["errors"].append(tv["error"])
            if cnbc["status"] == "ok":
                snap.update({"xauusd": cnbc["price"], "ratings": {}, "signals": {}, "tv": None, "price_source": "cnbc"})
            else:
                snap["errors"].append(cnbc["error"])
                snap["xauusd"] = None

        snap["cnbc_price"] = cnbc.get("price")
        market_time = parse_cnbc_time(cnbc.get("market_time", "")) if cnbc["status"] == "ok" else None
        snap["market_time"] = market_time
        if market_time is not None:
            snap["market_open"] = utc_now() - market_time < timedelta(minutes=self.settings.stale_minutes)
        else:
            snap["market_open"] = snap.get("xauusd") is not None  # 无法判断时按开市处理，并在 errors 中留痕

        from .wave_evidence import daily_indicators
        snap["daily_indicators"] = daily_indicators(snap,snap["ts"])
        snap["international_market_open"] = snap["market_open"]
        snap["market_open"] = snap["jd_session"]["is_open"]
        snap["fx"] = self.fx
        snap["ratios"] = {bank: r for bank, r in self.ratios.items() if not self._ratio_expired(r)}
        xau = snap.get("xauusd")
        snap["bank_est"] = {bank: round(xau * r["ratio"], 2) for bank, r in snap["ratios"].items()} if xau else {}
        return snap
