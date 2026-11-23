"""Staged, read-only buy reviews over timestamped bank quotes."""
import json
from hashlib import sha256
from datetime import timedelta, timezone
from statistics import median

from .jd_live_conditions import condition_key, renewed_pullback_deadline, stamp, unchanged_buy_observation, zoned_pullback
from .trade_costs import cost_reference, number

MAX_GAP = timedelta(minutes=3)
WATCH_WINDOW = timedelta(hours=1)
CONTINUATION_HOLD_MINUTES = 3
CONTINUATION_MIN_QUOTES = 3
BREAKOUT_MODES = ('continuation', 'retest')
PRICE_DISCARD_KEY = 'live-price-discards:'
PRICE_SUPPRESSION_KEY = 'live-price-suppressed:'
PRICE_REVIEW_HISTORY_LIMIT = 512
QUOTE_SCHEMA = """CREATE TABLE IF NOT EXISTS jd_live_quote_samples (
    bank TEXT NOT NULL, quoted_at TEXT NOT NULL, observed_at TEXT NOT NULL,
    buy_price_cny REAL NOT NULL, sell_price_cny REAL NOT NULL,
    PRIMARY KEY (bank, quoted_at)
) WITHOUT ROWID;"""


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def sample(account, now):
    from .trading_hours import trading_session
    quote = account.get('quote') or {}
    at = stamp(quote.get('quoted_at'))
    buy, sell = number(quote.get('buy_price_cny')), number(quote.get('sell_price_cny'))
    if (account.get('connected') and quote.get('status') == 'fresh' and at and buy and sell
            and now - MAX_GAP <= at <= now and trading_session(at)['is_open']):
        return at, now, buy, sell
    return None


def record(conn, bank, point):
    at, observed, buy, sell = point
    with conn:
        inserted = conn.execute('INSERT OR IGNORE INTO jd_live_quote_samples VALUES(?,?,?,?,?)',
                                (bank, utc_text(at), utc_text(observed), buy, sell))
        if inserted.rowcount:
            conn.execute('DELETE FROM jd_live_quote_samples WHERE bank=? AND quoted_at<?',
                         (bank, utc_text(observed - timedelta(days=40))))


def quote_tape(conn, bank, after, point):
    """Ordered distinct bank quotes through the current sample, as known when observed."""
    rows = conn.execute(
        'SELECT quoted_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
        'WHERE bank=? AND quoted_at>? AND quoted_at<=? AND observed_at<=? ORDER BY quoted_at',
        (bank, utc_text(after), utc_text(point[0]), utc_text(point[1])))
    quotes = {at: (buy, sell) for at, buy, sell in rows}
    quotes[utc_text(point[0])] = point[2:]
    return [(stamp(at), buy, sell) for at, (buy, sell) in sorted(quotes.items())]


def earlier_sampled_ranges(conn, bank, before, now):
    """Preserve the coverage of earlier Beijing-date samples, not official daily extrema."""
    rows = conn.execute('''
        SELECT MIN(quoted_at),MAX(quoted_at),COUNT(*),MIN(buy_price_cny),MAX(buy_price_cny),MAX(sell_price_cny)
        FROM jd_live_quote_samples
        WHERE bank=? AND quoted_at BETWEEN ? AND ? AND observed_at>=quoted_at AND observed_at<=?
        GROUP BY date(quoted_at,'+8 hours')
        HAVING COUNT(*)>=3
        ORDER BY MAX(quoted_at) DESC
        ''', (bank, utc_text(before - timedelta(days=7)), utc_text(before), utc_text(now)))
    ranges = []
    for first, last, count, low, buy_high, sell_high in rows:
        if stamp(last) - stamp(first) < timedelta(minutes=15):
            continue
        ranges.append({'from': first, 'to': last, 'n': count, 'buy_low_cny': low,
                       'buy_high_cny': buy_high, 'sell_high_cny': sell_high})
        if len(ranges) == 2:
            break
    return ranges


def buy_price_windows(points, anchor):
    start = anchor - WATCH_WINDOW
    windows = []
    for index in range(4):
        end = start + timedelta(minutes=(index + 1) * 15)
        window = [(at, p) for at, p in points if end - timedelta(minutes=15) < at <= end]
        if window:
            values = [p for _, p in window]
            windows.append({'from': utc_text(window[0][0]), 'to': utc_text(window[-1][0]),
                            'n': len(values), 'low': min(values), 'high': max(values), 'last': values[-1]})
    return windows


def bank_breakout_condition(windows):
    high = max(window['high'] for window in windows)
    tolerance = max(0.01, round(median(w['high'] - w['low'] for w in windows) / 2, 2))
    return {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': high,
            'bank_breakout': {'retest_ceiling_cny': round(high + tolerance, 2), 'hold_minutes': 3},
            'reason': '本行买价突破近期高位后，持续走高或回踩守稳时重新评估。', 'checks': []}


def earlier_bank_breakout(ranges, nearby, current_buy):
    buy = number(current_buy)
    if not buy:
        return None
    ceiling = nearby['bank_breakout']['retest_ceiling_cny']
    high = min((level for row in ranges if (level := number(row.get('buy_high_cny')))
                and level > ceiling and level >= buy), default=None)
    if high is None:
        return None
    return {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': high,
            'bank_breakout': {'earlier_high': True}, 'checks': [],
            'reason': '突破本行更早高位后持续走高，再评估延续空间。'}


def quote_context(conn, account, now):
    """Compact price history and fee-adjusted rebound reference aligned to the current bank quote."""
    current = sample(account, now)
    if current is None:
        return {'status': 'unavailable'}
    anchor, _, buy, sell = current
    start = anchor - WATCH_WINDOW
    has_samples = conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone()
    tape = quote_tape(conn, account['bank'], start, current) if has_samples else [(anchor, buy, sell)]
    points = [(at, price) for at, price, _ in tape]
    recent = [(at, p) for at, p in points if anchor - timedelta(minutes=10) < at]
    available = len(recent) >= 3 and recent[-1][0] - recent[0][0] >= timedelta(minutes=2)
    context = {'status': 'available' if available else 'limited', 'as_of': utc_text(anchor),
               'buy_price_windows_15m': buy_price_windows(points, anchor),
               'recent_samples': [[int((at-anchor).total_seconds()), p] for at, p in recent[-6:]],
               'recent_sample_format': ['seconds_before_as_of_signed', 'buy_cny_per_gram'],
               'max_gap_seconds': max((int((right[0]-left[0]).total_seconds())
                                       for left, right in zip(points, points[1:])), default=None)}
    ranges = earlier_sampled_ranges(conn, account['bank'], start, now) if has_samples else []
    if ranges:
        context['earlier_sampled_ranges'] = ranges
    sell_low = min(s for _, _, s in tape)
    sell_high = max(s for _, _, s in tape)
    fee = account.get('sell_fee_rate')
    if available and (ref := cost_reference(sell_high, fee)):
        context['break_even_buy_at_sampled_sell_high_cny'] = ref['net_sell_price_cny']
        levels = {'hour_high': sell_high}
        if sell_low < sell_high:
            levels['hour_low'] = sell_low
        earlier_high = max((window['sell_high_cny'] for window in ranges), default=0)
        if earlier_high > max(sell_high, buy):
            levels['earlier_above_hour_high'] = earlier_high
        context['sampled_sell_scenarios'] = {
            name: {'sell_price_cny': price, 'net_cny_per_gram': round(net['net_sell_price_cny'] - buy, 2)}
            for name, price in levels.items() if (net := cost_reference(price, fee))
        }
    return context


def valid_route(condition, minimum=None):
    review = condition.get('buy_review')
    if not review or condition.get('checks') or condition.get('order_key') or condition.get('quote_side') != 'buy':
        return False
    grams = number(review.get('grams'))
    if review.get('grams') is not None and (not grams or minimum is not None and grams < minimum):
        return False
    trigger = condition['price_cny']
    recovery = review['recovery_price_cny']
    cap = review['max_buy_price_cny']
    if zone := review.get('zone'):
        floor = zone['floor_cny']
        return (zone['risk_exit_cny'] <= floor and recovery <= cap < zone['weak_target_cny']
                and (floor < min(trigger, recovery) if condition['condition'] == 'at_or_below'
                     else floor < recovery <= trigger))
    if condition['condition'] == 'at_or_below':
        return trigger < recovery <= cap
    return recovery <= trigger <= cap


def extended(condition):
    return bool(condition.get('bank_breakout') or (condition.get('buy_review') or {}).get('zone'))


def observation_only(advice):
    return advice['action'] in ('wait', 'hold') and not any(
        o['action'] in ('cancel', 'replace') for o in advice.get('order_management', []))


def eligible(condition, minimum=None):
    return (not condition.get('checks') and bool(condition.get('bank_breakout'))) or valid_route(condition, minimum)


def boundary_terms(advice):
    path = advice.get('position_path') or {}
    return tuple(path.get(key) for key in ('boundary_price_cny', 'boundary_role', 'exit_scope'))


def active_window(advice, account):
    from .jd_live_semantics import blocking_checks
    blocking = [check for check in blocking_checks(advice) if check != advice.get('entry_pause_reason')]
    if (not account.get('connected') or advice.get('snapshot_revision') != account.get('revision')
            or advice.get('_path_pause') or blocking):
        return False
    path = advice.get('position_path') or {}
    boundary = path.get('boundary_price_cny')
    return (not path.get('checks') and not path.get('manual_resolution_required')
            and (boundary is None or number((account.get('quote') or {}).get('sell_price_cny')) is not None))


def watch_id(advice):
    return (advice.get('buy_watch') or {}).get('id', advice['id'])


def breakout_key(key, mode):
    if mode == 'retest' and not key.endswith(':retest'):
        return f'{key}:retest'
    return key


def watch_route_key(condition):
    return breakout_key(condition_key(condition), condition.get('confirmation_mode'))


def watch_routes(conditions):
    for condition in conditions:
        if breakout := condition.get('bank_breakout'):
            for mode in ('continuation',) if breakout.get('earlier_high') else BREAKOUT_MODES:
                yield {**condition, 'confirmation_mode': mode}
        else:
            yield condition


def breakout_retest_price_status(route, buy):
    if route.get('confirmation_mode') != 'retest' or not buy:
        return None
    floor = number(route.get('threshold_cny'))
    ceiling = number((route.get('bank_breakout') or {}).get('retest_ceiling_cny'))
    if not floor or not ceiling or ceiling < floor:
        return None
    if buy < floor:
        return 'below_breakout'
    return 'above_retest_zone' if buy > ceiling else 'within_retest_zone'


def watch_progress(current):
    """Read a watch saved before or after the breakout branches were split."""
    routes = current.get('routes', {})
    reviewed = current.get('reviewed_routes', {})
    for key, stage in list(routes.items()):
        normalized = breakout_key(key, stage.get('confirmation_mode'))
        if normalized == key:
            continue
        routes.setdefault(normalized, routes.pop(key))
        if key in reviewed:
            reviewed.setdefault(normalized, reviewed.pop(key))
    return current


def candidate_deadline(advice, condition):
    """Find the deadline of a matching low-entry candidate, including queued routes."""
    review = condition.get('buy_review')
    if not review or condition.get('condition') != 'at_or_below' or condition.get('order_key'):
        return None
    entries = {review.get('max_buy_price_cny'), condition.get('threshold_cny', condition.get('price_cny'))}
    risk_exit = (review.get('zone') or {}).get('risk_exit_cny')
    grams = review.get('grams')
    return min((cutoff for candidate in advice.get('limit_buy_reviews', [])
                if candidate.get('decision') == 'needs_review'
                and (grams is None or candidate.get('grams') == grams)
                and candidate.get('entry_price_cny') in entries and candidate.get('entry_price_cny') is not None
                and (risk_exit is None or candidate.get('risk_exit_cny') in (None, risk_exit))
                and (cutoff := stamp(candidate.get('expires_at')))), default=None)


def watch_deadline(advice, condition):
    watch = advice.get('buy_watch') if extended(condition) else None
    deadline = stamp((watch or {}).get('expires_at') or advice.get('expires_at'))
    cutoff = candidate_deadline(advice, condition)
    return min(deadline, cutoff) if deadline and cutoff else deadline


def queued_route(route, advice, source_advice_id, now):
    """Keep the original cutoff within the current plan's validity."""
    source = route.get('source_advice_id', source_advice_id)
    if not extended(route) and source != advice['id']:
        return None
    deadline = watch_deadline(advice, route)
    expiry = route.get('signal_expires_at')
    original = stamp(expiry)
    if expiry is not None and original is None:
        return None
    if deadline and original:
        deadline = min(deadline, original)
    if not deadline or now >= deadline:
        return None
    return {**route, 'condition_key': breakout_key(route['condition_key'], route.get('confirmation_mode')),
            'signal_expires_at': utc_text(deadline)}


def buy_review_terms(condition):
    review = condition.get('buy_review') or {}
    zone = review.get('zone') or {}
    return (*[review.get(key) for key in ('grams', 'recovery_price_cny', 'hold_minutes', 'max_buy_price_cny')],
            *[zone.get(key) for key in ('floor_cny', 'weak_target_cny', 'risk_exit_cny')])


def set_watch(conn, bank, analyst, value, source, now, minimum):
    """Keep a live window across identical advice and reopen it once expired."""
    from .controls import control
    from .trading_hours import trading_session
    session = trading_session(now)
    if (not session['is_open'] or source.get('status') != 'fresh'
            or (source.get('quote') or {}).get('status') != 'fresh'):
        return
    generation = control(conn, analyst)['generation']
    old = conn.execute('SELECT snapshot_revision,advice_json FROM jd_live_advice WHERE bank=? AND analyst=? '
                       'ORDER BY id DESC LIMIT 1', (bank, analyst)).fetchone()
    previous = json.loads(old[1]) if old and old[0] == source['revision'] else {}
    prior_watch = previous.get('buy_watch') or {}
    if prior_watch.get('generation') != generation:
        previous, prior_watch = {}, {}
    prior_end = stamp(prior_watch.get('expires_at'))
    prior_active = prior_end is not None and now < prior_end
    conditions = value['review_conditions']
    evidence = source.get('recent_bank_quotes') or {}
    invalidated = (value.get('reassessment') or {}).get('thesis_status') == 'invalidated'
    if invalidated:
        return
    # Program-managed observations are independent of the model's condition limit.
    if value['action'] in ('wait', 'hold') and evidence.get('status') == 'available':
        prior = {bool(c['bank_breakout'].get('earlier_high')): c for c in previous.get('review_conditions', [])
                 if prior_active and c.get('bank_breakout')}
        nearby = prior.get(False) or bank_breakout_condition(evidence['buy_price_windows_15m'])
        earlier = prior.get(True) or earlier_bank_breakout(evidence.get('earlier_sampled_ranges', []), nearby,
                                                          source['quote'].get('buy_price_cny'))
        for generated in (c for c in (nearby, earlier) if c is not None):
            key = condition_key(generated)
            distinct = bool(generated['bank_breakout'].get('earlier_high'))
            if any((distinct or c.get('buy_review')) and condition_key(c) == key for c in conditions):
                generated['bank_breakout']['companion'] = True
            conditions[:] = [c for c in conditions if c.get('buy_review') or condition_key(c) != condition_key(generated)]
            conditions.append(generated)
    terms = []
    for c in conditions:
        if not extended(c) or not eligible(c, minimum):
            continue
        terms.append([condition_key(c), *buy_review_terms(c), c.get('bank_breakout')])
    if not terms:
        return
    terms.sort(key=lambda item: item[0])
    path = value.get('position_path') or {}
    fingerprint = sha256(json.dumps([value['action'], path.get('boundary_price_cny'), terms],
                                    sort_keys=True).encode()).hexdigest()[:24]
    same_plan = prior_watch.get('signature') == fingerprint and boundary_terms(previous) == boundary_terms(value)
    if same_plan and prior_active:
        value['buy_watch'] = prior_watch
        return
    duration = max(WATCH_WINDOW, timedelta(minutes=value['valid_for_minutes']))
    close = stamp(session.get('close_at')) or now + duration
    value['buy_watch'] = {'id': f'{generation}-{fingerprint}-{int(now.timestamp()*1_000_000)}', 'signature': fingerprint,
                          'starts_at': utc_text(now),
                          'expires_at': utc_text(min(now + duration, close)),
                          'generation': generation}
    if same_plan:
        value['buy_watch']['expiry_review_plan_id'] = prior_watch.get('expiry_review_plan_id', prior_watch['id'])


def failed_breakout(at, buy):
    return {'stage': 'breakout_failed', 'last_at': utc_text(at), 'last_price_cny': buy}


def advance_breakout(state, condition, at, buy, *, breakout_mode=None):
    threshold = condition['price_cny']
    stage = state.get('stage')
    if stage in (None, 'breakout_failed'):
        if buy > threshold:
            return {**state, 'stage': 'touched', 'peak': buy, 'touched_at': utc_text(at),
                    'continuation_since': utc_text(at), 'breakout_quote_count': 1}
        return state
    if buy < threshold:
        return failed_breakout(at, buy)

    rising = buy > state['peak']
    if buy > threshold:
        since = state.setdefault('continuation_since', utc_text(at))
        count = state['breakout_quote_count'] = state.get('breakout_quote_count', 0) + 1
        if (breakout_mode != 'retest' and rising and count >= CONTINUATION_MIN_QUOTES
                and at - stamp(since) >= timedelta(minutes=CONTINUATION_HOLD_MINUTES)):
            return {**state, 'stage': 'confirmed', 'peak': buy, 'confirmed_at': utc_text(at),
                    'hold_since': since, 'confirmation_mode': 'continuation'}
    else:
        state.pop('continuation_since', None)
        state.pop('breakout_quote_count', None)

    peak = state['peak']
    state['peak'] = max(peak, buy)
    rule = condition['bank_breakout']
    if rule.get('earlier_high'):
        return state
    if stage == 'touched':
        if buy > rule['retest_ceiling_cny'] or buy >= peak:
            return state
        return {**state, 'stage': 'holding', 'hold_since': utc_text(at)}
    since = stamp(state.get('hold_since'))
    if breakout_mode != 'continuation' and at - since >= timedelta(minutes=rule['hold_minutes']):
        return {**state, 'stage': 'confirmed', 'confirmed_at': utc_text(at), 'confirmation_mode': 'retest'}
    return state


def advance(state, condition, at, buy, *, breakout_mode=None):
    """Require ordered quotes with no stale gap across the setup and its hold."""
    previous_at = stamp(state.get('last_at'))
    stage = state.get('stage')
    bank_breakout = condition.get('bank_breakout')
    if stage in ('confirmed', 'episode_reviewed') or (previous_at and at <= previous_at):
        return state
    if stage == 'above_cap':
        if not zoned_pullback(condition) or buy > min(condition['price_cny'], condition['buy_review']['max_buy_price_cny']):
            return state
        state = {}
    if stage == 'pullback_lost' and not bank_breakout:
        return state
    if stage == 'pullback_lost':
        # Existing observation plans can resume after the older terminal failure.
        state = failed_breakout(previous_at or at, state.get('last_price_cny'))
    if previous_at and at - previous_at > MAX_GAP:
        state = {}
    state = {**state, 'last_at': utc_text(at), 'last_price_cny': buy}
    if bank_breakout:
        return advance_breakout(state, condition, at, buy, breakout_mode=breakout_mode)
    review = condition['buy_review']
    trigger = condition['price_cny']
    recovery = review['recovery_price_cny']
    cap = review['max_buy_price_cny']
    zone = review.get('zone')
    floor = zone['floor_cny'] if zone else None
    breakout = condition['condition'] == 'at_or_above'
    stage = state.get('stage')
    if not stage:
        if not (buy >= trigger if breakout else buy <= trigger):
            return state
        if floor is not None and buy < floor:
            return {**state, 'stage': 'pullback_lost'}
        state.update(stage='above_cap' if buy > cap and not zone else 'touched',
                     touched_at=utc_text(at))
        state['peak' if breakout else 'trough']=buy
        return state
    if floor is not None and buy < floor:
        return {**state, 'stage': 'pullback_lost', 'hold_since': None}
    if buy > cap and not (zone and stage == 'touched'):
        return {**state, 'stage': 'above_cap', 'hold_since': None}
    if breakout and stage == 'touched':
        if buy > trigger or buy >= state['peak'] or (zone and buy > cap):
            return {**state, 'peak': max(state['peak'], buy)}
        # A pullback from the observed breakout must precede confirmation.
        state['stage'] = 'retest'
    if zone and not breakout and stage == 'touched':
        trough = min(state.get('trough', buy), buy)
        state['trough'] = trough
        if buy > cap and trough <= cap:
            return {**state, 'stage': 'above_cap'}
        if buy <= trough or buy > cap or buy < recovery:
            return state
        state['stage'] = 'retest'
    if buy < recovery:
        return {**state, 'stage': state['stage'] if state['stage'] in ('touched', 'retest') else 'retest',
                'hold_since': None}
    since = stamp(state.get('hold_since'))
    if not since:
        return {**state, 'stage': 'holding', 'hold_since': utc_text(at)}
    if at - since >= timedelta(minutes=review['hold_minutes']):
        return {**state, 'stage': 'confirmed', 'confirmed_at': utc_text(at)}
    return state


def advance_watch(previous, condition, at, buy):
    last = stamp(previous.get('last_at'))
    mode = condition.get('confirmation_mode')
    if previous.get('stage') not in ('confirmed', 'episode_reviewed') or not last or at <= last:
        return advance(previous, condition, at, buy, breakout_mode=mode)
    review = condition.get('buy_review')
    above_cap = review and buy > review['max_buy_price_cny']
    if at - last > MAX_GAP and not (above_cap and zoned_pullback(condition)):
        return advance({}, condition, at, buy, breakout_mode=mode)
    if condition.get('bank_breakout') and buy < condition['price_cny']:
        return failed_breakout(at, buy)
    step = {**previous, 'last_at': utc_text(at), 'last_price_cny': buy}
    if above_cap:
        step['stage'] = 'above_cap'
    elif review and review.get('zone') and buy < review['recovery_price_cny']:
        stage = 'pullback_lost' if buy < review['zone']['floor_cny'] else 'retest'
        step.update(stage=stage, hold_since=None, confirmed_at=None)
    return step


def confirmed_route(condition, stage, price, boundary, advice):
    review = condition.get('buy_review')
    mode = stage.get('confirmation_mode')
    deadline = watch_deadline(advice, condition)
    if carried_until := stamp(stage.get('observation_expires_at')):
        deadline = min(deadline, carried_until)
    return {'condition_key': breakout_key(condition_key(condition), mode), 'condition': condition['condition'],
            'threshold_cny': condition['price_cny'], 'touched_at': stage['touched_at'], 'hold_since': stage['hold_since'],
            'confirmed_at': stage['confirmed_at'], 'price_cny': price,
            'signal_expires_at': utc_text(deadline),
            **({'source_advice_id': advice['id']} if not extended(condition) else {}),
            **({'position_boundary_cny': boundary} if boundary is not None else {}),
            **({'confirmation_mode': mode} if mode else {}),
            **({'buy_review': review} if review else {'bank_breakout': condition['bank_breakout']})}


def entry_review_reason(bank, routes):
    from .jd_live_readonly import PRODUCTS

    modes = {r.get('confirmation_mode') for r in routes}
    if any((r.get('bank_breakout') or {}).get('earlier_high') for r in routes):
        label = '突破更早高位后持续走高'
    elif 'continuation' in modes:
        label = '突破后持续走高与回踩均已确认' if 'retest' in modes else '突破后持续走高'
    else:
        label = '突破回踩已守稳' if 'retest' in modes else '观察条件已企稳'
    return f'真实账户{label}，重新评估：{PRODUCTS[bank][1]}'


def review_event(event_id, bank, advice_id, watch, routes, now):
    latest = max(routes, key=lambda route: stamp(route['confirmed_at']))
    return {'id': event_id, 'reason': entry_review_reason(bank, routes),
            'min_gap_minutes': 0, 'observed_at': now.isoformat(),
            'data': {'bank': bank, 'advice_id': advice_id, 'price_cny': latest['price_cny'],
                     'quoted_at': latest['confirmed_at'], 'routes': routes, **({'plan_id': watch['id']} if watch else {})}}


def pending_plan_review(events, bank, identity):
    return next((event for event in events['pending'] if event['id'].startswith('live-buy-confirmed:')
                 and event['data'].get('bank') == bank
                 and event['data'].get('plan_id', event['data'].get('advice_id')) == identity), None)


def advance_observation(condition, tape, stage):
    confirmation_price = None
    reentered = False
    for at, buy, _ in tape:
        step = advance_watch(stage, condition, at, buy)
        if stage.get('stage') == 'above_cap' and step.get('stage') == 'touched':
            reentered = True
        if step.get('confirmed_at') != stage.get('confirmed_at'):
            confirmation_price = buy if step.get('stage') == 'confirmed' else None
        stage = step
    return stage, confirmation_price, reentered


def replay_observation(conn, account, stage, condition, boundary, now):
    point = sample(account, now)
    start = stamp(stage.get('touched_at'))
    if not point or not start or point[0] < stamp(stage['last_at']):
        return {}, None
    tape = quote_tape(conn, account['bank'], start - timedelta(microseconds=1), point)
    if tape[0][0] != start:
        return {}, None
    if boundary is not None and any(sell <= boundary for _, _, sell in tape):
        return {}, None
    stage, price, _ = advance_observation(condition, tape, {})
    return stage, price


def observation_terms(condition):
    breakout = condition.get('bank_breakout') or {}
    return (condition['condition'], condition['quote_side'], condition['price_cny'], *buy_review_terms(condition)[1:],
            breakout.get('retest_ceiling_cny'), breakout.get('hold_minutes'), bool(breakout.get('earlier_high')))


def carry_entry_observations(conn, model, view_id, now):
    """Keep quote progress when a new advisory changes an unrelated condition."""
    from .ai_events import read, write
    from .jd_live_buy_guard import ENTRY_EVENT_PREFIX, active_routes, confirmation_key, reviewed_breakout, with_active_routes
    from .jd_live_readonly import accounts
    from .price_guard import save
    from .jd_live_conditions import read_watch

    events = read(conn, model)
    changed = False
    published = conn.execute('SELECT id,bank,snapshot_revision,advice_json FROM jd_live_advice WHERE analyst=? AND view_id=?',
                             (model, view_id)).fetchall()
    for advice_id, bank, revision, new_raw in published:
        new = json.loads(new_raw)
        watch = new.get('buy_watch')
        if not watch or not observation_only(new):
            continue
        previous_row = conn.execute('SELECT snapshot_revision,advice_json FROM jd_live_advice WHERE bank=? AND analyst=? '
                                    'AND id<? ORDER BY id DESC LIMIT 1', (bank, model, advice_id)).fetchone()
        if not previous_row or previous_row[0] != revision:
            continue
        old = json.loads(previous_row[1])
        previous = old.get('buy_watch')
        previous_end = stamp((previous or {}).get('expires_at'))
        if (not previous_end or watch['id'] == previous['id'] or watch['generation'] != previous['generation']
                or now >= previous_end):
            continue
        if boundary_terms(old) != boundary_terms(new):
            continue
        boundary = (new.get('position_path') or {}).get('boundary_price_cny')
        key = f'live-buy-watch:{model}:{bank}'
        current = watch_progress(read_watch(conn, key))
        if current.get('plan_id') != previous['id'] or current.get('invalidated_at'):
            continue
        current_accounts = accounts(conn, now, include_advice=False, selected_bank=bank)
        if not current_accounts:
            continue
        account = current_accounts[0]
        if not active_window({**new, 'snapshot_revision': revision}, account):
            continue
        minimum = account.get('minimum_buy_grams')
        old_conditions = [c for c in old['review_conditions'] if extended(c) and eligible(c, minimum)]
        event = pending_plan_review(events, bank, previous['id'])
        queued = {confirmation_key(r): r for r in event['data']['routes']} if event else {}
        event_id = f'{ENTRY_EVENT_PREFIX}{model}:{bank}:{watch["id"]}'
        progress, signals = {}, []
        active_stages = ('touched', 'retest', 'holding', 'confirmed')
        for condition in watch_routes(new['review_conditions']):
            if not extended(condition) or not eligible(condition, minimum):
                continue
            terms = observation_terms(condition)
            grams = (condition.get('buy_review') or {}).get('grams')
            previous_condition = next((c for c in old_conditions if observation_terms(c) == terms
                                       and (grams is None or (c.get('buy_review') or {}).get('grams') in (None, grams))), None)
            if not previous_condition:
                continue
            mode = condition.get('confirmation_mode')
            previous_key = breakout_key(condition_key(previous_condition), mode)
            stage = current.get('routes', {}).get(previous_key, {})
            if (stage.get('stage') not in active_stages or stage.get('reviewed_confirmation_at')
                    or previous_key in current.get('reviewed_routes', {})):
                continue
            deadline = min(watch_deadline(old, previous_condition), watch_deadline(new, condition),
                           stamp(stage.get('observation_expires_at')) or previous_end)
            if now >= deadline:
                continue
            new_key = watch_route_key(condition)
            if stage['stage'] == 'confirmed':
                route = queued.get((previous_key, stage['confirmed_at']))
                if not route or not active_routes(conn, account, [route], now):
                    continue
                deadline = min(deadline, stamp(route.get('signal_expires_at')) or deadline)
                if now >= deadline:
                    continue
                route = {**route, 'condition_key': new_key, 'signal_expires_at': utc_text(deadline)}
                if condition.get('bank_breakout'):
                    route['bank_breakout'] = condition['bank_breakout']
                if route.get('buy_review') and route['buy_review'].get('grams') != grams:
                    route['buy_review'] = {**route['buy_review'], 'grams': grams}
                signals.append(route)
                progress[new_key] = {**stage, 'event_id': event_id, 'observation_expires_at': utc_text(deadline)}
                continue
            stage, confirmation_price = replay_observation(conn, account, stage, condition, boundary, now)
            if stage.get('stage') not in active_stages:
                continue
            stage['observation_expires_at'] = utc_text(deadline)
            if stage['stage'] == 'confirmed':
                receipt = condition.get('bank_breakout') and reviewed_breakout(
                    conn, model, account, condition['price_cny'], now, mode,
                    earlier=condition['bank_breakout'].get('earlier_high', False))
                if receipt:
                    stage.update(stage='episode_reviewed', confirmed_at=receipt['confirmed_at'], event_id=receipt['event_id'])
                else:
                    signals.append(confirmed_route(condition, stage, confirmation_price, boundary, new))
                    stage['event_id'] = event_id
            progress[new_key] = stage
        if signals:
            if event:
                updated = with_active_routes(event, signals)
                event.update(id=event_id, data={**updated['data'], 'advice_id': advice_id, 'plan_id': watch['id']},
                             reason=updated['reason'])
            else:
                events['pending'].append(review_event(event_id, bank, advice_id, watch, signals, now))
            changed = True
        if progress:
            save(conn, key, {'advice_id': advice_id, 'plan_id': watch['id'], 'generation': watch['generation'],
                             'routes': progress})
    if changed:
        write(conn, model, events)


def plain_buy_discards(conn, model):
    from .price_guard import read

    return read(conn, PRICE_DISCARD_KEY + model).get('events', [])


def plain_buy_suppressions(conn, model):
    from .price_guard import read

    return read(conn, PRICE_SUPPRESSION_KEY + model).get('events', [])


def append_price_review_records(conn, key, records):
    from .price_guard import read, save

    history = [*read(conn,key).get('events', []), *records][-PRICE_REVIEW_HISTORY_LIMIT:]
    save(conn,key,{'events': history})


def pending_events(conn, model, now):
    """Remove superseded signals and verify actionable confirmations against bank quotes."""
    from .ai_events import read, write
    from .jd_live_buy_guard import (ENTRY_EVENT_PREFIX, active_routes, plain_buy_levels, plain_buy_observation,
                                   reviewed_breakout, reviewed_price_observation, stored_price_condition, with_active_routes)
    from .jd_live_readonly import accounts, review_context_current

    def validated(event, discarded):
        event_id, data = event['id'], event.get('data') or {}

        def discard_plain_buy(reason):
            if discarded is not None:
                discarded.append({'event_id': event_id, 'bank': data['bank'], 'advice_id': data['advice_id'],
                                  'condition_key': data['condition_key'], 'queued_at': event.get('observed_at'),
                                  'discarded_at': utc_text(now), 'reason': reason})
            return None
        if event_id.startswith('live-retest-reprice:'):
            current = accounts(conn, now, selected_bank=data['bank'])
            return event if any(advice['id'] == data['advice_id'] and advice['analyst'] == model
                                and advice.get('retest_reprice_needed') for account in current
                                for advice in account['advice']) else None
        if event_id.startswith('live-move:') and data.get('bank') and data.get('advice_id'):
            return event if review_context_current(conn, {'bank': data['bank']},
                                                    {'id': data['advice_id'], 'analyst': model}) else None
        if event_id.startswith('live-price:'):
            if data.get('buy_review'):
                return None
            if data.get('advice_id') and data.get('condition_key'):
                condition, revision, origin = stored_price_condition(conn, model, data)
                if condition and (condition.get('bank_breakout') or condition.get('buy_review')):
                    return None
                if condition and plain_buy_observation(condition):
                    latest = conn.execute('SELECT id,expires_at,advice_json,snapshot_revision FROM jd_live_advice '
                                          'WHERE bank=? AND analyst=? ORDER BY id DESC LIMIT 1',
                                          (data['bank'], model)).fetchone()
                    if not latest:
                        return discard_plain_buy('advice_unavailable')
                    renewed = data.get('renewed_pullback')
                    if renewed and latest[0] != data['advice_id']:
                        return discard_plain_buy('advice_replaced')
                    if not renewed and now >= stamp(origin['expires_at']):
                        return discard_plain_buy('advice_expired')
                    current_conditions=json.loads(latest[2]).get('review_conditions',[])
                    levels=plain_buy_levels(current_conditions,condition['condition'])
                    if latest[0] != data['advice_id']:
                        if latest[3] != revision:
                            return discard_plain_buy('account_changed')
                        if now >= stamp(latest[1]):
                            return discard_plain_buy('advice_expired')
                        if data['condition_key'] not in levels:
                            return discard_plain_buy('advice_replaced')
                    quoted, observed = stamp(data.get('quoted_at')), stamp(event.get('observed_at'))
                    buy = number(data.get('price_cny'))
                    if quoted and observed and buy and unchanged_buy_observation(
                            conn, data['bank'], origin, condition, (quoted, observed, buy, None)):
                        return discard_plain_buy('already_in_analysis_input')
                    current = accounts(conn, now, include_advice=False, selected_bank=data['bank'])
                    if current and current[0].get('revision') != revision:
                        return discard_plain_buy('account_changed')
                    if renewed and (not current or not renewed_pullback_deadline(conn, current[0], origin, condition, now)):
                        return discard_plain_buy('renewed_pullback_no_longer_valid')
                    if current and current[0].get('revision') == revision and reviewed_price_observation(
                            conn, model, current[0], condition, now, independent=len(levels)>1):
                        return discard_plain_buy('same_price_episode_already_reviewed')
        if event_id.startswith('live-expired:') and data.get('advice_id'):
            row = conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=?', (data['advice_id'],)).fetchone()
            old = json.loads(row[0]) if row else {}
            if old.get('buy_watch') and observation_only(old):
                return None
        confirmation = event_id.startswith(ENTRY_EVENT_PREFIX)
        if confirmation or event_id.startswith('live-watch-expired:'):
            latest = conn.execute('SELECT id,expires_at,advice_json,snapshot_revision FROM jd_live_advice WHERE bank=? AND analyst=? '
                                  'ORDER BY id DESC LIMIT 1', (data.get('bank'), model)).fetchone()
            if not latest:
                return None
            advice = {**json.loads(latest[2]), 'id': latest[0], 'expires_at': latest[1]}
            watch = advice.get('buy_watch') or {}
            if data.get('plan_id'):
                if (watch.get('id') != data['plan_id']
                        or confirmation and now >= stamp(watch['expires_at'])):
                    return None
            elif latest[0] != data.get('advice_id') or now >= stamp(latest[1]):
                return None
            current = accounts(conn, now, include_advice=False, selected_bank=data['bank'])
            if not current or current[0].get('revision') != latest[3]:
                return None
            if confirmation:
                routes = [current_route for route in data.get('routes', [])
                          if (current_route := queued_route(route, advice, data.get('advice_id'), now))]
                boundary = (advice.get('position_path') or {}).get('boundary_price_cny')
                if watch and boundary is not None:
                    routes = [{'position_boundary_cny': boundary, **route} for route in routes]
                routes = active_routes(conn, current[0], routes, now)
                routes = [r for r in routes if not r.get('bank_breakout') or not reviewed_breakout(
                    conn, model, current[0], r['threshold_cny'], now, r.get('confirmation_mode'),
                    earlier=r['bank_breakout'].get('earlier_high', False))]
                return with_active_routes(event, routes) if routes else None
        return event

    def prune(events, discarded=None):
        return [fresh for event in events if (fresh := validated(event, discarded)) is not None]

    pending = read(conn, model)['pending']
    if prune(pending) == pending:
        return pending
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        events = read(conn, model)
        discarded = []
        pending = prune(events['pending'], discarded)
        if pending != events['pending']:
            events['pending'] = pending
            write(conn, model, events)
            if discarded:
                append_price_review_records(conn, PRICE_DISCARD_KEY + model, discarded)
    return pending


def stage_before_deadline(step, deadline):
    last = stamp(step.get('last_at'))
    original = stamp(step.get('observation_expires_at'))
    if last and last < deadline and (original is None or deadline <= original):
        return step.get('stage')
    return None


def retain_expired_progress(current, advice, now):
    expired={}
    for condition in advice.get('review_conditions', []):
        if not condition.get('buy_review') or not (deadline:=watch_deadline(advice,condition)) or now<deadline:
            continue
        key=watch_route_key(condition)
        step=current['routes'].get(key,{})
        if stage:=stage_before_deadline(step,deadline):
            expired[key]={'stage':stage,'expires_at':utc_text(deadline)}
        else:
            prior=current.get('expired_routes',{}).get(key,{})
            expired[key]=prior if stamp(prior.get('expires_at'))==deadline else {'expires_at':utc_text(deadline)}
    if expired:
        current['expired_routes']=expired
    else:
        current.pop('expired_routes',None)


def request(store, account, advice, now, point):
    from .ai_events import read, write
    from .controls import active, control
    from .jd_live_buy_guard import reviewed_breakout, with_active_routes
    from .jd_live_conditions import read_watch
    from .jd_live_readonly import review_context_current

    at, observed, _, sell = point
    watch = advice.get('buy_watch') or {}
    created = stamp(watch.get('starts_at') or advice.get('created_at'))
    published = stamp(advice.get('created_at')) or created
    boundary = (advice.get('position_path') or {}).get('boundary_price_cny')
    boundary_hit = watch and boundary is not None and sell <= boundary
    if (not created or not created <= at <= observed
            or not (active_window(advice, account) or boundary_hit)):
        return
    bank, model = account['bank'], advice['analyst']
    routes = [c for c in advice.get('review_conditions', [])
              if eligible(c, account.get('minimum_buy_grams'))
              and (watch and extended(c) or advice.get('action_status') == 'current')
              and (deadline := watch_deadline(advice, c)) and observed < deadline]
    if not routes:
        return
    conn = store.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        switch = control(conn, model)
        changed = stamp(switch.get('changed_at'))
        if (not active(conn, model) or (changed and created <= changed)
                or (watch and watch['generation'] != switch['generation'])):
            return
        if not review_context_current(conn, account, advice):
            return
        key = f'live-buy-watch:{model}:{bank}'
        current = watch_progress(read_watch(conn, key))
        identity = watch_id(advice)
        if current.get('plan_id', current.get('advice_id')) != identity or current.get('generation') != switch['generation']:
            current = {'advice_id': advice['id'], 'plan_id': identity, 'generation': switch['generation'], 'routes': {}}
        if current.get('invalidated_at'):
            return
        cursor = stamp(current.get('last_quote_at')) or max(
            (seen for stage in current['routes'].values() if (seen := stamp(stage.get('last_at')))),
            default=created - timedelta(microseconds=1),
        )
        if at <= cursor:
            return
        tape = quote_tape(conn, bank, cursor, point)
        expired_routes = current.get('expired_routes', {})
        if watch:
            retain_expired_progress(current,advice,observed)
        current['last_quote_at'] = utc_text(at)
        if watch and boundary is not None and (hit := next((when for when, _, price in tape if price <= boundary), None)):
            current['invalidated_at'] = utc_text(hit)
            routes = []
        updated = {}
        confirmed = []
        event_id = f'live-buy-confirmed:{model}:{bank}:{identity}'
        reviewed = current.setdefault('reviewed_routes', {})
        for condition in watch_routes(routes):
            fingerprint = watch_route_key(condition)
            previous = current['routes'].get(fingerprint, {})
            if previous.get('reviewed_confirmation_at'):
                reviewed.setdefault(fingerprint, previous.get('event_id', event_id))
            route_tape = tape
            if not previous and tape[0][0] < created:
                route_tape = [quote for quote in tape if quote[0] >= created]
            deadline = stamp(previous.get('observation_expires_at') or expired_routes.get(fingerprint, {}).get('expires_at'))
            if deadline and observed >= deadline:
                start = max(deadline, published)
                if at < start:
                    updated[fingerprint] = previous or {'observation_expires_at': utc_text(deadline)}
                    continue
                previous = {}
                route_tape = [quote for quote in tape if quote[0] >= start]
                if zoned_pullback(condition):
                    reviewed.pop(fingerprint, None)
                deadline = None
            if (candidate := candidate_deadline(advice, condition)) and not previous:
                route_tape = [quote for quote in route_tape if quote[0] >= published]
            if (condition.get('bank_breakout') and fingerprint in reviewed
                    and any(buy < condition['price_cny'] for _, buy, _ in route_tape)):
                # A new breakout can be reviewed after a verified break below the level.
                del reviewed[fingerprint]
            step, confirmation_price, reentered = advance_observation(condition, route_tape, previous)
            if reentered:
                reviewed.pop(fingerprint, None)
            if step and (deadline or candidate):
                step['observation_expires_at'] = utc_text(min(deadline, candidate) if deadline and candidate else deadline or candidate)
            if (step.get('stage') == 'confirmed' and confirmation_price is not None
                    and fingerprint not in reviewed):
                if condition.get('bank_breakout') and (receipt := reviewed_breakout(
                        conn, model, account, condition['price_cny'], observed, condition.get('confirmation_mode'),
                        earlier=condition['bank_breakout'].get('earlier_high', False))):
                    updated[fingerprint] = {**step, 'stage': 'episode_reviewed',
                                            'confirmed_at': receipt['confirmed_at'], 'event_id': receipt['event_id']}
                    continue
                confirmed.append(confirmed_route(condition, step, confirmation_price, boundary if watch else None, advice))
            updated[fingerprint] = step
        current['routes'] = updated
        events = read(conn, model)
        existing = pending_plan_review(events, bank, identity)
        if existing:
            for route in confirmed:
                updated[route['condition_key']]['event_id'] = existing['id']
            evidence = [r for original in existing['data']['routes']
                        if (r := queued_route(original, advice, existing['data'].get('advice_id'), observed))] + confirmed
            retained = [r for r in evidence if (step := updated.get(r['condition_key'], {})).get('stage') == 'confirmed'
                        and step.get('confirmed_at') == r['confirmed_at']]
            if not retained:
                events['pending'].remove(existing)
                write(conn, model, events)
            elif retained != existing['data']['routes']:
                existing.update(with_active_routes(existing, retained))
                write(conn, model, events)
        elif confirmed and (watch or event_id not in events['covered']):
            if event_id in events['covered']:
                event_id = f'{event_id}:{utc_text(at)}'
            for route in confirmed:
                updated[route['condition_key']]['event_id'] = event_id
            events['pending'].append(review_event(event_id, bank, advice['id'], watch, confirmed, now))
            write(conn, model, events)
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) '
                     'DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (key, json.dumps(current, ensure_ascii=False), now.isoformat()))
