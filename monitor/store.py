"""SQLite 持久化。所有数据库读写集中在此文件，日后换 MySQL 只需改这里。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    ts TEXT NOT NULL,
    xauusd REAL, day_high REAL, day_low REAL,
    jd_price REAL, zheshang_est REAL, minsheng_est REAL,
    r30 REAL, r60 REAL, r240 REAL, r1d REAL, r1w REAL,
    market_open INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);
CREATE TABLE IF NOT EXISTS bank_quotes (
    ts TEXT NOT NULL, bank TEXT NOT NULL, price REAL, xauusd REAL,
    ratio REAL, premium REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    ts TEXT NOT NULL, key TEXT NOT NULL, title TEXT, body TEXT, delivered INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, ts);
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    channel TEXT NOT NULL, status TEXT NOT NULL, provider_id TEXT, provider_code TEXT,
    event_keys_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_key TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    event_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due ON notification_outbox(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank TEXT NOT NULL, status TEXT NOT NULL,          -- open / closed
    strategy TEXT NOT NULL DEFAULT 'short',            -- short / short_ai
    opened_at TEXT, entry_usd REAL, entry_cny REAL, grams REAL, open_grams REAL,
    stop_cny REAL, t1_cny REAL, t2_cny REAL, t1_hit INTEGER DEFAULT 0,
    closed_at TEXT, realized_cny REAL DEFAULT 0, plan_json TEXT, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_accounts (
    strategy TEXT PRIMARY KEY, budget REAL NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_equity (
    strategy TEXT NOT NULL, ts TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY(strategy, ts)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, ts TEXT NOT NULL,
    side TEXT NOT NULL, grams REAL NOT NULL, price REAL NOT NULL,
    fee REAL NOT NULL, quote_json TEXT, reason TEXT
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(paper_trades)")}
        if "strategy" not in cols:   # 旧库迁移：已有短线记录归入 short
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'short'")
        for name, kind in (("exit_cny", "REAL"), ("close_decision_json", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        now = iso(utc_now())
        self.conn.execute("INSERT OR IGNORE INTO state(key,value,updated_at) VALUES(?,?,?)",
                          ("jd_execution_since", json.dumps(now), now))
        self.conn.commit()

    # ── state（键值，JSON 存储） ─────────────────────────────────────────
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), iso(utc_now())),
        )
        self.conn.commit()

    # ── 行情 ─────────────────────────────────────────────────────────────
    def add_tick(self, snap: Dict[str, Any]) -> None:
        r = snap.get("ratings", {})
        est = snap.get("bank_est", {})
        self.conn.execute(
            "INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(snap["ts"]), snap.get("xauusd"), snap.get("day_high"), snap.get("day_low"),
             snap.get("jd_price"), est.get("zheshang"), est.get("minsheng"),
             r.get("30m"), r.get("1h"), r.get("4h"), r.get("1d"), r.get("1w"),
             int(bool(snap.get("market_open")))),
        )
        self.conn.commit()

    def price_minutes_ago(self, minutes: float, tolerance_minutes: float = 5) -> Optional[float]:
        target = utc_now() - timedelta(minutes=minutes)
        row = self.conn.execute(
            "SELECT xauusd FROM ticks WHERE ts BETWEEN ? AND ? AND market_open=1 ORDER BY ts DESC LIMIT 1",
            (iso(target - timedelta(minutes=tolerance_minutes)), iso(target + timedelta(minutes=tolerance_minutes))),
        ).fetchone()
        return row["xauusd"] if row else None

    def add_bank_quote(self, bank: str, price: float, xauusd: float, ratio: float, premium: Optional[float], source: str) -> None:
        self.conn.execute("INSERT INTO bank_quotes VALUES(?,?,?,?,?,?,?)",
                          (iso(utc_now()), bank, price, xauusd, ratio, premium, source))
        self.conn.commit()

    # ── 提醒 ─────────────────────────────────────────────────────────────
    def last_alert_time(self, key: str) -> Optional[datetime]:
        row = self.conn.execute("SELECT ts FROM alerts WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
        return datetime.fromisoformat(row["ts"]) if row else None

    def add_alert(self, key: str, title: str, body: str, delivered: bool) -> None:
        self.conn.execute("INSERT INTO alerts VALUES(?,?,?,?,?)", (iso(utc_now()), key, title, body, int(delivered)))
        self.conn.commit()

    # ── 模拟盘 ───────────────────────────────────────────────────────────
    def open_trade(self, bank: str, strategy: str = "short") -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_trades WHERE bank=? AND strategy=? AND status='open' ORDER BY id DESC LIMIT 1",
            (bank, strategy)).fetchone()

    def last_closed_trade(self, bank: str, strategy: str = "short") -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_trades WHERE bank=? AND strategy=? AND status='closed' ORDER BY id DESC LIMIT 1",
            (bank, strategy)).fetchone()

    def insert_trade(self, *, fill=None, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({marks})", tuple(fields.values()))
        if fill:
            self._insert_paper_fill(cur.lastrowid, **fill)
        self.conn.commit()
        return int(cur.lastrowid)

    def update_trade(self, trade_id: int, *, fill=None, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE paper_trades SET {sets} WHERE id=?", (*fields.values(), trade_id))
        if fill:
            self._insert_paper_fill(trade_id, **fill)
        self.conn.commit()

    def _insert_paper_fill(self, trade_id, side, grams, price, fee, quote, reason):
        self.conn.execute("INSERT INTO paper_fills(trade_id,ts,side,grams,price,fee,quote_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                          (trade_id, iso(utc_now()), side, grams, price, fee, json.dumps(quote, ensure_ascii=False), reason))

    def trade_stats(self, bank: str, strategy: str = "short") -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT realized_cny, grams FROM paper_trades WHERE bank=? AND strategy=? AND status='closed'",
            (bank, strategy)).fetchall()
        pnl = [r["realized_cny"] for r in rows]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        return {
            "trades": len(pnl),
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "total_pnl_cny": round(sum(pnl), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        }

    def paper_balance(self, bank: str, strategy: str, price: Optional[float], fee: float, spread: float) -> Dict[str, Any]:
        acct = self.conn.execute("SELECT * FROM paper_accounts WHERE strategy=?", (strategy,)).fetchone()
        if not acct:
            return {}
        pnl = self.conn.execute("SELECT COALESCE(SUM(realized_cny),0) FROM paper_trades "
                                "WHERE bank=? AND strategy=? AND status='closed'", (bank, strategy)).fetchone()[0]
        trade = self.open_trade(bank, strategy)
        grams = trade["open_grams"] if trade else 0
        cash = acct["budget"] + pnl + (trade["realized_cny"] - grams * trade["entry_cny"] if trade else 0)
        value = cash + grams * (price * (1 - fee) - spread) if price is not None else None
        return {"budget": acct["budget"], "cash": cash, "grams": grams, "value": value,
                "return": value / acct["budget"] - 1 if value is not None else None, "started_at": acct["started_at"]}

    def all_trades(self, bank: str, strategy: str = "short") -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM paper_trades WHERE bank=? AND strategy=? ORDER BY id",
                                 (bank, strategy)).fetchall()
