"""每模型的持久化事件队列；只有成功分析确实包含的事件才确认完成。"""
import json
import uuid
from datetime import datetime, timezone


def read(conn, model):
    row = conn.execute("SELECT value FROM state WHERE key=?", ("ai_events:" + model,)).fetchone()
    return json.loads(row[0]) if row else {"pending": [], "covered": []}


def write(conn, model, state):
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) "
                 "DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 ("ai_events:" + model, json.dumps(state, ensure_ascii=False), stamp))


def enqueue(conn, model, reason, gap, event_id=None, payload=None, *, still_current=None):
    event_id = event_id or str(uuid.uuid4())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if still_current is not None and not still_current(conn):
            return False
        state = read(conn, model)
        if event_id in state["covered"] or any(e["id"] == event_id for e in state["pending"]):
            return False
        state["pending"].append({"id": event_id, "reason": reason, "min_gap_minutes": max(0, gap),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "data": payload or {}})
        write(conn, model, state)
    return True


def acknowledge(conn, model, ids):
    """调用者持有写事务；确认事件与写入判断原子提交。"""
    if not ids:
        return
    state = read(conn, model)
    covered = set(ids)
    state["pending"] = [e for e in state["pending"] if e["id"] not in covered]
    state["covered"] = list(dict.fromkeys(state["covered"] + list(ids)))[-2048:]
    write(conn, model, state)
