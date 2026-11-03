"""Public-only output of the existing model round; never projects private views."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .controls import active

POSITIONS = tuple(range(0, 101, 10))
TTL_MINUTES = 240
MOVE_PCT = 0.4
POSITION_BASIS = "黄金市值 /（黄金市值 + 为黄金投资预留的现金）"
POLICY = {"risk": "high", "horizon": "数日至两周", "position_basis": POSITION_BASIS,
          "trade_risk_pct": 4.0, "drawdown_reference_pct": 20.0}

PROMPT = """\n公众输出public_content与私人决策共用本轮研究，不再复述研究笔记。仅据公共行情/资讯，排除trading_accounts、live_readonly_accounts、账户历史及其触发事件；不得引用或暗示私人持仓、成本、余额、盈亏、挂单或账户标识。
公众基准：高风险、数日至两周波段，单次计划风险参考总资金4%、回撤参考20%，均非损失保证或仓位硬上限；可积极分批参与、调整，亦可有据观望。仓位=黄金市值/(黄金市值+黄金投资预留现金)。analysis简述形势、变化、驱动、风险和复评条件；recommendations覆盖0至100每10个百分点的11种独立当前仓位，各写操作、目标仓位、简短理由、失效条件及至多2个条件方案。数量只用仓位百分比，不用金额/克数；条件方案目标为触发后的总仓位，与即时目标分开；人民币价只据公共京东报价及可说明的市场证据，国际价不可直接充作元/克。没有可靠公共京东价时conditions为空、仅给仓位方向；休市不建议立即成交。共用理由写analysis，各档只写差异，不强制交易、不承诺收益。"""


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicAnalysis(PublicModel):
    outlook: str = Field(min_length=1, max_length=240)
    change_summary: str = Field(min_length=1, max_length=120)
    drivers: list[str] = Field(min_length=1, max_length=4)
    risk: str = Field(min_length=1, max_length=180)
    watch_conditions: list[str] = Field(min_length=1, max_length=3)


class PublicCondition(PublicModel):
    action: Literal["buy", "sell"]
    condition: Literal["at_or_above", "at_or_below"]
    price_cny: float = Field(gt=0, allow_inf_nan=False)
    target_position_pct: int = Field(ge=0, le=100, strict=True)


class PublicRecommendation(PublicModel):
    position_pct: int = Field(ge=0, le=100, strict=True)
    action: Literal["buy", "sell", "hold", "wait"]
    target_position_pct: int = Field(ge=0, le=100, strict=True)
    reason: str = Field(min_length=1, max_length=120)
    invalidation: str = Field(min_length=1, max_length=120)
    conditions: list[PublicCondition] = Field(max_length=2)


class PublicContent(PublicModel):
    analysis: PublicAnalysis
    recommendations: list[PublicRecommendation] = Field(min_length=11, max_length=11)


SCHEMA = """
CREATE TABLE IF NOT EXISTS public_ai_reports (
 id INTEGER PRIMARY KEY, analyst TEXT NOT NULL, generation INTEGER NOT NULL,
 view_id INTEGER NOT NULL, created_at TEXT NOT NULL, snapshot_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL, content_json TEXT,
 quote_json TEXT, UNIQUE(analyst, view_id)
);
CREATE INDEX IF NOT EXISTS idx_public_ai_latest ON public_ai_reports(analyst,id DESC);
"""


def stamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt


def public_quote(value, at):
    if not isinstance(value, dict) or value.get("source") != "jd_latest_price_api":
        return None
    try:
        if isinstance(value["price"], bool):
            return None
        price = float(value["price"])
        if not math.isfinite(price) or price <= 0:
            return None
        if any(not -30 <= (at - stamp(value[k])).total_seconds() <= 120 for k in ("quote_at", "fetched_at")):
            return None
        return {"price_cny": price, "quoted_at": value["quote_at"], "source": "jd_latest_price_api"}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, str):
        yield value


def privacy_ok(content, context):
    """Fail closed on private labels/identifiers, absolute holdings and balances.

    A shared model sees private inputs: this is defense in depth, not a claim of
    mathematical noninterference. No private prose is ever reused as public text.
    """
    text = "\n".join(strings(content))
    if re.search(r"真实账户|模拟[盘账]|私人|私有|本账户|你的账户|您[的有]|账户余额|持仓成本|浮盈亏|已挂|原挂单|trading_accounts|live_readonly|order_key|[\w.+-]+@[\w.-]+\.[a-z]{2,}|1[3-9]\d{9}", text, re.I):
        return False
    # Prices (元/克) and position percentages are public; absolute asset amounts aren't.
    if re.search(r"\d[\d,.]*\s*(?:克|grams?\b|g\b|万元|元(?![/／每]?克))", text, re.I):
        return False
    def numbers(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from numbers(v)
        elif isinstance(value, list):
            for v in value:
                yield from numbers(v)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield float(value)
    public_numbers = set(numbers({k: v for k, v in context.items() if k not in
                                 ("trading_accounts", "live_readonly_accounts", "trigger_events", "event_coverage")}))
    output_numbers = set(numbers(content))
    output_numbers.update(float(m.replace(",", "")) for m in re.findall(r"(?<![0-9.])\d[\d,]*(?:\.\d+)?", text))
    sensitive = {"average_cost_cny", "cash_cny", "cash_balance_cny", "cash", "budget", "holding_pnl_cny",
                 "price_cny", "limit_price_cny", "limit_price", "stop_price", "target_price", "total_grams", "grams"}
    def private_numbers(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in sensitive and isinstance(v, (int, float)) and not isinstance(v, bool):
                    yield float(v)
                else:
                    yield from private_numbers(v)
        elif isinstance(value, list):
            for v in value:
                yield from private_numbers(v)
    for name in ("trading_accounts", "live_readonly_accounts"):
        private = context.get(name) or {}
        # Private cost/order levels and distinctive balances must not become public levels.
        if any(n in output_numbers and n not in public_numbers for n in private_numbers(private) if n > 100):
            return False
        for key, account in private.get("accounts", {}).items():
            if str(key) in text or any(str(account.get(k, "")) in text for k in ("key", "revision") if len(str(account.get(k, ""))) >= 8):
                return False
        def identifiers(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in ("order_key", "account_id", "account_number", "phone", "mobile") and isinstance(v, (str, int)):
                        yield str(v)
                    else:
                        yield from identifiers(v)
            elif isinstance(value, list):
                for v in value:
                    yield from identifiers(v)
        if any(token in text for token in identifiers(private) if len(token) >= 4):
            return False
    return True


def validate_content(value, context):
    data = PublicContent.model_validate(value).model_dump()
    if sorted(r["position_pct"] for r in data["recommendations"]) != list(POSITIONS):
        raise ValueError("Incomplete position coverage")
    for row in data["recommendations"]:
        current, target, action = row["position_pct"], row["target_position_pct"], row["action"]
        if not ((action == "buy" and target > current) or (action == "sell" and target < current) or (action in ("hold", "wait") and target == current)):
            raise ValueError("Inconsistent position action")
        for condition in row["conditions"]:
            next_target = condition["target_position_pct"]
            if not (next_target > target if condition["action"] == "buy" else next_target < target):
                raise ValueError("Inconsistent conditional position")
    if any(len(s) > 240 for s in strings(data)) or not privacy_ok(data, context):
        raise ValueError("Public content rejected")
    data["recommendations"].sort(key=lambda r: r["position_pct"])
    return data


def save(conn, analyst, generation, view_id, created_at, snapshot_at, value, context, invalidated=False):
    """Within the existing analysis transaction; no network, model call or commit."""
    at = stamp(created_at)
    quote = public_quote(context.get("jd_accumulation_quote"), stamp(snapshot_at))
    status, content = "unavailable", None
    if value is not None:
        try:
            content = validate_content(value, context)
            # Conditional prices must never be borrowed from a private bank quote.
            if not quote and any(r["conditions"] for r in content["recommendations"]):
                raise ValueError("Public quote missing")
            status = "invalidated" if invalidated else "ready"
        except (ValueError, TypeError):
            content = None
    expiry = min(at + timedelta(minutes=TTL_MINUTES), stamp(snapshot_at) + timedelta(minutes=TTL_MINUTES))
    conn.execute("INSERT INTO public_ai_reports(analyst,generation,view_id,created_at,snapshot_at,expires_at,status,content_json,quote_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (analyst, generation, view_id, created_at, snapshot_at, expiry.isoformat(), status,
                  json.dumps(content, ensure_ascii=False) if content else None, json.dumps(quote) if quote else None))
    return status


def previous_analysis(conn, analyst, generation):
    row = conn.execute("SELECT created_at,content_json FROM public_ai_reports WHERE analyst=? AND generation=? AND status='ready' ORDER BY id DESC LIMIT 1", (analyst, generation)).fetchone()
    if not row or not row[1]:
        return None
    try:
        analysis = PublicContent.model_validate_json(row[1]).analysis
        return {"created_at": row[0], "outlook": analysis.outlook, "risk": analysis.risk}
    except ValueError:
        return None


def latest(conn, settings, analyst="codex", position=None, now=None):
    """Explicit public DTO from its own table; no fallback to ai_views/accounts."""
    if analyst not in ("claude", "codex") or analyst not in settings.ai_analysts:
        raise ValueError("Invalid analyst")
    if position is not None and (type(position) is not int or position not in POSITIONS):
        raise ValueError("Invalid position")
    now = now or datetime.now(timezone.utc)
    result = {"schema_version": 1, "analyst": analyst, "status": "unavailable", "policy": dict(POLICY),
              "analysis": None, "recommendations": []}
    if not active(conn, analyst):
        return result
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_ai_reports'").fetchone():
        return result
    row = conn.execute("SELECT generation,created_at,snapshot_at,expires_at,status,content_json,quote_json FROM public_ai_reports WHERE analyst=? ORDER BY id DESC LIMIT 1", (analyst,)).fetchone()
    if not row or not active(conn, analyst, row[0]):
        return result
    generation, created, snapshot, expiry, status, raw, quote_raw = row
    result.update(created_at=created, snapshot_at=snapshot, expires_at=expiry)
    if not raw or status != "ready":
        return result
    try:
        content = validate_content(json.loads(raw), {})
        quote = json.loads(quote_raw) if quote_raw else None
        reason = None
        if now < stamp(created) or now >= stamp(expiry):
            reason = "expired"
        state = conn.execute("SELECT value FROM state WHERE key='web_snapshot'").fetchone()
        snap = json.loads(state[0]) if state else {}
        current = public_quote(snap.get("execution_quote"), now)
        from .trading_hours import trading_session
        if not reason and not trading_session(now)["is_open"]:
            reason = "market_closed"
        if not reason and not current:
            reason = "quote_unavailable"
        if not reason and quote and abs(current["price_cny"] / quote["price_cny"] - 1) * 100 >= MOVE_PCT - 1e-9:
            reason = "price_changed"
        # Rebuild the response: never pass through arbitrary stored top-level fields.
        result.update(status="stale" if reason else "current", reason=reason, analysis=content["analysis"],
                      reference_quote=quote, recommendations=[r for r in content["recommendations"] if position is None or r["position_pct"] == position])
    except (ValueError, TypeError, KeyError):
        pass
    return result
