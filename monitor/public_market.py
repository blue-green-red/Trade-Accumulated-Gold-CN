"""Anonymous market DTOs from collected public quotes only; no account queries."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import threading
import time
from email.utils import parsedate_to_datetime

from .public_ai import public_quote, stamp
from .trading_hours import trading_session
from .config import BJ

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
    international_price = number(snap.get('xauusd'), True)
    if international_price is None: snapshot_status = 'unavailable'
    fx = snap.get('fx') or {}
    try:
        fx_fetched = stamp(fx.get('fetched_at')).isoformat()
    except (ValueError, TypeError): fx_fetched = None
    try:
        raw_date = fx.get('rate_date')
        try: fx_date = datetime.fromisoformat(raw_date).date().isoformat()
        except ValueError: fx_date = parsedate_to_datetime(raw_date).date().isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError): fx_date = None
    # Explicit fields only; never forward web_snapshot, bank estimates or errors.
    return {'schema_version': 1,
            'quote': {'price_cny': price, 'quoted_at': quoted_at, 'status': status,
                      'source': '京东积存金通用参考报价',
                      'valid_until': (min(stamp(value[k]) for k in ('quote_at', 'fetched_at')) + timedelta(seconds=120)).isoformat() if fresh else None},
            'session': {k: session[k] for k in ('is_open', 'status', 'next_open', 'close_at', 'rule')},
            'international': {'price_usd_oz': international_price,
                              'change_pct': number(snap.get('change_pct')) if international_price is not None else None,
                              'as_of': snapshot_at, 'status': snapshot_status},
            'fx': {'usd_cny': number(fx.get('rate'), True),
                   'rate_date': fx_date, 'fetched_at': fx_fetched, 'frequency': 'daily'},
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


def state(conn, key):
    row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def freshness(value, now, seconds):
    try: return 'fresh' if -30 <= (now - stamp(value)).total_seconds() <= seconds else 'stale'
    except (ValueError, TypeError): return 'unavailable'


def news_data(conn, now):
    items, seen = [], set()
    for row in sorted(state(conn, 'jin10_flash') or [], key=lambda r: str(r.get('time_bj', '')), reverse=True):
        try:
            at = datetime.strptime(row['time_bj'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ)
            if not timedelta(seconds=-30) <= now - at <= timedelta(hours=24): continue
            text = str(row['text']).strip()[:1200]
        except (ValueError, TypeError, KeyError): continue
        signature = ''.join(text.split())
        if not signature or signature in seen: continue
        seen.add(signature)
        items.append({'id': hashlib.sha256((at.isoformat()+text).encode()).hexdigest()[:20],
                      'at': at.isoformat(), 'important': row.get('important') is True, 'text': text})
        if len(items) >= 80: break
    return {'schema_version': 1, 'source': '金十数据', 'items': items,
            'latest_at': items[0]['at'] if items else None, 'window_hours': 24}


def calendar_data(conn, now):
    from .wave_evidence import calendar_window
    macro = state(conn, 'web_macro') or {}
    window = calendar_window(macro.get('calendar'), now, limit=80)
    rows = []
    for row in window['events']:
        if row.get('country') not in ('US', 'CN') or row.get('tier') not in (1, 2): continue
        at = stamp(row['time_utc']); released = at <= now and row.get('released') is True
        def value(key):
            val = row.get(key)
            if isinstance(val, bool) or val is None: return None
            if isinstance(val, str): return val[:80]
            return number(val)
        rows.append({'at': at.isoformat(), 'country': row['country'], 'title': str(row.get('title', ''))[:160],
                     'tier': row['tier'], 'released': released, 'actual': value('actual') if released else None,
                     'forecast': value('forecast'), 'previous': value('previous')})
    fetched = macro.get('calendar_fetched_at') or macro.get('fetched_at')
    return {'schema_version': 1, 'source': 'TradingView 经济日历', 'items': rows, 'fetched_at': fetched,
            'status': freshness(fetched, now, 2100), 'days_ahead': 7}


def indicators_data(conn, now):
    macro, snap = state(conn, 'web_macro') or {}, state(conn, 'web_snapshot') or {}
    specs = (('美元指数', '点', 2, '观察美元相对强弱，与黄金的关系会随阶段变化。'),
             ('美国10年期国债收益率', '%', 3, '长期美元利率环境，影响持有黄金的机会成本。'),
             ('美国2年期国债收益率', '%', 3, '对近期货币政策预期较敏感。'),
             ('离岸人民币 USD/CNH', 'USD/CNH', 4, '观察离岸人民币汇率，不等于银行积存金换算公式。'))
    quotes = []
    for name, unit, digits, note in specs:
        row = (macro.get('quotes') or {}).get(name) or {}
        quotes.append({'name': name, 'value': number(row.get('value'), name in ('美元指数', '离岸人民币 USD/CNH')), 'change_pct': number(row.get('change_pct')),
                       'unit': unit, 'digits': digits, 'note': note})
    fetched = macro.get('quotes_fetched_at') or macro.get('fetched_at')
    indicator = snap.get('daily_indicators') or {}
    technical = {'rsi': number(indicator.get('rsi')), 'ma20_usd': number(indicator.get('ma20_usd'), True),
                 'ma50_usd': number(indicator.get('ma50_usd'), True), 'fetched_at': indicator.get('fetched_at'),
                 'status': freshness(indicator.get('fetched_at'), now, 180), 'source': 'TradingView 国际现货黄金日线'}
    if technical['rsi'] is not None and not 0 <= technical['rsi'] <= 100: technical['rsi'] = None
    if not any(technical[k] is not None for k in ('rsi', 'ma20_usd', 'ma50_usd')):
        technical['status'] = 'unavailable'
    elif technical['status'] == 'fresh' and any(technical[k] is None for k in ('rsi', 'ma20_usd', 'ma50_usd')):
        technical['status'] = 'partial'
    macro_status = freshness(fetched, now, 420)
    if all(q['value'] is None for q in quotes): macro_status = 'unavailable'
    elif macro_status == 'fresh' and any(q['value'] is None for q in quotes): macro_status = 'partial'
    return {'schema_version': 1, 'quotes': quotes, 'fetched_at': fetched, 'status': macro_status,
            'technical': technical, 'note': '均线单位为美元/盎司；日线指标盘中会变化，不能直接当作京东人民币价位。'}


def cached_market(settings, query):
    if set(query) - {'view', 'range'} or any(len(v) != 1 for v in query.values()):
        raise ValueError('Invalid public market query')
    view = query.get('view', ['quote'])[0]
    range_key = query.get('range', ['7d'])[0]
    if view not in ('quote', 'history', 'news', 'calendar', 'indicators') or range_key not in ('1d', '7d', '30d') or (view != 'history' and 'range' in query):
        raise ValueError('Invalid public market view')
    key = (str(settings.db_path.resolve()), view, range_key)
    with _lock:
        saved = _cache.get(key)
        if saved and time.monotonic() < saved[0]:
            return saved[1], saved[2]
        from .web import connect
        now = datetime.now(timezone.utc)
        with closing(connect(settings.db_path)) as conn:
            value = (history_data(conn, range_key, now) if view == 'history' else
                     {'quote': quote_data, 'news': news_data, 'calendar': calendar_data, 'indicators': indicators_data}[view](conn, now))
        body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        tag = 'W/"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        if len(_cache) >= 32:
            _cache.pop(next(iter(_cache)))
        ttl = 5 if view == 'quote' else 15 if view in ('news', 'calendar') else 60
        if view == 'quote':
            for boundary in (value['quote']['valid_until'], value['session']['close_at']):
                if boundary:
                    ttl = min(ttl, max(0, (stamp(boundary) - now).total_seconds()))
        _cache[key] = (time.monotonic() + ttl, body, tag)
        return body, tag
