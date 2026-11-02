"""Page-specific, read-only dashboard queries. No market requests or model calls."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

from .config import Settings
from .controls import control, identity, daily_keys, short_labels
from .autonomous import keys as portfolio_keys, summaries, enabled as autonomous_enabled, exists as portfolios_exist, net_price
from .model_visibility import dashboard_visible, hidden_models
from .performance import autonomous_comparison, comparison, pnl_stats
from .execution import EXECUTION_NAME, execution_quote
from .web import connect, get_state, table_exists, equity_curves, leaderboard, short_paper, ai_section, jd_reference, price_history, history_page

PAGES = frozenset(('overview','strategies','compare','ai','market','records','notifications','scorecard','diagnostics'))
MARKET_VIEWS = ('auto','prices','news','calendar','reference')


def validate_page(page,view):
    allowed = MARKET_VIEWS if page == 'market' else ('auto','live','simulation') if page == 'strategies' else ('auto',)
    if page not in PAGES or view not in allowed:
        raise ValueError('invalid page or view')
_cache = {}
_locks = {}
_guard = threading.Lock()
CACHE_SECONDS = 2


def invalidate_pages(settings):
    prefix = str(settings.db_path.resolve())
    with _guard:
        for key in list(_cache):
            if key[0] == prefix:
                del _cache[key]
        # In-flight readers are prevented from refilling an invalidated generation.
        _epochs[prefix] = _epochs.get(prefix, 0) + 1


_epochs = {}


def cached_page(settings, page, view='auto'):
    validate_page(page,view)
    prefix = str(settings.db_path.resolve())
    key = (prefix, page, view)
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        with _guard:
            epoch = _epochs.get(prefix, 0)
            saved = _cache.get(key)
        if saved and time.monotonic()-saved[0] < CACHE_SECONDS:
            return saved[1], saved[2]
        result = page_data(settings, page, view)
        body = json.dumps(result, ensure_ascii=False, separators=(',',':'), default=str).encode()
        # Retrieval timestamps are metadata; freshness statuses and quote timestamps remain in the validator.
        semantic = dict(result)
        semantic.pop('generated_at', None)
        if 'wave_evidence' in semantic:
            semantic['wave_evidence'] = {k:v for k,v in semantic['wave_evidence'].items() if k != 'as_of'}
        tag = 'W/"'+hashlib.sha256(json.dumps(semantic,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()[:24]+'"'
        with _guard:
            if epoch == _epochs.get(prefix, 0):
                if len(_cache) >= 32:
                    _cache.clear()
                _cache[key] = (time.monotonic(), body, tag)
        return body, tag


def _controls(conn, settings):
    from .manual_ai import status as manual_status
    from .cadence import usage
    from .ai_events import read
    now = datetime.now(timezone.utc)
    return [{'key':m,'name':m.title(),**control(conn,m),**(get_state(conn,'ai_status:'+m) or {}),
             'runtime':get_state(conn,'ai_runtime:'+m) or {},'manual':manual_status(conn,m),
             'runs_today':usage(conn,m,now),'daily_cap':settings.ai_max_runs_per_day,
             'urgent_reserve':settings.ai_urgent_reserve,'pending_events':len(read(conn,m)['pending'])}
            for m in settings.ai_analysts if m in ('claude','codex')]


def _wave(conn, snap, view='auto'):
    from .wave_evidence import daily_reference, jd_daily_samples
    from .macro_cache import age_seconds
    now = datetime.now(timezone.utc)
    indicator = dict(snap.get('daily_indicators') or {'status':'unavailable'})
    if indicator.get('status') in ('fresh','partial') and not -30 <= age_seconds(indicator.get('fetched_at'),now) <= 180:
        indicator['status'] = 'stale'
    result = {'as_of':now.isoformat()}
    if view in ('auto','prices'):
        sampled = jd_daily_samples(conn,now)
        # Coverage diagnostics remain in AI inputs; the chart needs only dated prices.
        jd_daily = {k:sampled.get(k) for k in ('status','source','timezone','as_of')}
        jd_daily['days'] = [{k:row.get(k) for k in ('date_bj','last_price_cny')} for row in sampled.get('days',[])]
        result['jd_daily'] = jd_daily
    if view in ('auto','reference'):
        result.update(daily_reference=daily_reference(conn,now),daily_indicators=indicator)
    return result


def _active_keys(conn, settings):
    hidden = hidden_models(conn)
    return [k for k in portfolio_keys(settings) if identity(k)[0] not in hidden]


def _plans(conn, settings):
    keys = _active_keys(conn, settings)
    if not keys or not portfolios_exist(conn):
        return {'plans':[],'recent_expired':[]}
    marks = ','.join('?' for _ in keys)
    now = datetime.now(timezone.utc).isoformat()
    sql = f'SELECT * FROM ai_plans WHERE account IN ({marks})'
    active = conn.execute(sql+" AND status='pending' ORDER BY id DESC",keys).fetchall()
    expired = conn.execute(sql+" AND (status='expired' OR (status='pending' AND julianday(expires_at)<=julianday(?)) OR "
        "(status='executed' AND json_extract(decision_json,'$.action') IN ('hold','wait','cancel') AND julianday(expires_at)<=julianday(?))) "
        "ORDER BY julianday(expires_at) DESC,id DESC LIMIT 6",[*keys,now,now]).fetchall()
    def public(row):
        result = {k:row[k] for k in ('id','account','view_id','created_at','expires_at','status','reason')}
        result['decision'] = json.loads(row['decision_json'])
        return result
    return {'plans':[public(r) for r in active], 'recent_expired':[public(r) for r in expired]}


def _fills(conn, settings):
    keys = _active_keys(conn, settings)
    if not autonomous_enabled(settings):
        return history_page(settings, {'kind':['fills'],'limit':['4']})['items']
    if not keys or not portfolios_exist(conn):
        return []
    rows = conn.execute("SELECT f.*,v.view_json,v.run_id FROM ai_portfolio_fills f LEFT JOIN ai_views v ON v.rowid=f.view_id "
        "WHERE f.account IN ("+','.join('?' for _ in keys)+") AND f.reason NOT LIKE '实验初始化%' ORDER BY f.id DESC LIMIT 4",keys)
    result = []
    for r in rows:
        view = json.loads(r['view_json']) if r['view_json'] else {}
        value = {k:r[k] for k in ('id','account','ts','side','grams','price','fee','pnl','reason')}
        value['decision'] = {'analyst':identity(r['account'])[0],'version':view.get('_trading_version'),
            'view':{'summary':r['reason'],'_view_id':r['view_id'],'_run_id':r['run_id']},
            'execution_quote':json.loads(r['quote_json'])}
        result.append(value)
    return result


def _totals(conn, settings, accounts):
    models = []
    for model in settings.ai_analysts:
        owned = [a for a in accounts if a.get('model') == model]
        if not owned:
            continue
        budget = sum(a['budget'] for a in owned)
        starts = [a['started_at'] for a in owned if a['started_at']]
        value = sum(a['value'] for a in owned) if all(a['value'] is not None for a in owned) else None
        pnls = [r[0] for r in conn.execute('SELECT pnl FROM ai_portfolio_cycles WHERE account IN ('+
            ','.join('?' for _ in owned)+') AND closed_at IS NOT NULL',[a['key'] for a in owned])]
        models.append({'key':model,'name':model.title(),'budget':budget,'value':value,
            'return':value/budget-1 if starts and value is not None and budget else None,
            'started_at':min(starts) if starts else None,**pnl_stats(pnls)})
    return models


def _notifications(conn, settings):
    from .notification_control import visible_subscriptions
    from .notification_outbox import visible_tasks
    return {'configured':bool(settings.pushplus_token or settings.serverchan_sendkey),
        'channel':'PushPlus' if settings.pushplus_token else 'Server酱' if settings.serverchan_sendkey else None,
        'subscriptions':visible_subscriptions(conn,settings.ai_trading_mode),
        'deliveries':[dict(r) for r in conn.execute('SELECT id,ts,category,title,channel,status,provider_id,provider_code,event_keys_json FROM notification_deliveries ORDER BY id DESC LIMIT 10')],
        'outbox':visible_tasks(conn,settings.ai_trading_mode),
        'recent':[dict(r) for r in conn.execute("SELECT ts,key,title,delivered FROM alerts WHERE delivered=1 OR key LIKE 'live-advice:%' ORDER BY rowid DESC LIMIT 20")]}


def page_data(settings: Settings, page, view='auto'):
    validate_page(page,view)
    with closing(connect(settings.db_path)) as conn:
        snap = get_state(conn,'web_snapshot') or {}
        quote = execution_quote(snap)
        valuation = quote or snap.get('jd_last_quote')
        price = valuation['price'] if valuation else None
        result = {'page':page,'generated_at':datetime.now(timezone.utc).isoformat(),
            'trading_mode':settings.ai_trading_mode,'model_controls':_controls(conn,settings),
            'snapshot':{k:snap.get(k) for k in ('ts','market_open','jd_session')},
            'bank':settings.paper_bank,'bank_name':EXECUTION_NAME,'sell_fee_rate':settings.sell_fee_rate}
        if page == 'overview' or (page == 'market' and view in ('auto','prices')):
            result['snapshot'] = {k:snap.get(k) for k in ('ts','market_open','jd_session','xauusd','change_pct','day_high','day_low','fx')}
            result['execution'] = {'quote':quote,'valuation_quote':valuation,'ready':quote is not None}
        if page in ('overview','strategies'):
            from .jd_live_readonly import accounts as live_accounts
            live = live_accounts(conn)
            if page == 'overview':
                result['live_readonly'] = [{**{k:a.get(k) for k in ('bank','name','connected','status','fetched_at','pending_order_count')},
                    'advice':[{**{k:v.get(k) for k in ('analyst','action','status','expires_at')},
                               'suggested_order_count':len(v.get('suggested_orders',[]))} for v in a.get('advice',[])]} for a in live]
            else:
                result['account_view'] = ('live' if any(a.get('fetched_at') for a in live) else 'simulation') if view == 'auto' else view
                if result['account_view'] == 'live':
                    result.update(live_readonly=live,live_readonly_sync=get_state(conn,'jd_live_sync_status') or {},
                                  live_readonly_refresh=get_state(conn,'jd_live_refresh') or {})
        sim_accounts = page == 'strategies' and result['account_view'] == 'simulation'
        if page in ('overview','compare','ai') or sim_accounts:
            accounts = summaries(conn,settings,price) if autonomous_enabled(settings) and portfolios_exist(conn) else []
            if page == 'overview':
                result['comparison'] = {'models':_totals(conn,settings,accounts)}
                result['autonomous'] = _plans(conn,settings)
                result['fills'] = _fills(conn,settings)
                result['hard_events_24h'] = (get_state(conn,'web_macro') or {}).get('hard_events_24h') or []

            elif page == 'ai':
                result['autonomous'] = {'accounts':accounts}
                section = ai_section(conn,settings)
                result['ai'] = {'enabled':section['enabled'],'analysts':section['analysts']}
                result['ai_inputs'] = get_state(conn,'ai_inputs')
                result['cadence'] = {'review_minutes':settings.ai_short_review_minutes,'min_move_pct':settings.ai_periodic_min_move_pct,'max_idle_minutes':settings.ai_review_max_idle_minutes}
            else:
                board = leaderboard(conn,settings,price)
                short = [short_paper(conn,settings.paper_bank,k,price,settings.sell_fee_rate,settings.spread_cny,label) for k,label in short_labels(settings).items()]
                curves = equity_curves(conn,settings)
                cmp = autonomous_comparison(conn,settings,board,short,curves,price,accounts=accounts) if autonomous_enabled(settings) else comparison(conn,board,short,curves)
                result.update(comparison=cmp,equity=curves)
                if sim_accounts:
                    result.update(leaderboard=board+accounts,short=short)
        if page in ('overview','strategies','ai','records'):
            from .strategies import REGISTRY
            result['strategy_names'] = {**{k:c.name for k,c in REGISTRY.items() if k in daily_keys(settings)},**short_labels(settings),**portfolio_keys(settings)}
        if page == 'market':
            result['market_view'] = view
            if view in ('auto','prices','reference'):
                result['wave_evidence'] = _wave(conn,snap,view)
            if view in ('auto','prices'):
                result['prices'] = price_history(conn)
            if view in ('auto','calendar','reference'):
                macro = get_state(conn,'web_macro') or {}
                fields = (('calendar','calendar_fetched_at','calendar_refresh_seconds','fetched_at') if view == 'calendar'
                          else ('quotes','quotes_fetched_at','fetched_at') if view == 'reference' else tuple(macro))
                result['macro'] = {k:macro[k] for k in fields if k in macro}
                if view == 'calendar' and 'calendar' in result['macro']:
                    from .wave_evidence import calendar_window
                    result['macro']['calendar'] = calendar_window(result['macro']['calendar'],datetime.now(timezone.utc))['events']
                if view in ('calendar','reference'):
                    kind = 'calendar' if view == 'calendar' else 'quotes'
                    result['macro']['refresh_errors'] = {kind:(macro.get('refresh_errors') or {}).get(kind)}
            if view in ('auto','reference'):
                research = jd_reference(conn,settings)
                if view == 'reference':
                    allowed = ('status','long_pct','short_pct','volume_usd','long_clusters','short_clusters','published_at','fetched_at')
                    research = {kind:{k:part[k] for k in allowed if k in part} for kind in ('funds','orders') for part in [research.get(kind) or {}]}
                result['jd_research'] = research
            if view in ('auto','news'):
                result['jin10'] = (get_state(conn,'jin10_flash') or [])[:30]
                if view == 'auto':
                    ids = {x.get('id') for x in result['jin10']}
                    result['jin10_triage'] = {k:v for k,v in (get_state(conn,'jin10_triage') or {}).items() if k in ids}
        if page == 'diagnostics':
            result['jd_research'] = jd_reference(conn,settings)
        if page == 'notifications':
            result['notifications'] = _notifications(conn,settings)
        if page == 'scorecard':
            from .scorecard import evaluate
            result['scorecard'] = evaluate(conn,visible_models=[m for m in settings.ai_analysts if m not in hidden_models(conn)])
        return dashboard_visible(conn,result)
