"""AI 自主模拟盘：三档风险六账户短线实验，保留旧协议兼容，独立审计执行。"""
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

from .risk_profiles import MODE as RISK_MODE, MODES as RISK_MODES, LABELS as RISK_LABELS, profile, risk_state

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
    horizon: Literal["intraday", "swing", "position"] = Field(description="intraday兼容编码表示短线波段（数日至一两周），swing表示中线，position表示长线；不要求日内平仓")
    action: Literal["buy", "sell", "hold", "wait", "cancel"]
    execution: Literal["market", "limit"]
    amount_cny: Optional[float] = Field(default=None, ge=0, description="买入使用的人民币金额；其他动作填 null")
    grams: Optional[float] = Field(default=None, ge=0, description="卖出克数；其他动作填 null")
    limit_price_cny: Optional[float] = Field(default=None, gt=0, description="限价，元/克；市价填 null")
    min_execution_price_cny: Optional[float] = Field(default=None, gt=0, description="买卖可接受成交价下限，元/克；买卖必填，其他动作null")
    max_execution_price_cny: Optional[float] = Field(default=None, gt=0, description="买卖可接受成交价上限，元/克；买卖必填，其他动作null")
    risk_exit_cny: Optional[float] = Field(default=None, gt=0, description="风险实验买入必填：整仓风险退出参考价，元/克；仅用于计划风险预算，不自动设置止损")
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


class FourPortfolioDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_full: AccountDecision
    short_empty: AccountDecision
    medium_full: AccountDecision
    medium_empty: AccountDecision


class RiskPortfolioDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    low_full: AccountDecision
    low_empty: AccountDecision
    standard_full: AccountDecision
    standard_empty: AccountDecision
    high_full: AccountDecision
    high_empty: AccountDecision


class PortfolioDecisions(FourPortfolioDecisions):
    long_full: AccountDecision
    long_empty: AccountDecision


def policy_version(settings):
    if settings.ai_trading_mode==RISK_MODE:return "autonomous-risk-six-v1"
    return "autonomous-four-v1" if settings.ai_trading_mode=="four_account" else VERSION


def active_modes(settings):
    if settings.ai_trading_mode==RISK_MODE:return RISK_MODES
    return tuple(k for k in LABELS if settings.ai_trading_mode!="four_account" or not k.startswith("long_"))


def decision_type(settings):
    if settings.ai_trading_mode==RISK_MODE:return RiskPortfolioDecisions
    return FourPortfolioDecisions if settings.ai_trading_mode=="four_account" else PortfolioDecisions


def trading_prompt(settings):
    if settings.ai_trading_mode==RISK_MODE:return RISK_TRADING_PROMPT
    if settings.ai_trading_mode!="four_account": return TRADING_PROMPT
    return (TRADING_PROMPT.replace("六个", "四个").replace("六份", "四份")
        .replace("；long_full/long_empty：长线", "")
        .replace("；long_* 必须 position（数周至数月）", "")
        + "\n当前只操盘short_*与medium_*四户；长线账户已删除，不得为其生成新决策、恢复交易或转移其资金。")


RISK_TRADING_PROMPT = """操盘目标
你操盘六个独立模拟账户：low_full/low_empty、standard_full/standard_empty、high_full/high_empty，分别是低/标准/高风险的初始满仓/空仓起点。余额、持仓和历史以trading_accounts为准；一次提交全部六户account_decisions，禁止借款、做空、跨户调拨。
全部账户做数日至一两周的短线波段，horizon=intraday。目标是争取扣费后的净值增长，同时考虑回撤、成本与踏空代价。行动、仓位和保护由你决定；允许有依据地提前布局、逆势反弹或分批交易，不默认小仓，不要求所有信号转多，也不以研究概率或置信度限制交易。

风险预算
每户上限以risk_budget为准，不是必须用满的配额。买入必填risk_exit_cny，低于当前及计划入场价；将买入后全部持仓按该参考价退出并扣费，较当前净值的计划损失不得超过trade_risk_cny。实际止损更低时按更低价计算，加仓须包含原有仓位。风险参考不自动设置止损，实际损失可能超预算。
buy_paused=true时暂停新增买入，仍可持有、卖出或调整保护；回撤暂停不随反弹自动解除。初始满仓只是实验起点，不受单次买入预算约束。高风险账户不必比低风险账户更频繁交易或持仓更多。

决策依据
- 使用日线和数日至周级别结构、资金与事件，忽略30分钟/1小时/4小时信号。每周约两次买卖操作仅是节奏参考，买卖各计一次，不要求凑次数、日内平仓或三日退出；目标和期限适合波段，不无限延期。
- 空仓每轮在reason比较买入与等待：自选金额/仓位、入场E、目标T和风险退出R，说明依据并按卖费f计算T*(1-f)/E-1、R*(1-f)/E-1（另计价差）。允许明确标注假设的人民币换算情景，无需证明一定盈利；估计与历史事实分开，不编造价位、形态或胜率。无法估计须说明关键缺口；有持仓则比较持有、加减仓和退出。
- 结合last_plan及short_wait_review复查上次条件、当轮变化和等待代价。更改门槛说明新增证据或原条件不可验证的原因；旧条件未出现也重估机会。wait的invalidation写现有数据可验证的观察条件与建议复查时点；语义条件不自动下单，复查由程序调度。
- 有效京东执行报价和账户状态是交易必需信息；京东资金/挂单、ETF与央行配置流属于辅助证据，缺失不单独阻止交易。不能把未提供的完整京东日线或独立技术位设为前提。观望reason以“机会不合适：”（已知机会不值得参与）或“证据不足：”（关键判断或执行缺口）开头，并给依据。

执行语义
字段类型与范围按JSON Schema。buy/sell的可接受成交区间必须与限价条件有交集；市价用最新有效京东报价，限价买入等报价<=限价，卖出等报价>=限价，越出可接受区间会拒绝并复查。
新决策替换该户旧未成交委托；hold/wait不成交，cancel撤销旧委托，三者仍可更新保护。保护价null保留原设置，移除须显式clear_stop_loss/clear_take_profit=true，且对应价格null。实际止损止盈可以不设或自主调整，不设时程序不会按固定亏损自动平仓。
有效期从snapshot_at起算，到期只撤未成交委托，不强制平仓或撤销持仓保护。分析期间行情变动达到constraints.inflight_max_move_pct，或账户版本/模型开关变化，会拒绝过时决策；既有保护继续执行。根据最近拒绝原因修正计划。"""


TRADING_PROMPT = """你同时负责六个独立积存金模拟账户，每个账户初始10万元，资金不可混用：
short_full/short_empty：短线，分别初始满仓/空仓；medium_full/medium_empty：中线；long_full/long_empty：长线。
trading_accounts 含你自己的六个账户：现金、克数、成本、净值、版本、保护价、未完成计划、最近成交与最近拒绝。
必须在一次最终输出 account_decisions 中同时提交六份决策。各账户买卖独立，可以采取不同操作，不得跨账户转账。
目标是在各账户对应持仓周期内争取扣费后的账户净值增长，结合机会质量、不确定性与账户可承受损失自主选择行动和仓位。评价兼顾收益、回撤与交易成本，不以避免所有亏损、提高交易次数或维持某个胜率代替收益目标。买入与等待都需依据，等待不默认优于参与。
由你直接决定买入/卖出/持有/观望/撤单、数量、限价、止损止盈及有效期。仓位可轻、可中、可重，也可空仓或满仓，由当轮判断决定，不默认小仓，不设固定仓位或盈亏比。允许在不确定性下交易，有依据时提前参与、逆势反弹或分批建仓；目标与退出可以是明确标注假设的估计，不要求证明一定盈利。
short_* 的 horizon 必须 intraday（兼容编码，实际为数日至一两周的短线波段，不是日内交易）；medium_* 必须 swing（数日至数周）；long_* 必须 position（数周至数月）。研究概率用于预测评估，不作为交易门槛，不限定持仓期限。
短线决策要求（只适用于short_*）：
1. 以日线和数日至周级别的价格结构、资金与事件变化判断机会。short_*必须忽略这些指标：30分钟、1小时、4小时信号；不要求补齐小时K线、小时收盘或回踩确认；旧小时条件按新周期重审。日线卖出、未收复MA20或部分信号偏弱均非自动禁买条件，不要求全部信号转多。
2. 每周约两次买卖操作是参考，买入、卖出各计一次，不是必须完成的配额或风险退出上限。分析频率与交易频率分开，不要求日内平仓或三交易日退出。比较参与的收益、下跌损失与等待的踏空代价，自主选择行动；不强制试仓。
3. 空仓时每轮在reason简述一个买入情景并与等待比较：自主选择金额/仓位，以有效京东价或有依据的限价E，给目标T、风险退出R及依据，按输入卖费f计算T*(1-f)/E-1及R*(1-f)/E-1（另计价差）。情景是可调整的估计，不自动形成委托或保护；买入只须有合理机会与可接受风险，无需充分确认所有走势。若无法给数值，具体说明现有证据为何无法支持带假设估计，不仅写“依据不足”；不得伪造历史事实或胜率。有持仓时比较持有、加减仓和退出，不套用空仓模板。
4. wait的invalidation写现有有效数据可验证的买回观察条件与建议复查时点；语义条件不自动下单，复查仍按程序调度。结合short_wait_review与last_plan检查上次条件是否出现、当轮变化及等待代价；缺少历史信号不能声称已确认。更改条件须说明新增证据或旧条件不可验证的原因，不能逐轮抬高门槛。即使旧条件未出现，也重估参与价值。
5. 目标与持有期限由当轮证据决定，不固定900，不无限延期成长线。
所有活动账户共用的数据规则：
- 国际日线/周线、daily_trend_reference及有效事件用于方向与情景判断，京东有效报价用于实际成交和扣费，汇率用于人民币传导。期货开收盘代理、盘中指标和京东采样各有范围，不冒充完整京东日线或已确认形态。
- 入场与复查条件用现有有效输入或实际核实的信息判断。不得把未提供的完整京东人民币日线、独立人民币支撑验证或连续K线路径设为前提；不能逐轮重复等待缺失数据。
- 可用国际目标/退出假设、汇率及京东当前价做人民币情景测算，说明估计依据、换算误差及汇率/溢价风险。区分估计和已观测事实，允许估计不精确。
- 有效京东执行报价、账户状态属于执行必需信息，无效时不提交可执行买卖。京东资金/挂单、ETF和央行配置流是辅助证据，缺失只影响相应论据和信心，不单独阻止交易。中线重点判断数周持仓逻辑、参与价值与失效条件，配置流或短线趋势确认非统一前提。
- 观望reason以“机会不合适：”或“证据不足：”开头：前者说明已知机会不值得参与，后者指出影响判断或执行的关键缺口及为何现有数据无法替代。不得把未知当利空；不得为了避免等待而虚构目标或强制买入。
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
    return settings.ai_trading_mode in ("six_account","four_account","risk_six_account")


def keys(settings):
    return {f"{model}:{mode}": f"{model.title()} · {label}" for model in settings.ai_analysts if model in MODELS
            for mode, label in (RISK_LABELS if settings.ai_trading_mode==RISK_MODE else LABELS).items() if mode in active_modes(settings)}


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
        if a["key"] not in keys(settings): continue
        value = a["cash"] + a["grams"] * net_price(settings, price) if price else a["cash"] if a["grams"] < 1e-9 else None
        avg = a["cost"] / a["grams"] if a["grams"] > 1e-9 else None
        plan = conn.execute("SELECT * FROM ai_plans WHERE account=? ORDER BY id DESC LIMIT 1", (a["key"],)).fetchone()
        pnls = [r[0] for r in conn.execute("SELECT pnl FROM ai_portfolio_cycles WHERE account=? AND closed_at IS NOT NULL", (a["key"],))]
        from .performance import pnl_stats
        fills = conn.execute("SELECT COUNT(*),COALESCE(SUM(fee),0) FROM ai_portfolio_fills WHERE account=?", (a["key"],)).fetchone()
        rows.append({**a, **pnl_stats(pnls), "name": keys(settings).get(a["key"], a["key"]), "analyst": a["model"],
            "strategy": a["start_mode"], "started": a["started_at"] is not None, "requires_ai": True,
            "horizon": (RISK_LABELS | LABELS)[a["start_mode"]], "risk_budget":risk_state(a,price,lambda p:net_price(settings,p)) if settings.ai_trading_mode==RISK_MODE else None, "description": "短线波段：数日至一两周，以日线和波段价格结构为主；每周约两次操作为参考" if (a["start_mode"].startswith("short_") or settings.ai_trading_mode==RISK_MODE) else "AI 自主决定交易，三周期满仓/空仓独立记账",
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
        if settings.ai_trading_mode==RISK_MODE:
            from .risk_profiles import reset_experiment
            reset_experiment(store.conn)
        store.conn.executescript(SCHEMA)
        from .ai import ensure_schema
        ensure_schema(store)
        for key in keys(settings):
            model, mode = key.split(":")
            store.conn.execute("INSERT OR IGNORE INTO ai_portfolios(key,model,start_mode,budget,cash,peak) VALUES(?,?,?,?,?,?)",
                               (key, model, mode, settings.account_budget, settings.account_budget, settings.account_budget))
        store.conn.commit()
        if settings.ai_trading_mode=="four_account":
            from .retire_long import remove_long_accounts
            removed=remove_long_accounts(store.conn)
            if removed:
                for model in settings.ai_analysts:
                    if active(store.conn,model):enqueue(store.conn,model,"长线已删除，按四账户重新评估",0,event_id="four-account-mode:v1")
        if not store.get_state("autonomous_since"):
            store.set_state("autonomous_since", utc_now().isoformat())

    def input(self, model, quote, store=None, as_of=None):
        conn = (store or self.store).conn
        rows = [a for a in summaries(conn, self.settings, quote["price"] if quote else None) if a["model"] == model]
        if len(rows) != len(active_modes(self.settings)) or not all(a["started"] for a in rows):
            raise ValueError("活动账户尚未完成初始化，等待有效报价和模型开启")
        accounts = {}
        as_of = as_of or utc_now().isoformat()
        for a in rows:
            recent = conn.execute("SELECT ts,side,grams,price,fee,reason FROM ai_portfolio_fills WHERE account=? ORDER BY id DESC LIMIT 6", (a["key"],)).fetchall()
            accounts[a["start_mode"]] = {k:a[k] for k in ("key", "revision", "budget", "cash", "grams", "avg_cost", "value", "unrealized_pnl", "realized_pnl", "position_pct", "stop_price", "target_price")}
            if self.settings.ai_trading_mode==RISK_MODE:accounts[a["start_mode"]]["risk_budget"]=a["risk_budget"]
            accounts[a["start_mode"]].update({"last_plan": {k:a["plan"][k] for k in ("status","reason","decision","expires_at")} if a["plan"] else None, "recent_fills": [dict(r) for r in recent]})
            if (a["start_mode"].startswith("short_") or self.settings.ai_trading_mode==RISK_MODE):
                count=conn.execute("SELECT COUNT(*) FROM ai_portfolio_fills WHERE account=? "
                    "AND julianday(ts)>=julianday(?)-7 AND julianday(ts)<=julianday(?) "
                    "AND reason NOT LIKE '实验初始化%'",(a["key"],as_of,as_of)).fetchone()[0]
                accounts[a["start_mode"]]["short_trade_activity"]={"lookback_days":7,"filled_operations":count,
                    "note":"买卖各计一次，排除初始化；每周约两次是节奏参考，不是配额或硬上限，不限制必要的风险退出。"}
            if (a["start_mode"].startswith("short_") or self.settings.ai_trading_mode==RISK_MODE) and a["grams"]<=1e-9:
                from .short_review import wait_review
                review=wait_review(conn,a["plan"],quote,as_of)
                if review:accounts[a["start_mode"]]["short_wait_review"]=review

        return {"policy_version":policy_version(self.settings), "short_strategy_profile":{"name":"短线波段","holding_period":"数日至一两周","primary_timeframes":["1D"],"ignored_timeframes":["30m","1H","4H"],"entry_exit_timing":"日线与数日至周级别价格结构，不要求小时确认","pace":"每周约两次买卖操作是参考，不是配额或硬上限；不要求日内平仓"}, "reference_quote":quote, "accounts": accounts, "control_generation": control(conn,model)["generation"], "constraints": {"sell_fee_rate": self.settings.sell_fee_rate,
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
        if d["horizon"] != ("intraday" if self.settings.ai_trading_mode==RISK_MODE else PERIODS[a["start_mode"].split("_")[0]][1]):
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
            if self.settings.ai_trading_mode==RISK_MODE:
                budget=risk_state(a,price,lambda p:net_price(self.settings,p))
                if budget['buy_paused']:raise ValueError("阶段回撤触线，暂停新增买入")
                exit_price=d.get('risk_exit_cny')
                if exit_price is None or exit_price>=min(entry,price):raise ValueError("买入必须给低于当前及入场价的风险退出参考")
                protection=None if d.get('clear_stop_loss') else d.get('stop_loss_cny') or a['stop_price']
                exit_price=min(exit_price,protection) if protection else exit_price
                # Pending orders reserve risk at their highest possible entry, then recheck on fill.
                risk_entry=entry if check_range else min(entry,d['max_execution_price_cny'])
                after=a['cash']-amount+(a['grams']+amount/risk_entry)*net_price(self.settings,exit_price)
                current=a['cash']+a['grams']*net_price(self.settings,price)
                if current-after>budget['trade_risk_cny']+1e-6:raise ValueError("买入后整仓计划损失超过本账户单次风险预算")
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
                can_trade = a["key"] in keys(self.settings) and a["model"] in self.settings.ai_analysts and active(c,a["model"]) and status.get("available",True)
                if not a["started_at"]:
                    if not can_trade and self.settings.ai_trading_mode!=RISK_MODE:
                        continue
                    a["started_at"], a["initial_price"] = batch_start, quote["price"]
                    a["revision"] += 1
                    refreshes.append((a["model"],"portfolio-init:"+a["model"],"活动账户初始化，按新账户提交决策"))
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
                        refreshes.append((a["model"],f"portfolio-fill:{fill_id}",f"{(RISK_LABELS | LABELS)[a['start_mode']]}{reason}"))
                        emitted.append({"notification_topic":a["model"]+":"+a["start_mode"].split("_")[0],"key":f"autonomous:{fill_id}","title":f"{a['model'].title()} · {(RISK_LABELS | LABELS)[a['start_mode']]} {reason}","body":f"模拟卖出 {sold_grams:.4f} 克 @ 京东 {quote['price']:.2f} 元/克，保护已执行；卖出手续费 {sold_grams*quote['price']*self.settings.sell_fee_rate:.2f} 元"})
                latest = c.execute("SELECT rowid,* FROM ai_views WHERE analyst=? AND error IS NULL ORDER BY rowid DESC LIMIT 1",(a["model"],)).fetchone() if has_views else None
                if latest and latest["rowid"] > a["last_view_id"] and can_trade:
                    data = json.loads(latest["view_json"])
                    if (data.get("_trading_version")==policy_version(self.settings) or self.settings.ai_trading_mode!=RISK_MODE) and data.get("account_decisions") and view_active(c,a["model"],data):
                        self._accept(a,latest,data,quote)
                        rejected = c.execute("SELECT id,reason FROM ai_plans WHERE account=? AND view_id=? AND status='rejected'",(a["key"],latest["rowid"])).fetchone()
                        if rejected:
                            guard=data.get("_price_guard") or {}
                            if rejected['reason'].startswith("分析期间行情异动") and guard.get("attempt"):
                                event_id=f"price-guard:{a['model']}:{guard['attempt']}"
                            else:
                                prefix="price-range" if "成交价格区间" in rejected['reason'] else "portfolio-reject"
                                event_id=f"{prefix}:{rejected['id']}"
                            refreshes.append((a["model"],event_id,f"{(RISK_LABELS | LABELS)[a['start_mode']]}委托拒绝：{rejected['reason']}"))
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
                        emitted.append({"notification_topic":a["model"]+":"+a["start_mode"].split("_")[0],"key":f"autonomous:{fill_id}","title":f"{a['model'].title()} · {(RISK_LABELS | LABELS)[a['start_mode']]} 模拟"+("买入" if d["action"]=="buy" else "卖出"),"body":f"{grams:.4f} 克 @ 京东 {quote['price']:.2f} 元/克；{d['reason']}"})
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
