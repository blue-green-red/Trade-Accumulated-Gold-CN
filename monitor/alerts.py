"""行情异动内部事件与CLI计划格式；同方向事件在冷却期内只记录一次。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from .config import BANK_NAMES, BJ, Settings
from .store import Store, utc_now


def fmt_cny(value: Optional[float]) -> str:
    return f"{value:.1f}" if value is not None else "—"


def format_plan(snap: Dict[str, Any], plan: Dict[str, Any]) -> str:
    """完整计划（Markdown），用于定时摘要与 --once 输出。"""
    ts = snap["ts"].astimezone(BJ).strftime("%m-%d %H:%M")
    lines = [f"**XAUUSD {snap['xauusd']:.2f}**（{snap.get('change_pct', 0):+.2f}%）｜ {ts} 北京时间"]
    est = snap.get("bank_est") or {}
    if est:
        lines.append("｜".join(f"{BANK_NAMES[b]} ≈ {p:.2f}" for b, p in est.items()) + " 元/克")
    sig = snap.get("signals") or {}
    if sig:
        lines.append("信号：" + " ".join(f"{k}{v}" for k, v in sig.items()))
    if not plan.get("ok"):
        lines.append(f"⚠️ {plan.get('reason')}")
        return "\n\n".join(lines)

    lines.append(f"**结论**：{plan['summary']}\n\n**门控**：{plan['gate_text']}")
    lines.append(
        f"三轴：M {plan['axis_m']['axis']}（{plan['axis_m'].get('note', '')}）｜"
        f"L {plan['axis_l']['axis']}（{plan['axis_l']['note']}）｜S {plan['axis_s']['axis']}（{plan['axis_s']['note']}）"
    )
    rows = [(lv["price"], f"R{i + 1}", fmt_cny(lv["cny"]), "、".join(lv["reasons"])) for i, lv in enumerate(plan["resistance"])]
    rows += [(lv["price"], f"S{i + 1}", fmt_cny(lv["cny"]), "、".join(lv["reasons"])) for i, lv in enumerate(plan["support"])]
    rows += [(plan["pivot"], "P 盘中轴心", "—", "(高+低)/2"), (snap["xauusd"], "▶ 现价", fmt_cny((snap.get("bank_est") or {}).get(plan["ratio_bank"])), "")]
    table = ["| 点位 | XAUUSD | 元/克 | 依据 |", "|---|---|---|---|"]
    table += [f"| {label} | {price:.1f} | {cny} | {why} |" for price, label, cny, why in sorted(rows, key=lambda r: -r[0])]
    lines.append("\n".join(table))
    buy = plan.get("buy")
    if buy and buy.get("t1"):
        rr2 = f"{buy['rr2']:.2f}" if buy.get("rr2") is not None else "—"
        lines.append(
            f"**低吸计划（{buy['level']}）**：入场 {buy['zone'][0]:.0f}–{buy['zone'][1]:.0f}"
            f"（{fmt_cny((buy.get('zone_cny') or [None])[0])}–{fmt_cny((buy.get('zone_cny') or [None, None])[1])} 元/克）"
            f"｜止损 {buy['stop']:.0f}（{fmt_cny(buy.get('stop_cny'))}）"
            f"｜T1 {buy['t1']:.0f}（{fmt_cny(buy.get('t1_cny'))}）R:R {buy['rr1']:.2f}"
            f"｜T2 {buy['t2'] or 0:.0f}（{fmt_cny(buy.get('t2_cny'))}）R:R {rr2}"
            + (f"｜保本卖价 {buy['breakeven_cny']:.2f}（含卖出手续费）" if buy.get("breakeven_cny") else "")
        )
    lines.append(f"持仓减仓区：{plan['reduce_zone'][0]:.0f}–{plan['reduce_zone'][1]:.0f}")
    lines.append("> 程序自动生成，仅供参考，不构成投资建议")
    return "\n\n".join(lines)


class AlertEngine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def cooled_down(self, key: str, minutes: Optional[float] = None) -> bool:
        last = self.store.last_alert_time(key)
        window = timedelta(minutes=minutes if minutes is not None else self.settings.move_cooldown_minutes)
        return last is None or utc_now() - last >= window

    def check(self, snap: Dict[str, Any]) -> List[Dict[str, str]]:
        if not snap.get("market_open") or snap.get("xauusd") is None:
            return []
        events: List[Dict[str, str]] = []
        price = snap["xauusd"]

        # 1. 短时异动（默认 30 分钟 ±0.6%）
        window = int(self.settings.move_window_minutes)
        prev = self.store.price_minutes_ago(window)
        if prev:
            pct = (price - prev) / prev * 100
            if abs(pct) >= self.settings.move_pct:
                events.append({"key": f"move:{'up' if pct > 0 else 'down'}",
                               "title": f"⚡{window}分钟{'急涨' if pct > 0 else '急跌'} {pct:+.2f}%",
                               "body": f"XAUUSD {window} 分钟内 {prev:.2f} → {price:.2f}（{pct:+.2f}%），达到异动复评阈值，交由已开启模型调度；不代表买卖建议"})

        return [e for e in events if self.cooled_down(e["key"])]
