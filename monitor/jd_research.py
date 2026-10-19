"""京东黄金结构化参考数据。仅分析时请求，无 LLM/浏览器，缓存跨进程重启有效。"""
from __future__ import annotations

import json
import math
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone, timedelta
from .config import BJ

BASE = "https://youqian.jd.com/api/gateway/gold/"
TTL = 900


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite")
    return result


def items(raw):
    if not raw.get("success") or raw.get("code") != "0000":
        raise ValueError("provider rejected")
    values = raw.get("data")
    if not isinstance(values, list) or not values:
        raise ValueError("empty data")
    return [json.loads(x) if isinstance(x, str) else x for x in values]


def summarize(kind, raw, now):
    rows = items(raw)
    if kind == "funds":
        row = max(rows, key=lambda x: number(x["publishTime"]))
        stamp = number(row["publishTime"]) / 1000
        result = {"long_pct": number(row["longPositionRatio"]),
                  "short_pct": number(row["shortPositionRatio"]),
                  "volume_usd": number(row["tradingVolume"])}
        if any(not 0 <= result[k] <= 100 for k in ("long_pct", "short_pct")) or result["volume_usd"] < 0:
            raise ValueError("invalid ratios")
    else:
        row = max(rows, key=lambda x: number(x["time"]))
        stamp = number(row["time"])
        levels = row["levels"]
        levels = json.loads(levels) if isinstance(levels, str) else levels
        result = {"reference_price": number(row["price"]), "price_unit": "provider_unspecified",
                  "note": "非京东积存金成交盘口；仅比较占比，不换算或直接用作成交价"}
        for key, field in (("long_clusters", "ol"), ("short_clusters", "os")):
            parsed = [{"price": number(x["price"]), "pct": number(x[field])} for x in levels]
            if any(x["price"] <= 0 or not 0 <= x["pct"] <= 100 for x in parsed):
                raise ValueError("invalid levels")
            result[key] = sorted(parsed, key=lambda x: x["pct"], reverse=True)[:3]
    result["published_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
    age = now.timestamp() - stamp
    # 资金炸弹是离散事件，最多提供最近 24h；盘口仅允许最近一小时。
    result["status"] = "fresh" if -300 <= age <= (86400 if kind == "funds" else 3600) else "stale"
    if result["status"] == "stale":
        return {"status": "stale", "published_at": result["published_at"]}
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS jd_research_requests (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, endpoint TEXT NOT NULL, day_bj TEXT NOT NULL,
 started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, error_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_jd_request_day ON jd_research_requests(day_bj,id);
"""
CONTROL = "jd_research:control"


class ProviderError(ValueError):
    def __init__(self, status, error_type):
        super().__init__(error_type)
        self.status, self.error_type = status, error_type


def provider_error(raw, http_code=None):
    """Classify provider responses without persisting their text or credentials."""
    code=str(raw.get("code",http_code or ""))
    text=" ".join(str(raw.get(k,"")) for k in ("message","msg","error"))
    daily=any(k in text for k in ("今日","每日","当天","daily")) and any(k in text for k in ("额度","配额","上限","quota","limit"))
    if daily: return ProviderError("quota_exhausted","ProviderDailyQuota")
    if code=="429" or http_code==429: return ProviderError("rate_limited","ProviderRateLimit")
    if code in ("401","403") or http_code in (401,403): return ProviderError("unavailable","ProviderUnauthorized")
    return ProviderError("unavailable","ProviderRejected")


def fetch(endpoint, key):
    request = urllib.request.Request(BASE + endpoint, data=b"{}", headers={
        "Content-Type": "application/json", "apikey": key,
        "x-skill-code": "gold-analysis-hub", "x-skill-run-id": str(uuid.uuid4()),
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw=json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        try: raw=json.loads(exc.read(1024 * 1024))
        except (ValueError,TypeError): raw={}
        raise provider_error(raw if isinstance(raw,dict) else {},exc.code) from None
    if not isinstance(raw,dict): raise ValueError("invalid response object")
    if not raw.get("success") or str(raw.get("code"))!="0000": raise provider_error(raw)
    return raw


def _state(conn,key):
    row=conn.execute("SELECT value FROM state WHERE key=?",(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def _write(conn,key,value,now):
    conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (key,json.dumps(value,ensure_ascii=False),now.isoformat()))


def _age(stamp,now):
    try: return (now-datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError,ValueError): return float('inf')


def quota_view(conn,daily_cap,now):
    cap=max(0,int(daily_cap));day=now.astimezone(BJ).date().isoformat()
    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_research_requests'").fetchone()
    used=conn.execute("SELECT COUNT(*) FROM jd_research_requests WHERE day_bj=?",(day,)).fetchone()[0] if exists else 0
    since=conn.execute("SELECT MIN(started_at) FROM jd_research_requests").fetchone()[0] if exists else None
    gate=_state(conn,CONTROL)
    blocked=gate if _age(gate.get("retry_at"),now)<0 else {}
    reset=(now.astimezone(BJ).replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    return {"used_today":used,"daily_cap":cap,"remaining":max(0,cap-used),"tracked_since":since,
            "status":blocked.get("status") or ("budget_exhausted" if used>=cap else "available"),
            "retry_at":blocked.get("retry_at") or (reset if used>=cap else None),"local_reset_at":reset,
            "scope":"本地后台两个参考接口合计；不包含历史或外部调用，不是供应商剩余额度"}


def public_entry(conn,kind,now,quota=None):
    """One freshness/error policy shared by model input and the read-only dashboard."""
    cached=_state(conn,"jd_research:"+kind)
    original=cached.get("summary") or ({} if cached.get("raw") else {"status":"not_fetched"})
    last_success=cached.get("last_success_at") or (cached.get("fetched_at") if cached.get("raw") else None)
    attempted=cached.get("attempted_at") or cached.get("fetched_at")
    if original.get("status")=="refreshing" and not -300<=_age(attempted,now)<TTL:
        original={"status":"unavailable","error_type":"RequestInterrupted"}
    summary=dict(original)
    if cached.get("raw"):
        try:
            summary=summarize(kind,cached["raw"],now)
            if summary["status"]=="fresh" and not 0<=_age(last_success,now)<=3600:
                summary={"status":"cache_stale","published_at":summary.get("published_at")}
        except (KeyError,TypeError,ValueError,OverflowError): summary={"status":"unavailable","error_type":"InvalidCachedData"}
    refresh=original.get("status") if original.get("status") not in ("fresh","stale") else None
    retry=None
    if quota and quota["status"]!="available": refresh=quota["status"];retry=quota["retry_at"]
    if summary.get("status")!="fresh" and refresh in ("quota_exhausted","rate_limited","budget_exhausted"):
        summary={"status":refresh,"published_at":summary.get("published_at")}
    # A failed attempt must not become stale just because time passed.
    return {**summary,"fetched_at":last_success,"attempted_at":attempted,
            "refresh_status":refresh,"retry_at":retry,"error_type":({"quota_exhausted":"ProviderDailyQuota","rate_limited":"ProviderRateLimit","budget_exhausted":"LocalDailyBudget"}.get(refresh) or original.get("error_type") or summary.get("error_type"))}


def _claim(store,kind,endpoint,now,daily_cap):
    """Persist reservation before dispatch; concurrent models/processes share the cap/cache."""
    c=store.conn
    with c:
        c.execute("BEGIN IMMEDIATE")
        cached=_state(c,"jd_research:"+kind)
        if -300<=_age(cached.get("attempted_at") or cached.get("fetched_at"),now)<TTL: return None
        if quota_view(c,daily_cap,now)["status"]!="available": return None
        stamp=now.isoformat(timespec="microseconds")
        cur=c.execute("INSERT INTO jd_research_requests(kind,endpoint,day_bj,started_at,status) VALUES(?,?,?,?,'running')",
                      (kind,endpoint,now.astimezone(BJ).date().isoformat(),stamp))
        cached.update({"attempted_at":stamp,"summary":{"status":"refreshing"}})
        _write(c,"jd_research:"+kind,cached,now)
        return cur.lastrowid


def snapshot(store, api_key, now=None, daily_cap=10):
    now = now or datetime.now(timezone.utc)
    store.conn.executescript(SCHEMA)
    for kind,endpoint in (("funds","bomb"),("orders","order-book")):
        if not api_key:
            with store.conn: _write(store.conn,"jd_research:"+kind,{"summary":{"status":"unconfigured"}},now)
            continue
        attempt=_claim(store,kind,endpoint,now,daily_cap)
        if attempt is None: continue
        state_key="jd_research:"+kind
        entry=_state(store.conn,state_key)
        try:
            raw=fetch(endpoint,api_key)
            # Also validate mocked responses and provider-level errors returned with HTTP 200.
            if not raw.get("success") or str(raw.get("code"))!="0000": raise provider_error(raw)
            values=raw.get("data")
            if isinstance(values,list) and not values:
                summary={"status":"no_data"}
            else: summary=summarize(kind,raw,now)
            entry.update({"fetched_at":now.isoformat(),"last_success_at":now.isoformat(),"summary":summary})
            if summary["status"]!="no_data": entry["raw"]=raw
            else: entry.pop("raw",None)
            status,error_type=summary["status"],None
        except Exception as exc:
            status=exc.status if isinstance(exc,ProviderError) else "unavailable"
            error_type=exc.error_type if isinstance(exc,ProviderError) else type(exc).__name__
            entry["summary"]={"status":status,"error_type":error_type}
        with store.conn:
            store.conn.execute("BEGIN IMMEDIATE")
            if status in ("quota_exhausted","rate_limited"):
                retry=now+timedelta(seconds=86400 if status=="quota_exhausted" else TTL)
                _write(store.conn,CONTROL,{"status":status,"retry_at":retry.isoformat()},now)
            _write(store.conn,state_key,entry,now)
            store.conn.execute("UPDATE jd_research_requests SET completed_at=?,status=?,error_type=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(),status,error_type,attempt))
    quota=quota_view(store.conn,daily_cap,now)
    return {"source":"京东金融有钳","url":"https://youqian.jd.com/","quota":quota,
            **{kind:public_entry(store.conn,kind,now,quota) for kind in ("funds","orders")}}
