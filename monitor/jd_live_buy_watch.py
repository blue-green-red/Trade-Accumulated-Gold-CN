"""Staged, read-only buy reviews over timestamped bank quotes."""
import json
from hashlib import sha256
from datetime import timedelta, timezone
from statistics import median

from .jd_live_conditions import condition_key, stamp
from .trade_costs import number

MAX_GAP = timedelta(minutes=3)
WATCH_WINDOW = timedelta(hours=1)
CONTINUATION_HOLD_MINUTES = 3
CONTINUATION_MIN_QUOTES = 3
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


def quote_context(conn, account, now):
    """Compact observed buy-price history, aligned to the bank quote in this request."""
    current = sample(account, now)
    if current is None:
        return {'status': 'unavailable'}
    anchor, _, buy, _ = current
    start = anchor - timedelta(hours=1)
    has_samples = conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone()
    prices = {
        stamp(at): price for at, price in conn.execute(
            'SELECT quoted_at,buy_price_cny FROM jd_live_quote_samples '
            'WHERE bank=? AND quoted_at>? AND quoted_at<=? AND observed_at<=?',
            (account['bank'], utc_text(start), utc_text(anchor), utc_text(now))
        )
    } if has_samples else {}
    prices[anchor] = buy
    points = sorted(prices.items())
    windows = []
    for index in range(4):
        end = start + timedelta(minutes=(index + 1) * 15)
        window = [(at, p) for at, p in points if end - timedelta(minutes=15) < at <= end]
        if window:
            values = [p for _, p in window]
            windows.append({'from': utc_text(window[0][0]), 'to': utc_text(window[-1][0]),
                            'n': len(values), 'low': min(values), 'high': max(values), 'last': values[-1]})
    recent = [(at, p) for at, p in points if anchor - timedelta(minutes=10) < at]
    return {'status': 'available' if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= timedelta(minutes=2)
            else 'limited', 'as_of': utc_text(anchor), 'buy_price_windows_15m': windows,
            'recent_samples': [[int((at-anchor).total_seconds()), p] for at, p in recent[-6:]],
            'recent_sample_format': ['seconds_before_as_of_signed', 'buy_cny_per_gram'],
            'max_gap_seconds': max((int((right[0]-left[0]).total_seconds())
                                    for left, right in zip(points, points[1:])), default=None)}


def valid_route(condition, minimum=None):
    review = condition.get('buy_review')
    if not review or condition.get('checks') or condition.get('order_key') or condition.get('quote_side') != 'buy':
        return False
    if minimum is not None and review['grams'] < minimum:
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


def watch_deadline(advice, condition):
    watch = advice.get('buy_watch') if extended(condition) else None
    return stamp((watch or {}).get('expires_at') or advice.get('expires_at'))


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
    # The model's condition limit does not include this program-managed observation.
    if value['action'] in ('wait', 'hold') and evidence.get('status') == 'available':
        prior = next((c for c in previous.get('review_conditions', []) if c.get('bank_breakout')), None)
        if prior and prior_active:
            conditions.append(prior)
        else:
            windows = evidence['buy_price_windows_15m']
            high = max(window['high'] for window in windows)
            # Use observed 15-minute ranges for the retest tolerance around the bank high.
            tolerance = max(0.01, round(median(w['high'] - w['low'] for w in windows) / 2, 2))
            conditions.append({'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': high,
                'bank_breakout': {'retest_ceiling_cny': round(high + tolerance, 2), 'hold_minutes': 3},
                'reason': '本行买价突破近期高位后，持续走高或回踩守稳时重新评估。', 'checks': []})
        generated = conditions[-1]
        if any(c.get('buy_review') and condition_key(c) == condition_key(generated) for c in conditions[:-1]):
            generated['bank_breakout']['companion'] = True
        conditions[:] = [c for c in conditions[:-1] if c.get('buy_review') or condition_key(c) != condition_key(generated)]
        conditions.append(generated)
    terms = []
    for c in conditions:
        if not extended(c) or not eligible(c, minimum):
            continue
        review = c.get('buy_review') or {}
        zone = review.get('zone') or {}
        terms.append([condition_key(c), *[review.get(k) for k in ('grams', 'recovery_price_cny',
                      'hold_minutes', 'max_buy_price_cny')], *[zone.get(k) for k in ('floor_cny', 'weak_target_cny',
                      'risk_exit_cny')], c.get('bank_breakout')])
    if not terms:
        return
    terms.sort(key=lambda item: item[0])
    path = value.get('position_path') or {}
    fingerprint = sha256(json.dumps([value['action'], path.get('boundary_price_cny'), terms],
                                    sort_keys=True).encode()).hexdigest()[:24]
    same_plan = prior_watch.get('signature') == fingerprint
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


def advance_breakout(state, condition, at, buy):
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
        if (rising and count >= CONTINUATION_MIN_QUOTES
                and at - stamp(since) >= timedelta(minutes=CONTINUATION_HOLD_MINUTES)):
            return {**state, 'stage': 'confirmed', 'peak': buy, 'confirmed_at': utc_text(at),
                    'hold_since': since, 'confirmation_mode': 'continuation'}
    else:
        state.pop('continuation_since', None)
        state.pop('breakout_quote_count', None)

    peak = state['peak']
    state['peak'] = max(peak, buy)
    rule = condition['bank_breakout']
    if stage == 'touched':
        if buy > rule['retest_ceiling_cny'] or buy >= peak:
            return state
        return {**state, 'stage': 'holding', 'hold_since': utc_text(at)}
    since = stamp(state.get('hold_since'))
    if at - since >= timedelta(minutes=rule['hold_minutes']):
        return {**state, 'stage': 'confirmed', 'confirmed_at': utc_text(at), 'confirmation_mode': 'retest'}
    return state


def advance(state, condition, at, buy):
    """Require ordered quotes with no stale gap across the setup and its hold."""
    previous_at = stamp(state.get('last_at'))
    stage = state.get('stage')
    bank_breakout = condition.get('bank_breakout')
    if stage in ('confirmed', 'episode_reviewed', 'above_cap') or (previous_at and at <= previous_at):
        return state
    if stage == 'pullback_lost' and not bank_breakout:
        return state
    if stage == 'pullback_lost':
        # Existing observation plans can resume after the older terminal failure.
        state = failed_breakout(previous_at or at, state.get('last_price_cny'))
    if previous_at and at - previous_at > MAX_GAP:
        state = {}
    state = {**state, 'last_at': utc_text(at), 'last_price_cny': buy}
    if bank_breakout:
        return advance_breakout(state, condition, at, buy)
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


def pending_events(conn, model, now):
    """Remove superseded signals and verify actionable confirmations against bank quotes."""
    from .ai_events import read, write
    from .jd_live_buy_guard import ENTRY_EVENT_PREFIX, active_routes, reviewed_breakout, with_active_routes
    from .jd_live_readonly import accounts

    def validated(event):
        event_id, data = event['id'], event.get('data') or {}
        if event_id.startswith('live-price:'):
            if data.get('buy_review'):
                return None
            if data.get('advice_id') and data.get('condition_key'):
                row = conn.execute('SELECT advice_json FROM jd_live_advice WHERE id=?', (data['advice_id'],)).fetchone()
                if row and any((c.get('bank_breakout') or c.get('buy_review')) and
                               condition_key(c) == data['condition_key']
                               for c in json.loads(row[0]).get('review_conditions', [])):
                    return None
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
            advice = json.loads(latest[2])
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
                routes = data.get('routes', [])
                boundary = (advice.get('position_path') or {}).get('boundary_price_cny')
                if watch and boundary is not None:
                    routes = [{'position_boundary_cny': boundary, **route} for route in routes]
                routes = active_routes(conn, current[0], routes, now)
                routes = [r for r in routes if not r.get('bank_breakout')
                          or not reviewed_breakout(conn, model, current[0], r['threshold_cny'], now)]
                return with_active_routes(event, routes) if routes else None
        return event

    def prune(events):
        return [fresh for event in events if (fresh := validated(event)) is not None]

    pending = read(conn, model)['pending']
    if prune(pending) == pending:
        return pending
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        events = read(conn, model)
        pending = prune(events['pending'])
        if pending != events['pending']:
            events['pending'] = pending
            write(conn, model, events)
    return pending


def request(store, account, advice, now, point):
    from .ai_events import read, write
    from .controls import active, control
    from .jd_live_buy_guard import reviewed_breakout, with_active_routes
    from .jd_live_conditions import read_watch
    from .jd_live_readonly import PRODUCTS

    at, observed, buy, sell = point
    watch = advice.get('buy_watch') or {}
    created = stamp(watch.get('starts_at') or advice.get('created_at'))
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
        latest = conn.execute('SELECT id FROM jd_live_advice WHERE bank=? AND analyst=? ORDER BY id DESC LIMIT 1',
                              (bank, model)).fetchone()
        if not latest or latest[0] != advice['id']:
            return
        key = f'live-buy-watch:{model}:{bank}'
        current = read_watch(conn, key)
        identity = watch_id(advice)
        if current.get('plan_id', current.get('advice_id')) != identity or current.get('generation') != switch['generation']:
            current = {'advice_id': advice['id'], 'plan_id': identity, 'generation': switch['generation'], 'routes': {}}
        if current.get('invalidated_at'):
            return
        if boundary_hit:
            current['invalidated_at'] = utc_text(at)
            routes = []
        updated = {}
        confirmed = []
        for condition in routes:
            fingerprint = condition_key(condition)
            previous = current['routes'].get(fingerprint, {})
            step = advance(previous, condition, at, buy)
            review = condition.get('buy_review')
            last = stamp(previous.get('last_at'))
            if previous.get('stage') in ('confirmed', 'episode_reviewed') and last and at > last:
                if at - last > MAX_GAP:
                    step = advance({}, condition, at, buy)
                elif condition.get('bank_breakout') and buy < condition['price_cny']:
                    step = failed_breakout(at, buy)
                elif review and buy > review['max_buy_price_cny']:
                    step = {**previous, 'stage': 'above_cap', 'last_at': utc_text(at), 'last_price_cny': buy}
                elif review and review.get('zone') and buy < review['recovery_price_cny']:
                    step = {**previous, 'stage': 'retest', 'last_at': utc_text(at),
                            'last_price_cny': buy, 'hold_since': None, 'confirmed_at': None}
                    if buy < review['zone']['floor_cny']:
                        step['stage'] = 'pullback_lost'
                else:
                    step = {**previous, 'last_at': utc_text(at), 'last_price_cny': buy}
            if step.get('stage') == 'confirmed' and previous.get('stage') != 'confirmed':
                if condition.get('bank_breakout') and (receipt := reviewed_breakout(
                        conn, model, account, condition['price_cny'], observed)):
                    updated[fingerprint] = {**step, 'stage': 'episode_reviewed',
                                            'confirmed_at': receipt['confirmed_at'], 'event_id': receipt['event_id']}
                    continue
                confirmed.append({'condition_key': fingerprint, 'condition': condition['condition'],
                                  'threshold_cny': condition['price_cny'], 'touched_at': step['touched_at'],
                                  'hold_since': step['hold_since'], 'confirmed_at': step['confirmed_at'],
                                  'price_cny': buy,
                                  **({'position_boundary_cny': boundary} if watch and boundary is not None else {}),
                                  **({'confirmation_mode': step['confirmation_mode']}
                                     if step.get('confirmation_mode') else {}),
                                  **({'buy_review': review} if review else {'bank_breakout': condition['bank_breakout']})})
            updated[fingerprint] = step
        if updated == current['routes'] and not current.get('invalidated_at'):
            return
        current['routes'] = updated
        event_id = f'live-buy-confirmed:{model}:{bank}:{identity}'
        events = read(conn, model)
        existing = next((e for e in events['pending'] if e['id'] == event_id), None)
        if existing:
            evidence = existing['data']['routes'] + confirmed
            retained = [r for r in evidence if (step := updated.get(r['condition_key'], {})).get('stage') == 'confirmed'
                        and step.get('confirmed_at') == r['confirmed_at']]
            if not retained:
                events['pending'].remove(existing)
                write(conn, model, events)
            elif retained != existing['data']['routes']:
                existing.update(with_active_routes(existing, retained))
                write(conn, model, events)
        elif confirmed and event_id not in events['covered']:
            continued = any(r.get('confirmation_mode') == 'continuation' for r in confirmed)
            label = '突破后持续走高' if continued else '观察条件已企稳'
            events['pending'].append({'id': event_id,
                'reason': f'真实账户{label}，重新评估：{PRODUCTS[bank][1]}',
                'min_gap_minutes': 0, 'observed_at': now.isoformat(),
                'data': {'bank': bank, 'advice_id': advice['id'], 'price_cny': buy,
                         'quoted_at': utc_text(at), 'routes': confirmed,
                         **({'plan_id': watch['id']} if watch else {})}})
            write(conn, model, events)
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) '
                     'DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (key, json.dumps(current, ensure_ascii=False), now.isoformat()))
