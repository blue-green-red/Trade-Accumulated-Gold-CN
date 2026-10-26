"""AI 自主六账户模拟盘：三周期各满仓/空仓起点，一次判断六个决策，独立审计执行。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator

from .controls import MODELS, active, control, view_active
from .execution import execution_quote
from .store import utc_now
from .ai_events import enqueue

VERSION = "autonomous-six-v2"
PERIODS = {"short": ("短线", "intraday"), "medium": ("中线", "swing"), "long": ("长线", "position")}
LABELS = {f"{period}_{start}": f"{label} · 初始{position}" for period,(label,_) in PERIODS.items() for start,position in (("full","满仓"),("empty","空仓"))}
SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_portfolios (
 key TEXT PRIMARY KEY, model TEXT NOT NULL, start_mode TEXT NOT NULL, budget REAL NOT NULL,
 cash REAL NOT NULL, grams REAL NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
 started_at TEXT, initial_price REAL, revision INTEGER NOT NULL DEFAULT 0,
 stop_price REAL, target_price REAL, last_view_id INTEGER NOT NULL DEFAULT 0,
 realized_pnl REAL NOT NULL DEFAULT 0, peak REAL NOT NULL, max_drawdown REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_plans (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, view_id INTEGER NOT NULL, generation INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decision_json TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT, UNIQUE(account, view_id)
);
CREATE TABLE IF NOT EXISTS ai_portfolio_fills (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, ts TEXT NOT NULL, side TEXT NOT NULL,
 grams REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL, pnl REAL NOT NULL,
 reason TEXT NOT NULL, quote_json TEXT NOT NULL, view_id INTEGER, plan_id INTEGER
);
CREATE TABLE IF NOT EXISTS ai_portfolio_cycles (
 id INTEGER PRIMARY KEY, account TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT,
 invested REAL NOT NULL DEFAULT 0, proceeds REAL NOT NULL DEFAULT 0, pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_plan_active ON ai_plans(account,status,id);
CREATE INDEX IF NOT EXISTS idx_ai_fill_account ON ai_portfolio_fills(account,id);
CREATE INDEX IF NOT EXISTS idx_ai_cycle_account ON ai_portfolio_cycles(account,closed_at);
CREATE TABLE IF NOT EXISTS ai_portfolio_model_stats (model TEXT PRIMARY KEY, peak REAL NOT NULL, max_drawdown REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ai_portfolio_equity (
 account TEXT NOT NULL, ts TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY(account,ts)
);
"""


class AccountDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    horizon: Literal["intraday", "swing", "position"] = Field(description="短线、波段、长线；是计划属性，不是独立账户")
    action: Literal["buy", "sell", "hold", "wait", "cancel"]
    execution: Literal["market", "limit"]
    amount_cny: Optional[float] = Field(default=None, ge=0, description="买入使用的人民币金额；其他动作填 null")
    grams: Optional[float] = Field(default=None, ge=0, description="卖出克数；其他动作填 null")
    limit_price_cny: Optional[float] = Field(default=None, gt=0, description="限价，元/克；市价填 null")
    min_execution_price_cny: Optional[float] = Field(default=None, gt=0, description="买卖可接受成交价下限，元/克；买卖必填，其他动作null")
    max_execution_price_cny: Optional[float] = Field(default=None, gt=0, description="买卖可接受成交价上限，元/克；买卖必填，其他动作null")
    stop_loss_cny: Optional[float] = Field(default=None, gt=0, description="持仓止损价，元/克；null 保留现有保护")
    take_profit_cny: Optional[float] = Field(default=None, gt=0, description="持仓全部止盈价，元/克；null 保留现有目标")
    clear_stop_loss: bool = Field(default=False, description="主动取消已有止损；与 stop_loss_cny 同时设置无效")
    clear_take_profit: bool = Field(default=False, description="主动取消已有止盈目标")
    valid_for_minutes: int = Field(ge=1, le=1440, description="从共同快照起算的计划有效分钟数，最长24小时")
    reason: str = Field(min_length=1, max_length=600)
    invalidation: str = Field(max_length=400, description="判断失效的理由；程序执行价格条件、保护价和到期，语义变化交给下次分析")

    @model_validator(mode="after")
    def execution_range(self):
        if self.action in ("buy","sell"):
            low,high=self.min_execution_price_cny,self.max_execution_price_cny
            if low is None or high is None or low>high:
                raise ValueError("买卖必须指定有效的可接受成交价格区间")
            if self.execution=="limit" and self.limit_price_cny is not None:
                if (self.action=="buy" and self.limit_price_cny<low) or (self.action=="sell" and self.limit_price_cny>high):
                    raise ValueError("限价条件与可接受成交价格区间没有交集")
        return self


class PortfolioDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_full: AccountDecision
    short_empty: AccountDecision
    medium_full: AccountDecision
    medium_empty: AccountDecision
    long_full: AccountDecision
    long_empty: AccountDecision


TRADING_PROMPT = """你同时负责六个独立积存金模拟账户，每个账户初始10万元，资金不可混用：
short_full/short_empty：短线，分别初始满仓/空仓；medium_full/medium_empty：中线；long_full/long_empty：长线。
trading_accounts 含你自己的六个账户：现金、克数、成本、净值、版本、保护价、未完成计划、最近成交与最近拒绝。
必须在一次最终输出 account_decisions 中同时提交六份决策。各账户买卖独立，可以采取不同操作，不得跨账户转账。
由你直接决定买入/卖出/持有/观望/撤单、数量、限价、止损止盈及有效期；概率用于评估，不是交易门槛。
short_* 的 horizon 必须 intraday（数小时至3交易日）；medium_* 必须 swing（数日至数周）；long_* 必须 position（数周至数月）。
未发生变化的账户可以用简短理由持有/观望，不必重复长篇解释。
围绕对应周期制定计划，考虑已有仓位和卖出费用；长线可以保持不动，短线没有扣费后机会也可以观望。
buy 的 amount_cny 是支出金额；sell 的 grams 是卖出克数。其他动作数量填 null。
buy/sell 必须指定 min_execution_price_cny 和 max_execution_price_cny，定义本决策可接受的京东成交价格区间（含边界）；其他动作填null。区间由你按周期与判断设置，禁止依赖市价无限追涨杀跌。达到成交条件时若越界，会拒绝委托并复查；尚未达到限价条件时继续等待。限价条件须与区间有交集。
市价使用最新有效京东报价；限价买入等报价<=限价，限价卖出等报价>=限价。horizon 不创建额外账户。
hold/wait 不成交，可更新持仓保护；cancel 撤销未成交委托，也可同时更新或撤销持仓保护。新决策替换该账户旧未成交委托。null 保护价保留现有值；如需取消止损，clear_stop_loss=true 且 stop_loss_cny=null；取消止盈用 clear_take_profit=true 且 take_profit_cny=null。
禁止借款、做空、跨账户使用资金；可以买满、分批买卖或清仓。止损止盈完全由你决定，可不设、收紧、放宽或显式撤销，没有固定亏损阈值。初始满仓不预设保护，已有保护不会被程序自动删除；未设置保护时不会自动平仓。需要撤销保护请显式提交对应 clear 字段，单纯 null 保留原设置。是否设置保护及风险敞口由你按周期、成本和市场判断决定，在 reason 中说明选择。
卖出费用以输入 constraints 为准，考虑手续费后的收益。不要机械复制六份决策，也不要为了交易而交易。
委托有效期从 snapshot_at 起算，最长24小时；到期仅撤未成交委托，不强制平仓，保护单持续到持仓退出或新决策更新。
分析期间京东报价或XAU/USD相对输入变动达到 constraints.inflight_max_move_pct 时，本轮决策失效并复查，不会中途反复取消模型；既有持仓保护继续执行。账户版本或模型开关变化时程序会拒绝过时指令。参考最近拒绝原因，避免重复无效委托。
给出各账户行为依据和失效条件；程序执行明确价格条件和保护，语义变化由后续分析重新判断。
日线趋势若来自期货代理，仅作趋势参考，不当作京东实际价格。"""


def enabled(settings):
    return settings.ai_trading_mode == "six_account"


def keys(settings):
    return {f"{model}:{mode}": f"{model.title()} · {label}" for model in settings.ai_analysts if model in MODELS
            for mode, label in LABELS.items()}


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_portfolios'").fetchone() is not None


def net_price(settings, price):
    return price * (1 - settings.sell_fee_rate) - settings.spread_cny


def summaries(conn, settings, price):
    if not exists(conn):
        return []
    rows = []
    for raw in conn.execute("SELECT * FROM ai_portfolios ORDER BY model,start_mode"):
        a = dict(raw)
        value = a["cash"] + a["grams"] * net_price(settings, price) if price else a["cash"] if a["grams"] < 1e-9 else None
        avg = a["cost"] / a["grams"] if a["grams"] > 1e-9 else None
        plan = conn.execute("SELECT * FROM ai_plans WHERE account=? ORDER BY id DESC LIMIT 1", (a["key"],)).fetchone()
        pnls = [r[0] for r in conn.execute("SELECT pnl FROM ai_portfolio_cycles WHERE account=? AND closed_at IS NOT NULL", (a["key"],))]
        from .performance import pnl_stats
        fills = conn.execute("SELECT COUNT(*),COALESCE(SUM(fee),0) FROM ai_portfolio_fills WHERE account=?", (a["key"],)).fetchone()
        rows.append({**a, **pnl_stats(pnls), "name": keys(settings).get(a["key"], a["key"]), "analyst": a["model"],
            "strategy": a["start_mode"], "started": a["started_at"] is not None, "requires_ai": True,
            "horizon": LABELS[a["start_mode"]], "description": "AI 自主决定交易，三周期满仓/空仓独立记账",
            "realized_pnl": a["realized_pnl"], "value": value, "return": value / a["budget"] - 1 if value is not None and a["started_at"] else None, "win_rate_applicable": True,
            "avg_cost": avg, "unrealized_pnl": a["grams"] * net_price(settings, price) - a["cost"] if price else None,
            "position_pct": a["grams"] * price / (a["cash"] + a["grams"] * price) * 100 if price and a["cash"] + a["grams"] * price > 0 else 0,
            "paused": not active(conn, a["model"]), "fills": fills[0], "fees": fills[1],
            "plan": {**{k:plan[k] for k in plan.keys() if k!="decision_json"}, "decision": json.loads(plan["decision_json"])} if plan else None})
    return rows


class AutonomousPortfolio:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        if not math.isfinite(settings.account_budget) or settings.account_budget <= 0:
            raise ValueError("账户本金配置无效")
        store.conn.executescript(SCHEMA)
        from .ai import ensure_schema
        ensure_schema(store)
        for key in keys(settings):
            model, mode = key.split(":")
            store.conn.execute("INSERT OR IGNORE INTO ai_portfolios(key,model,start_mode,budget,cash,peak) VALUES(?,?,?,?,?,?)",
                               (key, model, mode, settings.account_budget, settings.account_budget, settings.account_budget))
        store.conn.commit()
        if not store.get_state("autonomous_since"):
            store.set_state("autonomous_since", utc_now().isoformat())

    def input(self, model, quote, store=None):
        conn = (store or self.store).conn
        rows = [a for a in summaries(conn, self.settings, quote["price"] if quote else None) if a["model"] == model]
        if len(rows) != 6 or not all(a["started"] for a in rows):
            raise ValueError("六账户尚未完成初始化，等待有效报价和模型开启")
        accounts = {}
        for a in rows:
            recent = conn.execute("SELECT ts,side,grams,price,fee,reason FROM ai_portfolio_fills WHERE account=? ORDER BY id DESC LIMIT 6", (a["key"],)).fetchall()
            accounts[a["start_mode"]] = {k:a[k] for k in ("key", "revision", "budget", "cash", "grams", "avg_cost", "value", "unrealized_pnl", "realized_pnl", "position_pct", "stop_price", "target_price")}
            accounts[a["start_mode"]].update({"last_plan": {k:a["plan"][k] for k in ("status","reason","decision","expires_at")} if a["plan"] else None, "recent_fills": [dict(r) for r in recent]})
        return {"policy_version":VERSION, "reference_quote":quote, "accounts": accounts, "control_generation": control(conn,model)["generation"], "constraints": {"sell_fee_rate": self.settings.sell_fee_rate,
            "spread_cny": self.settings.spread_cny, "max_position_pct": 100, "protection_policy": "model_discretion", "stop_loss_required": False, "can_clear_stop_loss": True,
            "inflight_max_move_pct":self.settings.ai_inflight_max_move_pct, "max_input_age_minutes": self.settings.ai_decision_max_age_minutes, "max_plan_minutes": 1440,
            "currency": "CNY", "quantity_unit": "gram", "shorting": False}}

    def _fill(self, a, side, grams, quote, reason, view_id=None, plan_id=None):
        c, s = self.store.conn, self.settings
        if execution_quote({"execution_quote":quote}) is None:
            raise ValueError("成交报价已过期")
        price, stamp = quote["price"], utc_now().isoformat(timespec="microseconds")
        if grams <= 0 or not math.isfinite(grams):
            raise ValueError("成交克数无效")
        if side == "buy":
            amount = grams * price
            if amount > a["cash"] + 1e-7:
                raise ValueError("可用现金不足")
            if a["grams"] <= 1e-9:
                c.execute("INSERT INTO ai_portfolio_cycles(account,opened_at) VALUES(?,?)", (a["key"], stamp))
            fee, pnl = 0.0, 0.0
            a["cash"] = max(0, a["cash"] - amount)
            a["grams"] += grams
            a["cost"] += amount
            c.execute("UPDATE ai_portfolio_cycles SET invested=invested+? WHERE account=? AND closed_at IS NULL", (amount, a["key"]))
        else:
            if grams > a["grams"] + 1e-7:
                raise ValueError("持仓克数不足")
            grams = min(grams, a["grams"])
            fee = grams * price * s.sell_fee_rate
            proceeds = grams * net_price(s, price)
            allocated = a["cost"] * grams / a["grams"]
            pnl = proceeds - allocated
            a["cash"] += proceeds
            a["grams"] -= grams
            a["cost"] = max(0, a["cost"] - allocated)
            a["realized_pnl"] += pnl
            c.execute("UPDATE ai_portfolio_cycles SET proceeds=proceeds+? WHERE account=? AND closed_at IS NULL", (proceeds, a["key"]))
            if a["grams"] < 1e-8:
                a["grams"], a["cost"], a["stop_price"], a["target_price"] = 0.0, 0.0, None, None
                c.execute("UPDATE ai_portfolio_cycles SET closed_at=?,pnl=proceeds-invested WHERE account=? AND closed_at IS NULL", (stamp, a["key"]))
        a["revision"] += 1
        cursor = c.execute("INSERT INTO ai_portfolio_fills(account,ts,side,grams,price,fee,pnl,reason,quote_json,view_id,plan_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (a["key"],stamp,side,grams,price,fee,pnl,reason,json.dumps(quote,ensure_ascii=False),view_id,plan_id))
        return cursor.lastrowid

    def _save(self, a):
        fields = ("cash","grams","cost","started_at","initial_price","revision","stop_price","target_price","last_view_id","realized_pnl","peak","max_drawdown")
        self.store.conn.execute("UPDATE ai_portfolios SET " + ",".join(f"{k}=?" for k in fields) + " WHERE key=?", [a[k] for k in fields]+[a["key"]])

    def _reject(self, plan_id, reason):
        self.store.conn.execute("UPDATE ai_plans SET status='rejected',reason=? WHERE id=?", (reason,plan_id))

    def _validate(self, a, d, quote, check_range=True):
        AccountDecision.model_validate(d)
        price = quote["price"]
        if d["execution"] == "limit" and not d.get("limit_price_cny"):
            raise ValueError("限价委托缺少限价")
        if d["horizon"] != PERIODS[a["start_mode"].split("_")[0]][1]:
            raise ValueError("决策周期与账户不一致")
        action = d["action"]
        if check_range and action in ("buy","sell") and not d["min_execution_price_cny"] <= price <= d["max_execution_price_cny"]:
            raise ValueError("最新京东报价超出AI可接受成交价格区间")
        if action == "buy":
            amount = d.get("amount_cny")
            if amount is None or amount <= 0 or amount > a["cash"] + 1e-7:
                raise ValueError("买入金额必须为正且不得超过本账户现金")
            if d.get("grams") not in (None,0):
                raise ValueError("买入应指定金额，不同时指定卖出克数")
            stop = d.get("stop_loss_cny")
            entry = price if d["execution"] == "market" else d["limit_price_cny"]
            if stop is not None and stop >= entry:
                raise ValueError("买入止损须低于入场价")
        elif action == "sell":
            quantity = d.get("grams")
            if quantity is None or quantity <= 0 or quantity > a["grams"] + 1e-7:
                raise ValueError("卖出克数必须为正且不得超过本账户持仓")
            if d.get("amount_cny") not in (None,0):
                raise ValueError("卖出应指定克数，不同时指定买入金额")
        elif d.get("amount_cny") not in (None,0) or d.get("grams") not in (None,0):
            raise ValueError("持有、等待、撤单不能附带成交数量")
        if d["execution"] == "limit" and not d.get("limit_price_cny"):
            raise ValueError("限价委托缺少限价")
        stop, target = d.get("stop_loss_cny"), d.get("take_profit_cny")
        if d.get("clear_stop_loss") and stop is not None:
            raise ValueError("取消止损不能同时设置新止损")
        if d.get("clear_take_profit") and target is not None:
            raise ValueError("取消止盈不能同时设置新止盈")
        if a["grams"] > 1e-9 and action != "buy":
            if stop is not None and stop >= price:
                raise ValueError("持仓止损价必须低于当前报价")
        entry = price if d["execution"] == "market" else d.get("limit_price_cny", price)
        if target is not None and target <= (entry if action == "buy" else price):
            raise ValueError("止盈价必须高于当前或计划入场价格")

    def _accept(self, a, row, data, quote):
        c = self.store.conn
        d = data["account_decisions"][a["start_mode"]]
        snapshot = datetime.fromisoformat(data["_snapshot_at"])
        expires = snapshot + timedelta(minutes=d["valid_for_minutes"])
        cur = c.execute("INSERT OR IGNORE INTO ai_plans(account,view_id,generation,created_at,expires_at,decision_json,status) VALUES(?,?,?,?,?,?,?)",
            (a["key"],row["rowid"],data["_control_generation"],utc_now().isoformat(),expires.isoformat(),json.dumps(d,ensure_ascii=False),"pending"))
        a["last_view_id"] = row["rowid"]
        if not cur.rowcount:
            return
        plan_id = cur.lastrowid
        input_state = (data.get("_account_inputs") or {}).get(a["start_mode"],{})
        try:
            from .price_guard import check,read
            guard=dict(data.get("_price_guard") or {})
            if guard:
                check(guard,{**read(c,"ai_live_market"),"jd":quote["price"]})
                if guard.get("invalidated_at"):raise ValueError(guard["reason"])
            if input_state.get("revision") != a["revision"]:
                raise ValueError("分析期间账户已变化，请基于最新持仓重做决策")
            if not 0 <= (utc_now()-snapshot).total_seconds() <= self.settings.ai_decision_max_age_minutes*60:
                raise ValueError("共同输入已过期，不能接收新委托")
            if expires <= utc_now():
                raise ValueError("计划已经到期")
            ready=d["execution"]=="market" or (quote["price"]<=d["limit_price_cny"] if d["action"]=="buy" else quote["price"]>=d["limit_price_cny"])
            self._validate(a,d,quote,check_range=ready)
        except (ValueError,TypeError,KeyError) as exc:
            self._reject(plan_id,str(exc))
            return
        c.execute("UPDATE ai_plans SET status='cancelled',reason='新决策替换旧委托' WHERE account=? AND status='pending' AND id<>?",(a["key"],plan_id))
        if a["grams"] > 1e-9 and d["action"] != "buy":
            a["stop_price"] = None if d.get("clear_stop_loss") else d.get("stop_loss_cny") or a["stop_price"]
            a["target_price"] = None if d.get("clear_take_profit") else d.get("take_profit_cny") or a["target_price"]
        if d["action"] == "cancel":
            c.execute("UPDATE ai_plans SET status='executed',reason='已撤销未成交委托，已更新或保留持仓保护' WHERE id=?",(plan_id,))
            return
        if d["action"] in ("hold","wait"):
            c.execute("UPDATE ai_plans SET status='executed',reason='已记录持有/观望及保护价' WHERE id=?",(plan_id,))

    def expire_plans(self):
        """Expiry is a clock transition; it must not depend on a tradable quote."""
        with self.store.conn:
            self.store.conn.execute("UPDATE ai_plans SET status='expired',reason='计划到期' WHERE status='pending' AND julianday(expires_at)<=julianday(?)",(utc_now().isoformat(),))

    def on_tick(self, snap):
        self.expire_plans()
        from .price_guard import observe,request_refresh
        with self.store.conn:
            self.store.conn.execute("BEGIN IMMEDIATE")
            invalid=observe(self.store.conn,snap,self.settings)
        for model,state in invalid:request_refresh(self.store.conn,model,state)
        quote = execution_quote(snap)
        if not quote or not snap.get("market_open"):
            return []
        c, emitted, refreshes = self.store.conn, [], []
        has_views = c.execute("SELECT 1 FROM sqlite_master WHERE name='ai_views'").fetchone() is not None
        batch_start = utc_now().isoformat()
        # 一个事务内更新账户、计划、成交和权益，开关也受同一写锁保护。
        with c:
            c.execute("BEGIN IMMEDIATE")
            if execution_quote(snap) is None:
                return []
            for raw in c.execute("SELECT * FROM ai_portfolios ORDER BY model,start_mode").fetchall():
                a = dict(raw)
                status = self.store.get_state("ai_status:"+a["model"],{})
                can_trade = a["model"] in self.settings.ai_analysts and active(c,a["model"]) and status.get("available",True)
                if not a["started_at"]:
                    if not can_trade:
                        continue
                    a["started_at"], a["initial_price"] = batch_start, quote["price"]
                    a["revision"] += 1
                    refreshes.append((a["model"],"portfolio-init:"+a["model"],"六账户初始化，按新账户提交决策"))
                    if a["start_mode"].endswith("_full"):
                        self._fill(a,"buy",a["budget"]/quote["price"],quote,"实验初始化：满仓起点")
                # 已有保护优先执行，与模型开关、预测和计划到期无关。
                if a["grams"] > 1e-9:
                    stop = a["stop_price"]
                    reason = "持仓止损" if stop is not None and quote["price"] <= stop else "持仓止盈" if a["target_price"] and quote["price"] >= a["target_price"] else None
                    if reason:
                        sold_grams = a["grams"]
                        fill_id = self._fill(a,"sell",sold_grams,quote,reason)
                        c.execute("UPDATE ai_plans SET status='cancelled',reason='持仓保护已触发' WHERE account=? AND status='pending'",(a["key"],))
                        refreshes.append((a["model"],f"portfolio-fill:{fill_id}",f"{LABELS[a['start_mode']]}{reason}"))
                        emitted.append({"notification_topic":a["model"]+":"+a["start_mode"].split("_")[0],"key":f"autonomous:{fill_id}","title":f"{a['model'].title()} · {LABELS[a['start_mode']]} {reason}","body":f"模拟卖出 {sold_grams:.4f} 克 @ 京东 {quote['price']:.2f} 元/克，保护已执行；卖出手续费 {sold_grams*quote['price']*self.settings.sell_fee_rate:.2f} 元"})
                latest = c.execute("SELECT rowid,* FROM ai_views WHERE analyst=? AND error IS NULL ORDER BY rowid DESC LIMIT 1",(a["model"],)).fetchone() if has_views else None
                if latest and latest["rowid"] > a["last_view_id"] and can_trade:
                    data = json.loads(latest["view_json"])
                    if data.get("account_decisions") and view_active(c,a["model"],data):
                        self._accept(a,latest,data,quote)
                        rejected = c.execute("SELECT id,reason FROM ai_plans WHERE account=? AND view_id=? AND status='rejected'",(a["key"],latest["rowid"])).fetchone()
                        if rejected:
                            guard=data.get("_price_guard") or {}
                            if rejected['reason'].startswith("分析期间行情异动") and guard.get("attempt"):
                                event_id=f"price-guard:{a['model']}:{guard['attempt']}"
                            else:
                                prefix="price-range" if "成交价格区间" in rejected['reason'] else "portfolio-reject"
                                event_id=f"{prefix}:{rejected['id']}"
                            refreshes.append((a["model"],event_id,f"{LABELS[a['start_mode']]}委托拒绝：{rejected['reason']}"))
                plans = c.execute("SELECT * FROM ai_plans WHERE account=? AND status='pending' ORDER BY id",(a["key"],)).fetchall()
                for plan in plans:
                    if not can_trade or not active(c,a["model"],plan["generation"]):
                        c.execute("UPDATE ai_plans SET status='cancelled',reason='模型关闭或开关版本已变化' WHERE id=?",(plan["id"],)); continue
                    if datetime.fromisoformat(plan["expires_at"]) <= utc_now():
                        c.execute("UPDATE ai_plans SET status='expired',reason='计划到期' WHERE id=?",(plan["id"],)); continue
                    d = json.loads(plan["decision_json"])
                    try:
                        AccountDecision.model_validate(d)
                    except ValueError as exc:
                        self._reject(plan["id"],str(exc))
                        refreshes.append((a["model"],f"price-range:{plan['id']}",str(exc)))
                        continue
                    if d["execution"] == "limit" and not (quote["price"] <= d["limit_price_cny"] if d["action"] == "buy" else quote["price"] >= d["limit_price_cny"]):
                        continue
                    if not d["min_execution_price_cny"] <= quote["price"] <= d["max_execution_price_cny"]:
                        reason="最新京东报价超出AI可接受成交价格区间"
                        self._reject(plan["id"],reason)
                        refreshes.append((a["model"],f"price-range:{plan['id']}",reason))
                        continue
                    try:
                        self._validate(a,d,quote)
                        if d["action"] == "buy" and d.get("stop_loss_cny") is not None and quote["price"] <= d["stop_loss_cny"]:
                            raise ValueError("当前报价已跌破计划止损，拒绝买入")
                        grams = d["amount_cny"]/quote["price"] if d["action"] == "buy" else d["grams"]
                        fill_id = self._fill(a,d["action"],grams,quote,d["reason"],plan["view_id"],plan["id"])
                        if a["grams"] > 1e-9:
                            a["stop_price"] = None if d.get("clear_stop_loss") else d.get("stop_loss_cny") or a["stop_price"]
                            a["target_price"] = None if d.get("clear_take_profit") else d.get("take_profit_cny") or a["target_price"]
                        c.execute("UPDATE ai_plans SET status='executed',reason='按AI委托成交' WHERE id=?",(plan["id"],))
                        emitted.append({"notification_topic":a["model"]+":"+a["start_mode"].split("_")[0],"key":f"autonomous:{fill_id}","title":f"{a['model'].title()} · {LABELS[a['start_mode']]} 模拟"+("买入" if d["action"]=="buy" else "卖出"),"body":f"{grams:.4f} 克 @ 京东 {quote['price']:.2f} 元/克；{d['reason']}"})
                    except (ValueError,TypeError) as exc:
                        self._reject(plan["id"],str(exc))
                value = a["cash"] + a["grams"]*net_price(self.settings,quote["price"])
                a["peak"] = max(a["peak"],value)
                a["max_drawdown"] = max(a["max_drawdown"],1-value/a["peak"])
                self._save(a)
                c.execute("INSERT OR REPLACE INTO ai_portfolio_equity VALUES(?,?,?)",(a["key"],utc_now().isoformat(),value))
            for model in self.settings.ai_analysts:
                totals = c.execute("SELECT SUM(cash),SUM(grams),SUM(budget) FROM ai_portfolios WHERE model=?",(model,)).fetchone()
                if totals[2]:
                    value = totals[0] + totals[1]*net_price(self.settings,quote["price"])
                    old = c.execute("SELECT peak,max_drawdown FROM ai_portfolio_model_stats WHERE model=?",(model,)).fetchone()
                    peak = max(old[0] if old else totals[2],value)
                    mdd = max(old[1] if old else 0,1-value/peak)
                    c.execute("INSERT INTO ai_portfolio_model_stats VALUES(?,?,?) ON CONFLICT(model) DO UPDATE SET peak=excluded.peak,max_drawdown=excluded.max_drawdown",(model,peak,mdd))
        for model,event_id,reason in refreshes:
            if active(c,model):
                enqueue(c,model,reason,0 if event_id.startswith("portfolio-init:") else 0 if event_id.startswith(("price-range:","price-guard:")) else 15,event_id,{"source":"账户风控"})
        return emitted
