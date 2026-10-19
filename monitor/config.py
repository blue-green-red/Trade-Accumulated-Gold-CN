"""监控程序配置：读取仓库根目录 .env，并把 skill 的 scripts/ 加入 import 路径（复用取数与计算模块）。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / ".claude" / "skills" / "gold-analysis" / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

BJ = ZoneInfo("Asia/Shanghai")


def load_env_file(path: Path) -> None:
    """极简 .env 解析（KEY=VALUE，# 注释）；已存在的环境变量优先。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        quote = None
        for i, char in enumerate(value):
            if char in ("'", '"') and (i == 0 or value[i - 1] != "\\"):
                quote = None if quote == char else char if quote is None else quote
            elif char == "#" and quote is None and (i == 0 or value[i - 1].isspace()):
                value = value[:i]
                break
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env_file(ROOT / ".env")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    web_min_password_length: int = max(8, int(_env_float("GOLD_WEB_MIN_PASSWORD_LENGTH", 12)))
    db_path: Path = Path(os.environ.get("GOLD_MONITOR_DB", ROOT / "monitor" / "data" / "monitor.db"))
    tick_seconds: float = _env_float("GOLD_MONITOR_TICK_SECONDS", 60)
    bank_refresh_minutes: float = _env_float("GOLD_MONITOR_BANK_REFRESH_MINUTES", 20)
    fx_refresh_minutes: float = _env_float("GOLD_MONITOR_FX_REFRESH_MINUTES", 15)
    stale_minutes: float = _env_float("GOLD_MONITOR_STALE_MINUTES", 10)   # 报价超过此时长未更新视为休市
    alert_cooldown_minutes: float = _env_float("GOLD_MONITOR_ALERT_COOLDOWN_MINUTES", 60)
    level_touch_usd: float = _env_float("GOLD_MONITOR_LEVEL_TOUCH_USD", 3)
    move_window_minutes: float = _env_float("GOLD_MONITOR_MOVE_WINDOW_MINUTES", 30)   # 价格异动：窗口
    move_pct: float = _env_float("GOLD_MONITOR_MOVE_PCT", 0.6)                        # 价格异动：涨跌幅阈值 %
    paper_bank: str = os.environ.get("GOLD_MONITOR_PAPER_BANK", "zheshang")
    paper_grams: float = _env_float("GOLD_MONITOR_PAPER_GRAMS", 10)
    spread_cny: float = _env_float("GOLD_MONITOR_SPREAD_CNY", 0.0)          # 买卖价差，两家均为单一报价
    sell_fee_rate: float = _env_float("GOLD_MONITOR_SELL_FEE_RATE", 0.004)  # 卖出手续费 0.4%
    # 同时运行的模拟盘：hold/position 为规则日线，position_ai 按模型拆分；short 为短线基准
    strategies: tuple = tuple(s.strip() for s in os.environ.get("GOLD_MONITOR_STRATEGIES", "hold,position,position_ai,short,short_ai").split(",") if s.strip())
    ai_trading_mode: str = os.environ.get("GOLD_AI_TRADING_MODE", "six_account")
    ai_short_review_minutes: float = _env_float("GOLD_AI_SHORT_REVIEW_MINUTES", 0)
    ai_periodic_min_move_pct: float = _env_float("GOLD_AI_PERIODIC_MIN_MOVE_PCT", 0.4)
    ai_review_max_idle_minutes: float = _env_float("GOLD_AI_REVIEW_MAX_IDLE_MINUTES", 240)
    ai_max_runs_per_day: int = int(_env_float("GOLD_AI_MAX_RUNS_PER_DAY", 120))
    ai_urgent_reserve: int = int(_env_float("GOLD_AI_URGENT_RESERVE", 2))
    ai_decision_max_age_minutes: float = _env_float("GOLD_AI_DECISION_MAX_AGE_MINUTES", 15)
    account_budget: float = _env_float("GOLD_MONITOR_ACCOUNT_BUDGET", 100_000)   # 每个日线策略账户的虚拟本金
    weekly_report_bj: tuple = (5, 10)   # 每周六 10 点推送战绩榜（周几, 小时）
    reentry_cooldown_minutes: float = _env_float("GOLD_MONITOR_REENTRY_COOLDOWN_MINUTES", 60)
    digest_hours_bj: tuple = tuple(int(h) for h in os.environ.get("GOLD_MONITOR_DIGEST_HOURS", "9,20").split(",") if h)
    pushplus_token: str = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    serverchan_sendkey: str = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    ai_model: str = os.environ.get("GOLD_AI_MODEL", "claude-opus-5-5")
    ai_backend: str = os.environ.get("GOLD_AI_BACKEND", "auto")          # Claude 分析师后端：auto / api / claude_code
    ai_analysts: tuple = tuple(s.strip() for s in os.environ.get("GOLD_AI_ANALYSTS", "claude,codex").split(",") if s.strip())
    ai_strategy_source: str = os.environ.get("GOLD_AI_STRATEGY_SOURCE", "claude")   # 策略用谁的判断：claude / codex / consensus
    ai_optimize: bool = os.environ.get("GOLD_AI_OPTIMIZE", "1") != "0"
    codex_resume_enabled: bool = os.environ.get("GOLD_CODEX_RESUME", "0") != "0"
    codex_session_max_turns: int = max(1, int(_env_float("GOLD_CODEX_SESSION_MAX_TURNS", 6)))
    codex_session_max_minutes: float = _env_float("GOLD_CODEX_SESSION_MAX_MINUTES", 240)
    codex_cli: str = os.environ.get("GOLD_CODEX_CLI", "codex")
    codex_model: str = os.environ.get("GOLD_CODEX_MODEL", "").strip()      # 留空用 Codex 默认模型
    claude_cli: str = os.environ.get("GOLD_CLAUDE_CLI", "claude")
    ai_effort: str = os.environ.get("GOLD_AI_EFFORT", "").strip()          # 留空用模型默认；low/medium/high/xhigh/max
    ai_max_budget_usd: float = _env_float("GOLD_AI_MAX_BUDGET_USD", 3)   # claude_code 单次调用上限（按官网价折算）
    ai_schedule_bj: tuple = tuple(s.strip() for s in os.environ.get("GOLD_AI_SCHEDULE", "08:40,20:45").split(",") if s.strip())
    jd_research_max_requests_per_day: int = int(_env_float("GOLD_JD_RESEARCH_MAX_REQUESTS_PER_DAY", 10))
    clawx_api_key: str = field(default=os.environ.get("CLAWX_JR_API_KEY", ""), repr=False)
    ai_max_searches: int = int(_env_float("GOLD_AI_MAX_SEARCHES", 2))
    ai_view_max_age_hours: float = _env_float("GOLD_AI_VIEW_MAX_AGE_HOURS", 13)  # 短线用的 AI 判断有效期：覆盖 08:40→20:45 的间隔
    jin10_enabled: bool = os.environ.get("GOLD_JIN10_ENABLED", "1").strip() not in ("0", "false", "")
    jin10_poll_seconds: float = _env_float("GOLD_JIN10_POLL_SECONDS", 120)
    jin10_ai_daily_cap: int = int(_env_float("GOLD_JIN10_AI_DAILY_CAP", 4))        # 每天最多因快讯触发 AI 的次数
    jin10_ai_gap_minutes: float = _env_float("GOLD_JIN10_AI_GAP_MINUTES", 30)      # 快讯触发与上次分析的最小间隔
    # AI 主导（只作用于 +AI 账户）：概率阈值
    ai_short_entry_p: float = _env_float("GOLD_AI_SHORT_ENTRY_P", 0.50)     # 短线：3 日上涨概率达到此值才做多
    ai_short_edge: float = _env_float("GOLD_AI_SHORT_EDGE", 0.15)           # 短线：且上涨概率比下跌概率高出此值
    ai_short_exit_p: float = _env_float("GOLD_AI_SHORT_EXIT_P", 0.50)       # 短线：3 日下跌概率达到此值则持仓离场
    ai_long_buy_edge: float = _env_float("GOLD_AI_LONG_BUY_EDGE", 0.10)     # 中长线：3 个月涨跌概率差 ≥ 此值才建仓
    ai_long_reduce_edge: float = _env_float("GOLD_AI_LONG_REDUCE_EDGE", -0.10)  # ≤ 此值减仓一半
    ai_long_exit_edge: float = _env_float("GOLD_AI_LONG_EXIT_EDGE", -0.25)  # ≤ 此值清仓
    ai_long_stop_loss: float = _env_float("GOLD_AI_LONG_STOP_LOSS", 0.12)   # 中长线+AI 风控：亏损超过 12% 清仓
    ai_dca_max_mult: float = _env_float("GOLD_AI_DCA_MAX_MULT", 2.5)        # 定投+AI：每月金额最多为基础额的倍数
    ai_event_delay_minutes: float = _env_float("GOLD_AI_EVENT_DELAY_MINUTES", 10)  # 重要数据公布后多久触发 AI（等实际值出来）


BANK_NAMES = {"zheshang": "浙商积存金", "minsheng": "民生积存金"}
