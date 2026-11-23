"""Verify real-bank entry triggers against quotes received while the model ran."""
from collections import defaultdict
from datetime import datetime
import json

from .jd_live_buy_watch import MAX_GAP, quote_tape, sample
from .jd_live_conditions import stamp


ENTRY_EVENT_PREFIX = 'live-buy-confirmed:'


def stored_price_condition(conn, model, data):
    from .jd_live_conditions import condition_key

    row = conn.execute('SELECT advice_json,snapshot_revision FROM jd_live_advice WHERE id=? AND analyst=? AND bank=?',
                       (data.get('advice_id'), model, data.get('bank'))).fetchone()
    if not row:
        return None, None, {}
    advice = json.loads(row[0])
    condition = next((c for c in advice.get('review_conditions', [])
                      if condition_key(c) == data.get('condition_key')), None)
    return condition, row[1], advice


def plain_buy_observation(condition):
    return (condition.get('quote_side') == 'buy' and not condition.get('order_key')
            and not condition.get('buy_review') and not condition.get('bank_breakout') and not condition.get('checks'))


def reviewed_price_observation(conn, model, account, direction, now):
    from .controls import control
    from .price_guard import read

    receipt = read(conn, f'live-price-reviewed:{model}:{account["bank"]}:{direction}')
    return (receipt.get('revision') == account.get('revision')
            and receipt.get('generation') == control(conn, model)['generation']
            and bool(active_routes(conn, account, [receipt], now)))


def remember_reviewed_price_observations(conn, model, context, event_ids, generation):
    from .price_guard import read, save

    covered = set(event_ids)
    for event in context.get('trigger_events', []):
        if event['id'] not in covered or not event['id'].startswith('live-price:'):
            continue
        data = event.get('data') or {}
        if data.get('quote_side') != 'buy' or data.get('order_key') or not (at := stamp(data.get('quoted_at'))):
            continue
        condition, revision, _ = stored_price_condition(conn, model, data)
        if not condition or not plain_buy_observation(condition):
            continue
        direction = condition['condition']
        key = f'live-price-reviewed:{model}:{data["bank"]}:{direction}'
        previous = read(conn, key)
        if previous.get('generation') == generation and (last := stamp(previous.get('confirmed_at'))) and at <= last:
            continue
        save(conn, key, {'generation': generation, 'revision': revision, 'condition': direction,
                         'confirmed_at': data['quoted_at'], 'threshold_cny': condition['price_cny']})


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
    inputs = (context.get('live_readonly_accounts') or {}).get('accounts', {})
    for event in entry_events(context, event_ids):
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
            if route.get('position_boundary_cny') is not None:
                previous['position_boundary_cny'] = route['position_boundary_cny']
            save(conn, key, previous)


def active_routes(conn, account, routes, now):
    point = sample(account, now)
    anchored = []
    for route in routes:
        anchor = stamp(route.get('confirmed_at'))
        expiry = route.get('signal_expires_at')
        deadline = stamp(expiry)
        if anchor and (expiry is None or (deadline is not None and now < deadline)):
            anchored.append((route, anchor))
    if not anchored or not point:
        return []
    quotes = quote_tape(conn, account['bank'], min(anchor for _, anchor in anchored), point)
    return [route for route, anchor in anchored if route_valid(route, anchor, quotes, point[0])]


def entry_events(context, event_ids):
    ids = set(event_ids)
    return (event for event in context.get('trigger_events', [])
            if event['id'] in ids and event['id'].startswith(ENTRY_EVENT_PREFIX))


def submitted_routes(context, event_ids):
    by_bank = defaultdict(list)
    for event in entry_events(context, event_ids):
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
            warnings[bank] = '分析期间买入触发已到期、失守、超价、触及退出边界或报价中断，须按新行情复评'
    return warnings


def route_valid(route, anchor: datetime, quotes, latest_at):
    if latest_at < anchor:
        return False
    low, high = (None, route['threshold_cny']) if route.get('condition') == 'at_or_below' else (route['threshold_cny'], None)
    boundary = route.get('position_boundary_cny')
    if setup := route.get('buy_review'):
        low, high = setup['recovery_price_cny'], setup['max_buy_price_cny']
    previous = anchor
    for at, buy, sell in quotes:
        if at < anchor:
            continue
        if (at - previous > MAX_GAP or (low is not None and buy < low) or (high is not None and buy > high)
                or (boundary is not None and sell <= boundary)):
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
    active = {confirmation_key(route): route for route in current['data']['routes']}
    routes = [active[key] for route in event['data']['routes'] if (key := confirmation_key(route)) in active]
    if not routes:
        return None
    return with_active_routes(event, routes)


def remember_reviewed_confirmations(conn, model, submitted):
    """Mark the exact completed observation for each current watch condition."""
    from .price_guard import read, save

    for event in submitted:
        data = event['data']
        key = f'live-buy-watch:{model}:{data["bank"]}'
        current = read(conn, key)
        identity = data.get('plan_id', data.get('advice_id'))
        if not identity or current.get('plan_id', current.get('advice_id')) != identity:
            continue
        changed = False
        for route in data.get('routes', []):
            condition, confirmed_at = confirmation_key(route)
            step = current.get('routes', {}).get(condition, {})
            if (confirmed_at and step.get('confirmed_at') == confirmed_at
                    and step.get('reviewed_confirmation_at') != confirmed_at):
                step['reviewed_confirmation_at'] = confirmed_at
                current.setdefault('reviewed_routes', {})[condition] = event['id']
                changed = True
        if changed:
            save(conn, key, current)


def remove_submitted_confirmations(conn, model, submitted):
    """Remove only exact confirmations from submitted events, under the publication write lock."""
    from .ai_events import read, write

    covered = {event['id']: {confirmation_key(r) for r in event['data'].get('routes', [])} for event in submitted}
    if not covered:
        return set()
    events = read(conn, model)
    retained, remaining_ids = [], set()
    for event in events['pending']:
        event_id = event['id']
        if event_id not in covered:
            retained.append(event)
            continue
        remaining = [route for route in event['data'].get('routes', [])
                     if confirmation_key(route) not in covered[event_id]]
        if remaining:
            retained.append(with_active_routes(event, remaining))
            remaining_ids.add(event_id)
    if retained != events['pending']:
        events['pending'] = retained
        write(conn, model, events)
    return remaining_ids


def retire_failed_events(conn, model, context, event_ids, failures):
    """Discard submitted stale observations while keeping a newer attempt."""
    failed = [event for event in entry_events(context, event_ids) if event['data']['bank'] in failures]
    remove_submitted_confirmations(conn, model, failed)
    failed_ids = {event['id'] for event in failed}
    return [event_id for event_id in event_ids if event_id not in failed_ids]


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


def publication_entry_basis(conn, bank, value, routes, now):
    """Bind a recommendation to its named input route, or require all ambiguous routes to hold."""
    from .jd_live_readonly import accounts

    basis = value.get('entry_trigger_key')
    selected = [route for route in routes if route['condition_key'] == basis] if basis else routes
    if not selected:
        return [], '本轮加仓引用的复评分支与输入不符，须重新确认'
    if len(routes) == 1:
        return selected, None
    account = next(iter(accounts(conn, now, include_advice=False, selected_bank=bank)), {})
    if len(active_routes(conn, account, selected, now)) == len(selected):
        return selected, None
    if basis:
        return selected, '本轮加仓引用的复评分支已失效，须重新确认'
    return selected, '部分买入触发已失效，尚未明确剩余路径是否支持本轮加仓'


def current_entry_state(conn, account, advice, now):
    if advice['lifecycle_status'] != 'current':
        return
    routes = advice.get('entry_trigger_routes')
    checks = advice.setdefault('checks', [])
    if routes and len(active_routes(conn, account, routes, now)) != len(routes):
        warning = '本轮买入依据已到期、失守、超价、触及退出边界或报价中断，请用新行情复评'
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
