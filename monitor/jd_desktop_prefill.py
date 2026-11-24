"""Prepare the latest validated real-account Codex advice in the desktop form."""
import json
from datetime import datetime, timezone

from .controls import active
from .jd_desktop import gram_text
from .jd_live_readonly import accounts, age, enabled_banks, review_context_current
from .trading_hours import trading_session

STATE_KEY = "jd_desktop_prefill"


def arm(store):
    state = {"armed_at": datetime.now(timezone.utc).isoformat(), "banks": {}}
    store.set_state(STATE_KEY, state)
    return state


def trade_plan(account, advice, now):
    action = advice.get("action")
    if action not in ("buy", "sell"):
        return None, "no_trade"
    if (advice.get("validation_version") != 1 or advice.get("action_status") != "current"
            or advice.get("snapshot_revision") != account.get("revision")
            or age(advice.get("expires_at"), now) >= 0 or advice.get("_path_pause")):
        return None, "advice_unavailable"
    if account.get("source") != "desktop" or not account.get("connected") or not 0 <= age(account.get("fetched_at"), now) <= 120:
        return None, "account_unavailable"
    pending = account.get("pending_orders") or {}
    if pending.get("status") != "fresh" or not pending.get("complete"):
        return None, "account_unavailable"
    grams = gram_text(advice.get("grams"))
    if grams is None:
        return None, "quantity_unavailable"
    condition, price = advice.get("price_condition"), advice.get("price_cny")
    if condition not in ("at_or_below", "at_or_above", "reference", "none"):
        return None, "advice_unavailable"
    if condition in ("at_or_below", "at_or_above") and (type(price) not in (int, float) or not 0 < price < float("inf")):
        return None, "advice_unavailable"
    if not trading_session(now)["is_open"]:
        return None, "market_closed"
    return {"action": action, "grams": grams, "price_condition": condition, "price_cny": price}, None


def poll(store, reader, state):
    if not active(store.conn, "codex"):
        return
    now = datetime.now(timezone.utc)
    for bank in enabled_banks(store.conn):
        row = store.conn.execute(
            "SELECT id,advice_json FROM jd_live_advice "
            "WHERE bank=? AND analyst='codex' AND created_at>=? ORDER BY id DESC LIMIT 1",
            (bank, state["armed_at"]),
        ).fetchone()
        if row is None:
            continue
        previous = state["banks"].get(bank, {})
        if row[0] == previous.get("advice_id") and previous["status"] not in ("waiting_desktop", "account_unavailable"):
            continue
        advice_id, raw = row
        direction = json.loads(raw).get("action")
        status = {"advice_id": advice_id, "action": direction}
        if direction not in ("buy", "sell"):
            status["status"] = "no_trade"
        else:
            account = next(iter(accounts(store.conn, now, selected_bank=bank)), {})
            advice = next((item for item in account.get("advice", []) if item["id"] == advice_id), {})
            plan, reason = trade_plan(account, advice, now) if advice else (None, "advice_unavailable")
            if plan is None:
                status["status"] = reason
            elif review_context_current(store.conn, account, advice):
                try:
                    reader.landing_page(bank)
                except Exception:
                    status["status"] = "waiting_desktop"
                else:
                    # Persist the claim before touching the form. A worker restart
                    # never applies an already-published suggestion again.
                    state["banks"][bank] = {**status, "status": "preparing", "at": now.isoformat()}
                    store.set_state(STATE_KEY, state)
                    try:
                        result = reader.prepare_market_order(bank, plan)
                    except Exception:
                        result = {"status": "unverified"}
                    status.update(result)
                    if result["status"] == "prepared":
                        status["grams"] = plan["grams"]
            else:
                status["status"] = "advice_unavailable"
        if any(previous.get(key) != value for key, value in status.items()):
            state["banks"][bank] = {**status, "at": now.isoformat()}
            store.set_state(STATE_KEY, state)
