"""Persistent per-model switches, shared by the monitor and dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MODELS = ("claude", "codex")
REMOVED_ACCOUNTS = ("dca", "dca_smart", "claude:dca_smart_ai", "codex:dca_smart_ai")


def daily_keys(settings) -> tuple:
    """Active experiment only; retired DCA options in an old .env cannot recreate accounts."""
    keys = [key for key in ("hold", "position") if key in settings.strategies]
    for model in (() if settings.ai_trading_mode in ("six_account","four_account","risk_six_account") else settings.ai_analysts):
        key = f"{model}:position_ai"
        if model in MODELS and ("position_ai" in settings.strategies or key in settings.strategies):
            keys.append(key)
    return tuple(keys)


def control(conn: sqlite3.Connection, model: str) -> dict:
    row = conn.execute("SELECT value FROM state WHERE key=?", (f"model_control:{model}",)).fetchone()
    if row:
        import json
        return json.loads(row[0])
    return {"enabled": True, "generation": 0, "changed_at": None}


def set_enabled(conn: sqlite3.Connection, model: str, enabled: bool) -> dict:
    import json
    if model not in MODELS or type(enabled) is not bool:
        raise ValueError("模型或开关值无效")
    # Serialize read/modify/write so simultaneous browser requests cannot lose a generation.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        old = control(conn, model)
        if old["enabled"] == enabled:
            return old
        new = {"enabled": enabled, "generation": old["generation"] + 1,
               "changed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                     (f"model_control:{model}", json.dumps(new), new["changed_at"]))
    return new


def active(conn: sqlite3.Connection, model: str, generation=None) -> bool:
    c = control(conn, model)
    return c["enabled"] and (generation is None or c["generation"] == generation)


def view_active(conn: sqlite3.Connection, model: str, view) -> bool:
    if not view:
        return False
    c = control(conn, model)
    if not c["enabled"]:
        return False
    if "_control_generation" in view:
        return c["generation"] == view["_control_generation"]
    return not c["changed_at"] or (view.get("_ts") and
        datetime.fromisoformat(view["_ts"]) > datetime.fromisoformat(c["changed_at"]))


def identity(key: str):
    parts = key.split(":", 1)
    return (parts[0], parts[1]) if len(parts) == 2 and parts[0] in MODELS else (None, key)


def short_labels(settings) -> dict:
    labels = {"short": "短线日内"} if "short" in settings.strategies else {}
    if "short_ai" in settings.strategies and settings.ai_trading_mode not in ("six_account","four_account","risk_six_account"):
        labels.update({f"{m}:short_ai": f"{m.title()} · 短线" for m in settings.ai_analysts if m in MODELS})
    return labels
