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

PROMPT = """\n公众输出public_content仅用公共行情/资讯、public_reference，禁止引用私人账户、历史、账户事件及持仓/成本/余额/盈亏/挂单/标识。analysis写共用形势，驱动注明日期/来源。
高风险、数日至两周；4%单次风险、20%回撤为参考，不保证损失。仓位=黄金市值/(黄金市值+黄金预留现金)。decision_basis共用解释仓位高低取舍、扣费与风险；假设入场/目标/退出须从公共数据取值或推导并列依据，不用私人独有价位；无法可靠量化则null并说明，不代表买入。复评须有人民币阈值、具名事件或带时区时点，不用“日线修复”；watch_conditions留空。
recommendations覆盖0至100每10%的11档，只写动作、目标、差异，共用失效条件引用decision_basis。条件方案允许全空，共用说明原因及何时复评；不得为补字段强行买入或生成条件单。数量仅用仓位%，条件目标与即时目标分开；无有效公共京东价则入场情景null、条件单空，不以国际价代替元/克；休市不建议立即成交。"""


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicAnalysis(PublicModel):
    outlook: str = Field(min_length=1, max_length=240)
    change_summary: str = Field(min_length=1, max_length=120)
    drivers: list[str] = Field(min_length=1, max_length=4)
    risk: str = Field(min_length=1, max_length=180)
    watch_conditions: list[str] = Field(default_factory=list, max_length=3, description="旧版兼容；新版复评只写decision_basis.review_triggers")


class PublicEntryScenario(PublicModel):
    entry_price_cny: float = Field(gt=0, allow_inf_nan=False)
    target_price_cny: float = Field(gt=0, allow_inf_nan=False)
    exit_price_cny: float = Field(gt=0, allow_inf_nan=False)
    basis: str = Field(min_length=1, max_length=160, description="公共来源、日期及情景取值依据；假设情景不是买入指令")


class PublicReviewTrigger(PublicModel):
    kind: Literal["price", "event", "time"]
    condition: Literal["at_or_above", "at_or_below"] | None = None
    price_cny: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    event: str | None = Field(default=None, min_length=1, max_length=120, description="具名且可核验的事件，不写笼统趋势改善")
    at: str | None = Field(default=None, max_length=40, description="已知事件/复评时点的ISO8601时间，必须带时区")


class PublicDecisionBasis(PublicModel):
    position_rationale: str = Field(min_length=1, max_length=240, description="比较主目标与更低/更高仓位的收益、风险及费用；区分已有仓位和新增买入，缺数时说明假设或缺口")
    entry_scenario: PublicEntryScenario | None
    no_entry_reason: str | None = Field(max_length=160)
    no_conditions_reason: str | None = Field(max_length=160)
    review_triggers: list[PublicReviewTrigger] = Field(min_length=1, max_length=4)


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
    decision_basis: PublicDecisionBasis | None = Field(default=None, description="新版必填的共用操作依据；旧记录可缺省，勿重复到11档")
    recommendations: list[PublicRecommendation] = Field(min_length=11, max_length=11)


SCHEMA = """
CREATE TABLE IF NOT EXISTS public_ai_reports (
 id INTEGER PRIMARY KEY, analyst TEXT NOT NULL, generation INTEGER NOT NULL,
 view_id INTEGER NOT NULL, created_at TEXT NOT NULL, snapshot_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL, content_json TEXT,
 quote_json TEXT, assessment_json TEXT, evidence_json TEXT, UNIQUE(analyst, view_id)
);
CREATE INDEX IF NOT EXISTS idx_public_ai_latest ON public_ai_reports(analyst,id DESC);
"""


def stamp(value):
    if not isinstance(value, str): raise ValueError("ISO timestamp required")
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
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


def validate_content(value, context, require_basis=False):
    data = PublicContent.model_validate(value).model_dump()
    basis = data.get("decision_basis")
    if require_basis and not basis:
        raise ValueError("Public decision basis missing")
    if basis:
        scenario = basis["entry_scenario"]
        if scenario:
            if not 0 < scenario["exit_price_cny"] < scenario["entry_price_cny"] < scenario["target_price_cny"]:
                raise ValueError("Invalid public entry scenario")
        elif not (basis["no_entry_reason"] or "").strip():
            raise ValueError("Missing reason for absent entry scenario")
        if not any(r["conditions"] for r in data["recommendations"]) and not (basis["no_conditions_reason"] or "").strip():
            raise ValueError("Missing reason for absent conditional plans")
        for trigger in basis["review_triggers"]:
            if trigger["at"] is not None:
                stamp(trigger["at"])
            kind = trigger["kind"]
            if kind == "price":
                if trigger["condition"] is None or trigger["price_cny"] is None or trigger["event"] is not None or trigger["at"] is not None:
                    raise ValueError("Invalid public price trigger")
            elif trigger["condition"] is not None or trigger["price_cny"] is not None:
                raise ValueError("Non-price trigger contains price")
            elif kind == "event" and not (trigger["event"] or "").strip():
                raise ValueError("Missing named review event")
            elif kind == "time" and not trigger["at"]:
                raise ValueError("Missing review time")
    if sorted(r["position_pct"] for r in data["recommendations"]) != list(POSITIONS):
        raise ValueError("Incomplete position coverage")
    for row in data["recommendations"]:
        row["invalidation"] = row["invalidation"].replace("decision_basis.review_triggers", "共用复评条件").replace("decision_basis", "共用操作依据")
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


def publication_content(value, context):
    """Omit an unsafe optional scenario, never relax checks on the remaining output."""
    data = PublicContent.model_validate(value).model_dump()
    basis = data.get("decision_basis")
    if basis and basis["entry_scenario"] and not privacy_ok({"scenario": basis["entry_scenario"]}, context):
        basis["entry_scenario"] = None
        basis["no_entry_reason"] = "无法核验该情景全部价位的公共来源，暂不展示数值情景；按共用复评条件重估。"
        omit_scenario_math(data, context)
    return validate_content(data, context, require_basis=True)


def omit_scenario_math(data, context=None):
    """An omitted scenario must not survive through its calculated prose returns."""
    allowed = {POLICY["trade_risk_pct"], POLICY["drawdown_reference_pct"],
               float(((context or {}).get("public_reference") or {}).get("sell_fee_rate", .004)) * 100}
    for row in data["recommendations"]:
        allowed.update((row["position_pct"], row["target_position_pct"]))
        allowed.update(c["target_position_pct"] for c in row["conditions"])
    def clean(text, fallback):
        pieces = []
        for sentence in re.split(r"(?<=[。！？])", text):
            percentages = [abs(float(n)) for n in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*[%％]", sentence)]
            if any(not any(abs(n - a) < 1e-8 for a in allowed) for n in percentages):
                continue
            if re.search(r"(?:收益风险比|盈亏比|\bRR\b)\s*[约为:=：]*\s*\d", sentence, re.I):
                continue
            pieces.append(sentence)
        return "".join(pieces).strip() or fallback
    basis = data["decision_basis"]
    basis["position_rationale"] = clean(basis["position_rationale"], "数值情景未通过公共来源核验，目标仓位的量化依据待复核；按共用条件重估。")
    basis["no_conditions_reason"] = clean(basis["no_conditions_reason"], "数值情景待复核，本轮不提供条件方案。") if basis["no_conditions_reason"] else None
    for row in data["recommendations"]:
        row["reason"] = clean(row["reason"], "数值情景待复核，本档目标仅供观察。")
        row["invalidation"] = clean(row["invalidation"], "按共用复评条件重估。")


def save(conn, analyst, generation, view_id, created_at, snapshot_at, value, context, invalidated=False, assessment=None):
    """Within the existing analysis transaction; no network, model call or commit."""
    at = stamp(created_at)
    quote = public_quote(context.get("jd_accumulation_quote"), stamp(snapshot_at))
    if quote:
        reference = context.get("public_reference") or {}
        quote.update(sell_fee_rate=reference.get("sell_fee_rate", 0.004), spread_cny=reference.get("spread_cny", 0.0))
    status, content = "unavailable", None
    if value is not None:
        try:
            content = publication_content(value, context)
            # Conditional prices must never be borrowed from a private bank quote.
            basis = content["decision_basis"]
            has_prices = basis["entry_scenario"] or any(t["kind"] == "price" for t in basis["review_triggers"])
            if not quote and (has_prices or any(r["conditions"] for r in content["recommendations"])):
                raise ValueError("Public quote missing")
            status = "invalidated" if invalidated else "ready"
        except (ValueError, TypeError):
            content = None
    expiry = min(at + timedelta(minutes=TTL_MINUTES), stamp(snapshot_at) + timedelta(minutes=TTL_MINUTES))
    from .public_research import assessment_projection, evidence_for
    conn.execute("INSERT INTO public_ai_reports(analyst,generation,view_id,created_at,snapshot_at,expires_at,status,content_json,quote_json,assessment_json,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (analyst, generation, view_id, created_at, snapshot_at, expiry.isoformat(), status,
                  json.dumps(content, ensure_ascii=False) if content else None, json.dumps(quote) if quote else None,
                  json.dumps(assessment_projection(assessment)), json.dumps(evidence_for(content, context), ensure_ascii=False)))
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


def scenario_metrics(content, quote, position=None):
    """Program arithmetic for a hypothetical new position, never personal P/L."""
    scenario = (content.get("decision_basis") or {}).get("entry_scenario")
    if not scenario or not quote:
        return None
    fee, spread = float(quote.get("sell_fee_rate", 0.004)), float(quote.get("spread_cny", 0))
    if not 0 <= fee < 1 or not math.isfinite(spread) or spread < 0:
        raise ValueError("Invalid public fee assumption")
    entry = scenario["entry_price_cny"]
    upside = ((scenario["target_price_cny"] * (1 - fee) - spread) / entry - 1) * 100
    downside = ((scenario["exit_price_cny"] * (1 - fee) - spread) / entry - 1) * 100
    result = {"sell_fee_rate": fee, "spread_cny": spread, "net_upside_pct": round(upside, 3),
              "net_downside_pct": round(downside, 3), "reward_risk_ratio": round(upside / -downside, 3) if upside > 0 and downside < 0 else None}
    if position is not None:
        row = next(r for r in content["recommendations"] if r["position_pct"] == position)
        result["at_target_position"] = {"target_position_pct": row["target_position_pct"],
            "net_upside_pct": round(upside * row["target_position_pct"] / 100, 3),
            "net_downside_pct": round(downside * row["target_position_pct"] / 100, 3)}
    return result


def review_due(content, current_quote, now):
    for trigger in (content.get("decision_basis") or {}).get("review_triggers", []):
        if trigger["at"] and now >= stamp(trigger["at"]):
            return True
        if trigger["kind"] == "price" and current_quote:
            price = current_quote["price_cny"]
            if (price >= trigger["price_cny"] if trigger["condition"] == "at_or_above" else price <= trigger["price_cny"]):
                return True
    return False


def latest(conn, settings, analyst="codex", position=None, now=None):
    """Explicit public DTO from its own table; no fallback to ai_views/accounts."""
    if analyst not in ("claude", "codex") or analyst not in settings.ai_analysts:
        raise ValueError("Invalid analyst")
    if position is not None and (type(position) is not int or position not in POSITIONS):
        raise ValueError("Invalid position")
    now = now or datetime.now(timezone.utc)
    result = {"schema_version": 2, "analyst": analyst, "status": "unavailable", "policy": dict(POLICY),
              "available_analysts": [m for m in settings.ai_analysts if m in ('claude', 'codex') and active(conn, m)],
              "analysis": None, "decision_basis": None, "scenario_metrics": None, "recommendations": []}
    if not active(conn, analyst):
        return result
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_ai_reports'").fetchone():
        return result
    row = conn.execute("SELECT generation,created_at,snapshot_at,expires_at,status,content_json,quote_json,assessment_json,evidence_json FROM public_ai_reports WHERE analyst=? ORDER BY id DESC LIMIT 1", (analyst,)).fetchone()
    if not row or not active(conn, analyst, row[0]):
        return result
    generation, created, snapshot, expiry, status, raw, quote_raw, assessment_raw, evidence_raw = row
    result.update(created_at=created, snapshot_at=snapshot, expires_at=expiry)
    if not raw or status != "ready":
        return result
    try:
        content = validate_content(json.loads(raw), {})
        quote = json.loads(quote_raw) if quote_raw else None
        # Also protect the short-lived public records written before this cleanup.
        if ((content.get("decision_basis") or {}).get("no_entry_reason") or "").startswith("无法核验该情景全部价位的公共来源"):
            omit_scenario_math(content, {"public_reference": quote or {}})
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
        if not reason and review_due(content, current, now):
            reason = "review_due"
        # Rebuild the response: never pass through arbitrary stored top-level fields.
        result.update(schema_version=2 if content.get("decision_basis") else 1,
                      status="stale" if reason else "current", reason=reason, analysis=content["analysis"],
                      decision_basis=content.get("decision_basis"), scenario_metrics=scenario_metrics(content, quote, position),
                      reference_quote=quote, recommendations=[r for r in content["recommendations"] if position is None or r["position_pct"] == position])
        from .public_research import metadata
        result.update(metadata(assessment_raw, evidence_raw))
    except (ValueError, TypeError, KeyError):
        pass
    return result
