"""Read-only replay of a 10g bank-gold purchase at publication, after confirmation, or never."""
import argparse
import json
from datetime import datetime, timezone

from .config import Settings
from .jd_live_buy_watch import MAX_GAP, advance, extended, utc_text, valid_route
from .jd_live_conditions import stamp
from .participation_audit import horizon_at, readonly
from .trade_costs import number
from .trading_hours import trading_session

GRAMS = 10


def complete_sampling(start, end, points):
    """A closed period does not count as missing intraday quotes."""
    times = [start, *(p[0] for p in points if start < p[0] < end), end]
    for left, right in zip(times, times[1:]):
        while left < right:
            session = trading_session(left)
            if session['is_open']:
                close = stamp(session['close_at'])
                if min(right, close) - left > MAX_GAP:
                    return False
                left = close
            else:
                left = stamp(session['next_open'])
    return True


def unavailable(status):
    return {'status': status}


def entries(value, published, expires, quotes, now):
    fee = number(value.get('input_sell_fee_rate'))
    minimum = number(value.get('input_minimum_buy_grams'))
    cash = number(value.get('input_participation_cash_cny'))
    if minimum and minimum > GRAMS:
        return [unavailable('below_bank_minimum')] * 2
    if (value.get('input_data_status') or {}).get('account') != 'fresh' or value.get('input_quote_status') != 'fresh':
        return [unavailable('input_not_fresh')] * 2
    if cash is None or fee is None or fee >= 1:
        return [unavailable('cash_or_fee_unknown')] * 2

    actionable = [p for p in quotes if published <= p[0] <= p[1] < expires]
    first = next((p for p in actionable if p[1] - published <= MAX_GAP), None)
    if first:
        immediate = first if first[2] * GRAMS <= cash else unavailable('insufficient_input_cash')
    else:
        immediate = unavailable('pending' if now < min(expires, published + MAX_GAP) else 'publication_quote_missing')

    from .jd_live_semantics import blocking_checks
    routes = [c for c in value.get('review_conditions', []) if valid_route(c, minimum)]
    if not routes or blocking_checks(value):
        return immediate, unavailable('no_valid_buy_review')

    watch_end = stamp((value.get('buy_watch') or {}).get('expires_at'))
    deadlines = [watch_end if watch_end and extended(route) else expires for route in routes]
    last_deadline = max(deadlines)
    actionable = [p for p in quotes if published <= p[0] <= p[1] < last_deadline]
    states = [{} for _ in routes]
    too_expensive = False
    for point in actionable:
        for i, route in enumerate(routes):
            if point[1] >= deadlines[i]:
                continue
            previous = states[i]
            states[i] = advance(previous, route, point[0], point[2])
            if states[i].get('stage') == 'confirmed' and previous.get('stage') != 'confirmed':
                if point[2] * GRAMS <= cash:
                    return immediate, point
                too_expensive = True
    if now < last_deadline:
        return immediate, unavailable('pending')
    if not complete_sampling(published, last_deadline, actionable):
        return immediate, unavailable('confirmation_quotes_missing')
    return immediate, unavailable('insufficient_input_cash' if too_expensive else 'no_confirmation')


def mark(entry, exit_quote, fee, quotes):
    if isinstance(entry, dict):
        return entry.copy()
    quoted_at, observed_at, buy, _ = entry
    if not exit_quote:
        return {'status': 'horizon_quote_missing', 'entry_at': utc_text(observed_at), 'entry_cny_per_gram': buy}
    end_at = exit_quote[0]
    if end_at < quoted_at:
        return {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
    marks = [p for p in quotes if quoted_at <= p[0] <= end_at and p[1] <= exit_quote[1]]
    net = lambda sell: (sell * (1 - fee) - buy) * GRAMS
    complete = complete_sampling(quoted_at, end_at, marks)
    return {'status': 'valued', 'entry_at': utc_text(observed_at), 'entry_cny_per_gram': buy,
            'net_cny': round(net(exit_quote[3]), 2),
            'sampled_adverse_excursion_cny': round(max(0., -min(net(p[3]) for p in marks)), 2) if complete else None,
            'mark_samples': len(marks)}


def evaluate(value, published, expires, quotes, now):
    immediate, confirmed = entries(value, published, expires, quotes, now)
    fee = number(value.get('input_sell_fee_rate'))
    horizons = []
    for days in (1, 3):
        target = horizon_at(published.isoformat(), days)
        session = trading_session(target)
        if not session['is_open']:
            target = stamp(session['next_open'])
        exit_quote = next((p for p in quotes if target <= p[0] <= p[1] <= target + MAX_GAP), None)
        row = {'trading_days': days, 'target_at': target.isoformat(), 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if exit_quote:
            row['exit_quote_at'] = utc_text(exit_quote[0])
        for label, entry in (('immediate_10g', immediate), ('confirmed_10g', confirmed)):
            if not isinstance(entry, dict) and entry[1] > target:
                result = {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
            elif exit_quote is None and now < target + MAX_GAP and not isinstance(entry, dict):
                result = unavailable('pending')
            else:
                result = mark(entry, exit_quote, fee, quotes)
            if result['status'] == 'no_confirmation':
                result['net_cny'] = 0.0
            if result.get('status') == 'valued':
                delta = result['net_cny']
                result['missed_gain_by_waiting_cny'] = max(0., delta)
                result['loss_avoided_by_waiting_cny'] = max(0., -delta)
            row[label] = result
        horizons.append(row)
    return horizons


def report(conn, bank, model, limit, now):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone():
        return {'status': 'sampling_not_initialized', 'cases': []}
    cases = []
    recent = conn.execute('SELECT id,created_at,expires_at,advice_json FROM jd_live_advice '
                          'WHERE bank=? AND analyst=? ORDER BY id DESC LIMIT ?', (bank, model, limit)).fetchall()
    for advice_id, created, expires, raw in recent:
        published, end = stamp(created), stamp(expires)
        if not published or not end or published > now:
            continue
        rows = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                            'WHERE bank=? AND quoted_at>=? AND quoted_at<=? ORDER BY quoted_at',
                            (bank, utc_text(published), utc_text(now))).fetchall()
        points = [(stamp(q), stamp(o), buy, sell) for q, o, buy, sell in rows if stamp(o) <= now]
        value = json.loads(raw)
        watch = value.get('buy_watch')
        if watch:
            replacement = conn.execute("SELECT created_at FROM jd_live_advice WHERE bank=? AND analyst=? "
                                       "AND id>? AND COALESCE(json_extract(advice_json,'$.buy_watch.id'),'')!=? "
                                       "ORDER BY id LIMIT 1", (bank, model, advice_id, watch['id'])).fetchone()
            if replacement:
                watch['expires_at'] = utc_text(min(stamp(watch['expires_at']), stamp(replacement[0])))
        cases.append({'advice_id': advice_id, 'published_at': published.isoformat(),
                      'horizons': evaluate(value, published, end, points, now)})
    return {'bank': bank, 'model': model, 'as_of': now.isoformat(), 'grams': GRAMS, 'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', choices=('zheshang', 'minsheng'), default='zheshang')
    parser.add_argument('--model', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--limit', type=int, choices=range(1, 101), metavar='1..100', default=12)
    options = parser.parse_args()
    conn = readonly(Settings().db_path)
    try:
        print(json.dumps(report(conn, options.bank, options.model, options.limit, datetime.now(timezone.utc)),
                         ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
