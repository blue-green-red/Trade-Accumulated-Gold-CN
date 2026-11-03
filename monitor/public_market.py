"""Anonymous market DTOs from collected public quotes only; no account queries."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import threading
import time

from .public_ai import public_quote, stamp
from .trading_hours import trading_session

_cache = {}
_lock = threading.Lock()


def number(value, positive=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (not positive or value > 0) else None
    except (ValueError, TypeError, OverflowError):
        return None


def quote_data(conn, now):
    row = conn.execute("SELECT value FROM state WHERE key='web_snapshot'").fetchone()
    snap = json.loads(row[0]) if row else {}
    session = trading_session(now)
    value = snap.get('execution_quote') or snap.get('jd_last_quote') or {}
    if value.get('source') != 'jd_latest_price_api':
        value = {}
    price = number(value.get('price'), True)
    fresh = public_quote(value, now) if session['is_open'] else None
    quoted_at = value.get('quote_at')
    try:
        stamp(quoted_at)
    except (ValueError, TypeError):
        quoted_at = None
    cached = bool(value.get('cache_reused'))
    status = ('closed' if not session['is_open'] else 'cached' if fresh and cached else 'fresh' if fresh
              else 'stale' if price is not None else 'unavailable')
    try:
        snapshot_at = snap.get('ts')
        age = (now - stamp(snapshot_at)).total_seconds()
        snapshot_status = 'fresh' if -30 <= age <= 180 else 'stale'
    except (ValueError, TypeError):
        snapshot_at, snapshot_status = None, 'unavailable'
    # Explicit fields only; never forward web_snapshot, bank estimates or errors.
    return {'schema_version': 1,
            'quote': {'price_cny': price, 'quoted_at': quoted_at, 'status': status,
                      'source': '京东积存金通用参考报价',
                      'valid_until': (min(stamp(value[k]) for k in ('quote_at', 'fetched_at')) + timedelta(seconds=120)).isoformat() if fresh else None},
            'session': {k: session[k] for k in ('is_open', 'status', 'next_open', 'close_at', 'rule')},
            'international': {'price_usd_oz': number(snap.get('xauusd'), True),
                              'change_pct': number(snap.get('change_pct')),
                              'as_of': snapshot_at, 'status': snapshot_status},
            'fx': {'usd_cny': number((snap.get('fx') or {}).get('rate'), True)},
            'sampled_at': snapshot_at}


def history_data(conn, range_key, now):
    days = {'1d': 1, '7d': 7, '30d': 30}[range_key]
    since = (now - timedelta(days=days)).isoformat(timespec='seconds')
    bucket = "substr(ts,1,15)" if range_key == '1d' else "substr(ts,1,13)" if range_key == '7d' else "date(ts,'+8 hours')"
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ticks'").fetchone()
    rows = conn.execute("SELECT ts,jd_price FROM ticks WHERE rowid IN (SELECT MAX(rowid) FROM ticks "
                        "WHERE ts>=? AND market_open=1 AND jd_price>0 GROUP BY " + bucket + ") ORDER BY ts", (since,)) if exists else []
    points = [[row[0], price] for row in rows if (price := number(row[1], True)) is not None]
    return {'schema_version': 1, 'range': range_key, 'unit': '元/克', 'points': points,
            'source': '京东通用报价本地采样', 'note': '仅展示有报价时段的采样值，不是官方完整K线。'}


def cached_market(settings, query):
    if set(query) - {'view', 'range'} or any(len(v) != 1 for v in query.values()):
        raise ValueError('Invalid public market query')
    view = query.get('view', ['quote'])[0]
    range_key = query.get('range', ['7d'])[0]
    if view not in ('quote', 'history') or range_key not in ('1d', '7d', '30d') or (view == 'quote' and 'range' in query):
        raise ValueError('Invalid public market view')
    key = (str(settings.db_path.resolve()), view, range_key)
    with _lock:
        saved = _cache.get(key)
        if saved and time.monotonic() < saved[0]:
            return saved[1], saved[2]
        from .web import connect
        now = datetime.now(timezone.utc)
        with closing(connect(settings.db_path)) as conn:
            value = quote_data(conn, now) if view == 'quote' else history_data(conn, range_key, now)
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        tag = 'W/"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        if len(_cache) >= 32:
            _cache.pop(next(iter(_cache)))
        ttl = 5 if view == 'quote' else 60
        if view == 'quote':
            for boundary in (value['quote']['valid_until'], value['session']['close_at']):
                if boundary:
                    ttl = min(ttl, max(0, (stamp(boundary) - now).total_seconds()))
        _cache[key] = (time.monotonic() + ttl, body, tag)
        return body, tag
