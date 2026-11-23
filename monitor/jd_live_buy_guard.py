"""Verify real-bank entry triggers against quotes received while the model ran."""
from collections import defaultdict
from datetime import datetime

from .jd_live_buy_watch import MAX_GAP, sample, utc_text
from .jd_live_conditions import stamp


ENTRY_EVENT_PREFIX = 'live-buy-confirmed:'


def reviewed_breakout(conn, model, account, threshold, now):
    """Return the previously reviewed breakout while its quote sequence still holds."""
    from .controls import control
    from .price_guard import read
    receipt = read(conn, f"live-breakout-reviewed:{model}:{account['bank']}")
    if (receipt.get('threshold_cny') != threshold or receipt.get('revision') != account.get('revision')
            or receipt.get('generation') != control(conn, model)['generation']):
        return None
    return receipt if active_routes(conn, account, [receipt], now) else None


def remember_reviewed_breakouts(conn, model, context, event_ids, generation):
    """Record the reviewed market move independently of the other routes in its plan."""
    from .price_guard import read, save
    ids = set(event_ids)
    inputs = (context.get('live_readonly_accounts') or {}).get('accounts', {})
    for event in context.get('trigger_events', []):
        if event['id'] not in ids or not event['id'].startswith(ENTRY_EVENT_PREFIX):
            continue
        data = event['data']
        bank = data['bank']
        revision = (inputs.get(bank) or {}).get('revision')
        if not revision:
            continue
        key = f'live-breakout-reviewed:{model}:{bank}'
        previous = read(conn, key)
        for route in data.get('routes', []):
            if not route.get('bank_breakout') or not (confirmed := stamp(route.get('confirmed_at'))):
                continue
            old = stamp(previous.get('confirmed_at'))
            if (old and old >= confirmed and previous.get('generation') == generation
                    and previous.get('revision') == revision):
                continue
            previous = {'threshold_cny': route['threshold_cny'], 'confirmed_at': route['confirmed_at'],
                        'revision': revision, 'generation': generation, 'event_id': event['id']}
            save(conn, key, previous)


def active_routes(conn, account, routes, now):
    point = sample(account, now)
    anchored = [(route, anchor) for route in routes if (anchor := stamp(route.get('confirmed_at')))]
    if not anchored or not point:
        return []
    tape = dict(conn.execute(
        'SELECT quoted_at,buy_price_cny FROM jd_live_quote_samples '
        'WHERE bank=? AND quoted_at>? AND quoted_at<=? AND observed_at<=? ORDER BY quoted_at',
        (account['bank'], utc_text(min(anchor for _, anchor in anchored)), utc_text(point[0]), utc_text(now))))
    tape[utc_text(point[0])] = point[2]
    quotes = [(stamp(at), price) for at, price in sorted(tape.items())]
    return [route for route, anchor in anchored if route_valid(route, anchor, quotes, point[0])]


def submitted_routes(context, event_ids):
    by_bank = defaultdict(list)
    ids = set(event_ids)
    for event in context.get('trigger_events', []):
        if event['id'] in ids and event['id'].startswith(ENTRY_EVENT_PREFIX):
            by_bank[event['data']['bank']].extend(event['data'].get('routes', []))
    return by_bank


def review_failures(conn, context, event_ids, now):
    """Return a bank warning when every included entry route has lost its basis."""
    by_bank = submitted_routes(context, event_ids)
    if not by_bank:
        return {}

    from .jd_live_readonly import accounts
    current = {a['bank']: a for a in accounts(conn, now, include_advice=False)}
    source = (context.get('live_readonly_accounts') or {}).get('accounts', {})
    warnings = {}
    for bank, routes in by_bank.items():
        account = current.get(bank) or {}
        point = sample(account, now)
        if not routes or not point or account.get('revision') != (source.get(bank) or {}).get('revision'):
            warnings[bank] = '分析完成时无法核对本行最新报价或账户状态，原买入触发需重新确认'
            continue
        anchors = [stamp(route.get('confirmed_at')) for route in routes]
        if not any(anchors):
            warnings[bank] = '原买入触发缺少确认时间，需用新报价重新评估'
            continue
        if not active_routes(conn, account, routes, now):
            warnings[bank] = '分析期间买入触发已失守、超价或报价中断，须按新行情复评'
    return warnings


def route_valid(route, anchor: datetime, quotes, latest_at):
    if latest_at < anchor:
        return False
    low = route['threshold_cny']
    high = None
    if setup := route.get('buy_review'):
        low, high = setup['recovery_price_cny'], setup['max_buy_price_cny']
    previous = anchor
    for at, price in quotes:
        if at < anchor:
            continue
        if at - previous > MAX_GAP or price < low or (high is not None and price > high):
            return False
        previous = at
    return True


def confirmation_key(route):
    return route.get('condition_key'), route.get('confirmed_at')


def with_active_routes(event, routes):
    if routes == event['data']['routes']:
        return event
    latest = max(routes, key=lambda route: stamp(route['confirmed_at']))
    label = '突破后持续走高' if any(r.get('confirmation_mode') == 'continuation' for r in routes) else '观察条件已企稳'
    from .jd_live_readonly import PRODUCTS
    bank = event['data']['bank']
    return {**event, 'data': {**event['data'], 'routes': routes,
                             'price_cny': latest.get('price_cny'), 'quoted_at': latest['confirmed_at']},
            'reason': f'真实账户{label}，重新评估：{PRODUCTS[bank][1]}'}


def matching_entry_event(event, current):
    """Keep only submitted confirmations that still exist in the live queue."""
    if not current:
        return None
    active = {confirmation_key(route) for route in current['data']['routes']}
    routes = [route for route in event['data']['routes'] if confirmation_key(route) in active]
    if not routes:
        return None
    return with_active_routes(event, routes)


def retire_failed_events(conn, model, context, event_ids, failures):
    """Discard the submitted stale observation while keeping a newer attempt."""
    from .ai_events import read, write

    def confirmations(event):
        return {confirmation_key(r) for r in event['data'].get('routes', [])}

    failed = {event['id']: confirmations(event) for event in context.get('trigger_events', [])
              if event['id'] in event_ids and event['id'].startswith(ENTRY_EVENT_PREFIX)
              and event['data']['bank'] in failures}
    if not failed:
        return event_ids
    events = read(conn, model)
    retained = [e for e in events['pending']
                if e['id'] not in failed or not confirmations(e).issubset(failed[e['id']])]
    if retained != events['pending']:
        events['pending'] = retained
        write(conn, model, events)
    return [event_id for event_id in event_ids if event_id not in failed]


def block_entries(value, warning, *, include_reviews=True):
    """Keep protective actions and the archived model output, and flag stale buys."""
    if value['action'] == 'buy':
        value['checks'].append(warning)
    groups = (
        ('suggested_orders', '条件单建议：', lambda item: item['kind'] == 'limit_buy'),
        ('limit_buy_reviews', '限价评估：', lambda item: item['decision'] in ('recommend', 'needs_review')),
        ('review_conditions', '复评条件：', lambda item: include_reviews and bool(item.get('buy_review'))),
        ('order_management', '挂单建议：', lambda item: item['action'] == 'replace' and
         (item.get('effective_replacement') or {}).get('action') == 'buy'),
    )
    for field, prefix, affects_entry in groups:
        for item in value.get(field, []):
            if affects_entry(item):
                item.setdefault('checks', []).append(warning)
                value['checks'].append(prefix + warning)


def current_entry_state(conn, account, advice, now):
    if advice['lifecycle_status'] != 'current':
        return
    routes = advice.get('entry_trigger_routes')
    checks = advice.setdefault('checks', [])
    if routes and not active_routes(conn, account, routes, now):
        warning = '本轮买入依据已失守、超价或报价中断，请用新行情复评'
        size = len(checks)
        block_entries(advice, warning, include_reviews=False)
        if len(checks) > size:
            advice['status'] = 'review_required'
        if advice.get('action') == 'buy':
            advice['action_status'] = 'review_required'
            advice['entry_pause_reason'] = warning
        return
    if advice.get('action') != 'buy' or advice['action_status'] != 'current':
        return
    from .trading_hours import trading_session
    from .trade_costs import number
    quote = account.get('quote') or {}
    quote_at = stamp(quote.get('quoted_at'))
    price = number(quote.get('buy_price_cny'))
    input_at = stamp(advice.get('input_quote_at'))
    limit = advice.get('price_cny')
    if (quote.get('status') != 'fresh' or not price or not quote_at or
            (input_at and quote_at < input_at) or not trading_session(now)['is_open']):
        warning = '暂无有效本行买价，买入建议待核对'
    elif limit is not None and advice.get('price_condition') == 'at_or_below' and price > limit:
        warning = f'当前本行买价{price:g}元/克超过建议上限{limit:g}元/克，暂停按该建议买入'
    elif limit is not None and advice.get('price_condition') == 'at_or_above' and price < limit:
        warning = f'当前本行买价{price:g}元/克低于建议触发价{limit:g}元/克，等待条件成立'
    else:
        return
    checks.append(warning)
    advice['entry_pause_reason'] = warning
    advice['action_status'] = advice['status'] = 'review_required'
