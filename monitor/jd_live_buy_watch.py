"""Staged, read-only buy reviews over timestamped bank quotes."""
import json
from datetime import timedelta, timezone

from .jd_live_conditions import condition_key, stamp
from .trade_costs import number

MAX_GAP = timedelta(minutes=3)
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


def valid_route(condition, minimum=None):
    review = condition.get('buy_review')
    if not review or condition.get('checks') or condition.get('order_key') or condition.get('quote_side') != 'buy':
        return False
    if minimum is not None and review['grams'] < minimum:
        return False
    trigger = condition['price_cny']
    recovery = review['recovery_price_cny']
    cap = review['max_buy_price_cny']
    if condition['condition'] == 'at_or_below':
        return trigger < recovery <= cap
    return recovery <= trigger <= cap


def advance(state, condition, at, buy):
    """Require ordered quotes with no stale gap across the setup and its hold."""
    previous_at = stamp(state.get('last_at'))
    if state.get('stage') in ('confirmed', 'above_cap') or (previous_at and at <= previous_at):
        return state
    if previous_at and at - previous_at > MAX_GAP:
        state = {}
    state = {**state, 'last_at': utc_text(at), 'last_price_cny': buy}
    review = condition['buy_review']
    trigger = condition['price_cny']
    recovery = review['recovery_price_cny']
    cap = review['max_buy_price_cny']
    breakout = condition['condition'] == 'at_or_above'
    stage = state.get('stage')
    if not stage:
        if not (buy >= trigger if breakout else buy <= trigger):
            return state
        state.update(stage='above_cap' if buy > cap else 'touched', touched_at=utc_text(at), peak=buy)
        return state
    if buy > cap:
        return {**state, 'stage': 'above_cap', 'hold_since': None}
    if breakout and stage == 'touched':
        if buy > trigger or buy >= state['peak']:
            return {**state, 'peak': max(state['peak'], buy)}
        # A pullback from the observed breakout must precede confirmation.
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
    """Ignore old collector touch events and confirmations from superseded advice."""
    from .ai_events import read, write

    def obsolete(event):
        event_id, data = event['id'], event.get('data') or {}
        if event_id.startswith('live-price:') and data.get('buy_review'):
            return True
        if event_id.startswith('live-buy-confirmed:'):
            latest = conn.execute('SELECT id,expires_at FROM jd_live_advice WHERE bank=? AND analyst=? '
                                  'ORDER BY id DESC LIMIT 1', (data.get('bank'), model)).fetchone()
            return not latest or latest[0] != data.get('advice_id') or now >= stamp(latest[1])
        return False

    pending = read(conn, model)['pending']
    if not any(obsolete(event) for event in pending):
        return pending
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        events = read(conn, model)
        pending = [event for event in events['pending'] if not obsolete(event)]
        if pending != events['pending']:
            events['pending'] = pending
            write(conn, model, events)
    return pending


def request(store, account, advice, now, point):
    from .ai_events import read, write
    from .controls import active, control
    from .jd_live_conditions import read_watch
    from .jd_live_readonly import PRODUCTS

    at, observed, buy, _ = point
    created, expires = stamp(advice.get('created_at')), stamp(advice.get('expires_at'))
    if (advice.get('action_status') != 'current' or advice.get('_path_pause')
            or not created or not expires or not created <= at <= observed < expires):
        return
    bank, model = account['bank'], advice['analyst']
    routes = [c for c in advice.get('review_conditions', []) if valid_route(c, account.get('minimum_buy_grams'))]
    if not routes:
        return
    conn = store.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        switch = control(conn, model)
        changed = stamp(switch.get('changed_at'))
        if not active(conn, model) or (changed and created <= changed):
            return
        latest = conn.execute('SELECT id FROM jd_live_advice WHERE bank=? AND analyst=? ORDER BY id DESC LIMIT 1',
                              (bank, model)).fetchone()
        if not latest or latest[0] != advice['id']:
            return
        key = f'live-buy-watch:{model}:{bank}'
        current = read_watch(conn, key)
        if current.get('advice_id') != advice['id'] or current.get('generation') != switch['generation']:
            current = {'advice_id': advice['id'], 'generation': switch['generation'], 'routes': {}}
        updated = {}
        confirmed = []
        for condition in routes:
            fingerprint = condition_key(condition)
            previous = current['routes'].get(fingerprint, {})
            step = advance(previous, condition, at, buy)
            cap = condition['buy_review']['max_buy_price_cny']
            if (previous.get('stage') == 'confirmed' and at > stamp(previous['last_at']) and buy > cap):
                step = {**previous, 'stage': 'above_cap', 'last_at': utc_text(at), 'last_price_cny': buy}
            updated[fingerprint] = step
            if step.get('stage') == 'confirmed' and previous.get('stage') != 'confirmed':
                confirmed.append({'condition_key': fingerprint, 'condition': condition['condition'],
                                  'threshold_cny': condition['price_cny'], 'touched_at': step['touched_at'],
                                  'hold_since': step['hold_since'], 'confirmed_at': step['confirmed_at'],
                                  'price_cny': buy, 'buy_review': condition['buy_review']})
        if updated == current['routes']:
            return
        current['routes'] = updated
        event_id = f'live-buy-confirmed:{model}:{bank}:{advice["id"]}'
        events = read(conn, model)
        existing = next((e for e in events['pending'] if e['id'] == event_id), None)
        if existing:
            evidence = existing['data']['routes'] + confirmed
            retained = [r for r in evidence if updated.get(r['condition_key'], {}).get('stage') == 'confirmed']
            if not retained:
                events['pending'].remove(existing)
            else:
                existing['data']['routes'] = retained
            if existing['data']['routes'] != evidence or confirmed or not retained:
                write(conn, model, events)
        elif confirmed and event_id not in events['covered']:
            events['pending'].append({'id': event_id,
                'reason': f'真实账户买入候选已企稳，重新评估：{PRODUCTS[bank][1]}',
                'min_gap_minutes': 0, 'observed_at': now.isoformat(),
                'data': {'bank': bank, 'advice_id': advice['id'], 'price_cny': buy,
                         'quoted_at': utc_text(at), 'routes': confirmed}})
            write(conn, model, events)
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) '
                     'DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (key, json.dumps(current, ensure_ascii=False), now.isoformat()))
