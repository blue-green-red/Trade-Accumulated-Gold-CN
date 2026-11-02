"""Remove the retired standalone rule books without resetting the AI experiment."""
import json

from .store import utc_now

RULE_KEYS = ("hold", "position", "short")
MARKER = "removed_rule_accounts:v1"


def remove_rule_accounts(conn):
    if conn.execute("SELECT 1 FROM state WHERE key=?", (MARKER,)).fetchone():
        return False
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    removed = {}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM state WHERE key=?", (MARKER,)).fetchone():
            return False
        if "paper_fills" in tables and "paper_trades" in tables:
            removed["paper_fills"] = conn.execute(
                "DELETE FROM paper_fills WHERE trade_id IN "
                "(SELECT id FROM paper_trades WHERE strategy IN (?,?,?))", RULE_KEYS).rowcount
        for table in ("accounts", "account_fills", "account_equity", "trade_cycles",
                      "paper_accounts", "paper_trades", "paper_equity"):
            if table in tables:
                removed[table] = conn.execute(
                    f"DELETE FROM {table} WHERE strategy IN (?,?,?)", RULE_KEYS).rowcount
        for table, column in (("alerts", "key"), ("notification_outbox", "event_key")):
            if table in tables:
                for key in RULE_KEYS:
                    conn.execute(f"DELETE FROM {table} WHERE {column} LIKE ? OR {column} LIKE ?",
                                 (f"acct:{key}:%", f"paper:{key}:%"))
        # Explicit topic overrides must not leak retired trades into system alerts.
        if "notification_outbox" in tables:
            conn.execute("DELETE FROM notification_outbox WHERE "
                         "json_extract(event_json,'$.notification_topic') IN (?,?,?)",
                         tuple("rules:" + key for key in RULE_KEYS))
        conn.execute("DELETE FROM state WHERE key IN (?,?,?)",
                     tuple("notify:rules:" + key for key in RULE_KEYS))
        stamp = utc_now().isoformat()
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?)",
                     (MARKER, json.dumps({"at": stamp, "removed": removed}), stamp))
    return True
