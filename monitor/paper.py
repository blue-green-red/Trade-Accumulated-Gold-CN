"""模拟盘：按交易计划在入场区虚拟买入，止损/目标在入场时冻结。

成本模型（与银行交易规则一致）：按京东接口直取报价买入、买入 0 手续费；
卖出到手 = 卖出价 × (1 − 卖出手续费率) − 价差，与 gold_calc.risk_reward_long 口径一致。T1 平一半并把止损移到保本卖价（含手续费），T2 平剩余。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

import gold_calc as gc

from .config import BANK_NAMES, Settings
from .store import Store, iso, utc_now
from .controls import identity, view_active
from .execution import execution_quote, EXECUTION_NAME


class PaperBook:
    def __init__(self, settings: Settings, store: Store, strategy: str, label: str):
        if strategy == "short":
            raise ValueError("独立规则短线账户已移除")
        self.settings = settings
        self.store = store
        self.bank = settings.paper_bank
        self.strategy = strategy
        self.label = label
        self.analyst = identity(strategy)[0]
        self.store.conn.execute("INSERT OR IGNORE INTO paper_accounts VALUES(?,?,?)",
                                (strategy, settings.account_budget, iso(utc_now())))
        self.store.conn.commit()

    def on_tick(self, snap: Dict[str, Any], plan: Dict[str, Any]) -> List[Dict[str, str]]:
        quote = execution_quote(snap)
        price = quote["price"] if quote else None
        if price is None or not snap.get("market_open"):
            return []
        if self.analyst and not view_active(self.store.conn, self.analyst, plan.get("decision")):
            plan = {"ok": False}
        trade = self.store.open_trade(self.bank, self.strategy)
        events = []
        if trade is not None:
            if plan.get("force_exit"):   # AI 主导账户：AI 看跌时提前离场
                events = self._exit_now(trade, price, "AI 看跌离场", plan.get("decision"), quote)
            else:
                events = self._manage(trade, price, quote)
        else:
            events = self._maybe_open(snap, plan, price)
        balance = self.store.paper_balance(self.bank, self.strategy, price, self.settings.sell_fee_rate, self.settings.spread_cny)
        self.store.conn.execute("INSERT OR REPLACE INTO paper_equity VALUES(?,?,?)",
                                (self.strategy, iso(snap["ts"]), balance["value"]))
        self.store.conn.commit()
        return events

    # ── 开仓 ─────────────────────────────────────────────────────────────
    def _maybe_open(self, snap: Dict[str, Any], plan: Dict[str, Any], price: float) -> List[Dict[str, str]]:
        buy = plan.get("buy") if plan.get("ok") else None
        if not plan.get("ok") or plan.get("action") != "buy_on_dip" or not buy or not buy.get("valid") or "zone_cny" not in buy:
            return []
        if plan.get("ratio_bank") != self.bank or (plan["gate"] == "block" and not plan.get("ignore_gate")):
            return []
        low, high = buy["zone_cny"]
        if not (low <= price <= high):
            return []
        rr = gc.risk_reward_long(price, buy["stop_cny"], buy["t1_cny"],
                                 sell_fee_rate=self.settings.sell_fee_rate, spread=self.settings.spread_cny)
        if rr is None or rr < gc.MIN_RR or price <= buy["stop_cny"]:
            return []
        last = self.store.last_closed_trade(self.bank, self.strategy)
        if last is not None and last["closed_at"]:
            closed_at = datetime.fromisoformat(last["closed_at"])
            if utc_now() - closed_at < timedelta(minutes=self.settings.reentry_cooldown_minutes):
                return []

        # 原版：门控矛盾时半仓；AI 主导：按 AI 把握大小 0.5~1.5 倍
        grams = self.settings.paper_grams * plan.get("size_mult", 1.0) * (
            0.5 if plan["gate"] == "light" and not plan.get("ignore_gate") else 1.0)
        self.store.conn.execute("BEGIN IMMEDIATE")
        if execution_quote(snap) is None:
            self.store.conn.rollback()
            return []
        if self.analyst and not view_active(self.store.conn, self.analyst, plan.get("decision")):
            self.store.conn.rollback()
            return []
        if self.store.open_trade(self.bank, self.strategy):
            self.store.conn.rollback()
            return []
        balance = self.store.paper_balance(self.bank, self.strategy, price, self.settings.sell_fee_rate, self.settings.spread_cny)
        grams = min(grams, max(0, balance["cash"]) / price)
        if grams <= 1e-9:
            self.store.conn.rollback()
            return []
        plan = dict(plan, execution_rr=rr, version="independent-v1", analyst=self.analyst,
                    execution_quote=execution_quote(snap))
        trade_id = self.store.insert_trade(
            fill={"side": "buy", "grams": grams, "price": price, "fee": 0, "quote": execution_quote(snap), "reason": "计划入场"},
            bank=self.bank, strategy=self.strategy, status="open", opened_at=iso(utc_now()), entry_usd=snap["xauusd"],
            entry_cny=price, grams=grams, open_grams=grams,
            stop_cny=buy["stop_cny"], t1_cny=buy["t1_cny"], t2_cny=buy["t2_cny"] or buy["t1_cny"],
            plan_json=json.dumps(plan, ensure_ascii=False, default=str),
        )
        name = EXECUTION_NAME
        return [{
            "key": f"paper:{self.strategy}:open:{iso(utc_now())}",
            "title": f"📗[{self.label}]模拟买入 {price:.2f}",
            "body": (f"**模拟买入** {name} {grams:g} 克 @ {price:.2f} 元/克（XAUUSD {snap['xauusd']:.2f}）\n\n"
                     f"- 止损 {buy['stop_cny']:.2f} ｜ T1 {buy['t1_cny']:.2f} ｜ T2 {(buy['t2_cny'] or buy['t1_cny']):.2f}\n"
                     f"- 保本价 {buy['breakeven_cny']:.2f}（卖出手续费 {self.settings.sell_fee_rate:.1%}）｜ R:R {buy['rr1']:.2f} ｜ 门控：{plan['gate_text']}\n"
                     f"- 依据：{plan['summary']}"),
        }]

    # ── 持仓管理 ─────────────────────────────────────────────────────────
    def _exit_now(self, trade, price: float, reason: str, decision=None, quote=None) -> List[Dict[str, str]]:
        per_gram = gc.net_sell_proceeds(price, self.settings.sell_fee_rate, self.settings.spread_cny) - trade["entry_cny"]
        realized = trade["realized_cny"] + per_gram * trade["open_grams"]
        self._close(trade["id"], realized, reason, price, decision, quote, trade["open_grams"])
        return [self._close_event(trade, price, realized, reason, EXECUTION_NAME)]

    def _manage(self, trade, price: float, quote=None) -> List[Dict[str, str]]:
        name = EXECUTION_NAME
        per_gram = gc.net_sell_proceeds(price, self.settings.sell_fee_rate, self.settings.spread_cny) - trade["entry_cny"]
        events: List[Dict[str, str]] = []

        if price <= trade["stop_cny"]:
            realized = trade["realized_cny"] + per_gram * trade["open_grams"]
            reason = "保本止损" if trade["t1_hit"] else "止损"
            self._close(trade["id"], realized, reason, price, quote=quote, grams=trade["open_grams"])
            events.append(self._close_event(trade, price, realized, reason, name))
        elif not trade["t1_hit"] and price >= trade["t1_cny"]:
            half = trade["open_grams"] / 2
            realized = trade["realized_cny"] + per_gram * half
            breakeven = round(gc.breakeven_price(trade["entry_cny"], self.settings.sell_fee_rate, self.settings.spread_cny), 2)
            self.store.update_trade(trade["id"], t1_hit=1, open_grams=trade["open_grams"] - half,
                                    realized_cny=realized, stop_cny=breakeven,
                                    fill=self._sell_fill(half, price, quote, "T1止盈"))
            events.append({
                "key": f"paper:{self.strategy}:t1:{trade['id']}",
                "title": f"📘[{self.label}]T1止盈 {price:.2f}",
                "body": (f"**T1 到达**，平一半 {half:g} 克 @ {price:.2f}，本笔已实现 {realized:+.2f} 元；"
                         f"剩余止损上移到保本卖价 {breakeven:.2f}（含手续费），等 T2 {trade['t2_cny']:.2f}"),
            })
        elif trade["t1_hit"] and price >= trade["t2_cny"]:
            realized = trade["realized_cny"] + per_gram * trade["open_grams"]
            self._close(trade["id"], realized, "T2止盈", price, quote=quote, grams=trade["open_grams"])
            events.append(self._close_event(trade, price, realized, "T2止盈", name))
        return events

    def _sell_fill(self, grams, price, quote, reason):
        return {"side": "sell", "grams": grams, "price": price,
                "fee": grams * price * self.settings.sell_fee_rate, "quote": quote, "reason": reason}

    def _close(self, trade_id: int, realized: float, reason: str, price: float, decision=None, quote=None, grams=None) -> None:
        self.store.update_trade(trade_id, fill=self._sell_fill(grams, price, quote, reason) if grams is not None else None,
                                status="closed", open_grams=0, closed_at=iso(utc_now()),
                                realized_cny=round(realized, 2), close_reason=reason, exit_cny=price,
                                close_decision_json=json.dumps({"version": "independent-v1", "analyst": self.analyst,
                                                                "view": decision, "reason": reason, "execution_quote": quote}, ensure_ascii=False))

    def _close_event(self, trade, price: float, realized: float, reason: str, name: str) -> Dict[str, str]:
        stats = self.store.trade_stats(self.bank, self.strategy)
        win_rate = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "—"
        return {
            "key": f"paper:{self.strategy}:close:{trade['id']}",
            "title": f"📕[{self.label}]平仓 {reason}",
            "body": (f"**{reason}** 平仓 @ {price:.2f}，本笔 {realized:+.2f} 元（入场 {trade['entry_cny']:.2f}）\n\n"
                     f"累计：{stats['trades']} 笔，胜率 {win_rate}，总盈亏 {stats['total_pnl_cny']:+.2f} 元"),
        }
