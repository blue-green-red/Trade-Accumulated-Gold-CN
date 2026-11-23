"""Read-only replay of a 10g bank-gold purchase at publication, after confirmation, or never."""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import Settings
from .jd_live_buy_watch import MAX_GAP, advance, eligible, utc_text, watch_deadline
from .jd_live_conditions import stamp
from .jd_live_buy_guard import ENTRY_EVENT_PREFIX
from .participation_audit import horizon_at, readonly
from .trade_costs import bank_review_payoff, number
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


def entries(value, published, expires, quotes, now, *, breakout_mode=None, progress=None):
    fee = number(value.get('input_sell_fee_rate'))
    minimum = number(value.get('input_minimum_buy_grams'))
    cash = number(value.get('input_participation_cash_cny'))
    if minimum and minimum > GRAMS:
        return unavailable('below_bank_minimum'), unavailable('below_bank_minimum'), None
    if (value.get('input_data_status') or {}).get('account') != 'fresh' or value.get('input_quote_status') != 'fresh':
        return unavailable('input_not_fresh'), unavailable('input_not_fresh'), None
    if cash is None or fee is None or fee >= 1:
        return unavailable('cash_or_fee_unknown'), unavailable('cash_or_fee_unknown'), None

    actionable = [p for p in quotes if published <= p[0] <= p[1] < expires]
    first = next((p for p in actionable if p[1] - published <= MAX_GAP), None)
    if first:
        immediate = first if first[2] * GRAMS <= cash else unavailable('insufficient_input_cash')
    else:
        immediate = unavailable('pending' if now < min(expires, published + MAX_GAP) else 'publication_quote_missing')

    from .jd_live_semantics import blocking_checks
    routes = [c for c in value.get('review_conditions', []) if eligible(c, minimum)]
    if not routes or blocking_checks(value):
        return immediate, unavailable('no_valid_buy_review'), None

    watch_end = stamp((value.get('buy_watch') or {}).get('expires_at'))
    advice = {**value, 'expires_at': utc_text(expires)}
    deadlines = [watch_deadline(advice, route) for route in routes]
    last_deadline = max(deadlines)
    if last_deadline <= published:
        return immediate, unavailable('no_valid_buy_review'), None
    boundary = number((value.get('position_path') or {}).get('boundary_price_cny'))
    actionable = [p for p in quotes if published <= p[0] <= p[1] < last_deadline]
    states = [{} for _ in routes]
    too_expensive = False
    for point in actionable:
        if watch_end and boundary is not None and point[3] <= boundary:
            last_deadline = point[1]
            if progress is not None:
                progress['last_stage'] = 'position_boundary'
            break
        for i, route in enumerate(routes):
            if point[1] >= deadlines[i]:
                continue
            previous = states[i]
            states[i] = advance(previous, route, point[0], point[2], breakout_mode=breakout_mode)
            if progress is not None:
                buy, trigger = point[2], route['price_cny']
                gap = max(0., buy-trigger if route['condition']=='at_or_below' else trigger-buy)
                progress['closest_trigger_gap_cny_per_gram'] = min(
                    gap, progress.get('closest_trigger_gap_cny_per_gram', gap))
                progress['quote_samples_examined'] = progress.get('quote_samples_examined', 0) + 1
                progress['last_stage'] = states[i].get('stage') or 'awaiting_trigger'
            if states[i].get('stage') == 'confirmed' and previous.get('stage') != 'confirmed':
                if point[2] * GRAMS <= cash:
                    branch = review_branch({**route, 'confirmation_mode': states[i].get('confirmation_mode')})
                    return immediate, point, branch
                too_expensive = True
    if now < last_deadline:
        return immediate, unavailable('pending'), None
    if not actionable or not complete_sampling(published, last_deadline, actionable):
        return immediate, unavailable('confirmation_quotes_missing'), None
    return immediate, unavailable('insufficient_input_cash' if too_expensive else 'no_confirmation'), None


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
    adverse = drawdown = None
    if complete:
        peak = low = drawdown = 0.
        for _, _, _, sell in marks:
            value = net(sell)
            peak = max(peak, value)
            low = min(low, value)
            drawdown = max(drawdown, peak - value)
        adverse, drawdown = round(-low,2), round(drawdown,2)
    return {'status': 'valued', 'entry_at': utc_text(observed_at), 'entry_cny_per_gram': buy,
            'net_cny': round(net(exit_quote[3]), 2), 'sampled_adverse_excursion_cny': adverse,
            'sampled_max_drawdown_cny': drawdown, 'mark_samples': len(marks)}


def trading_horizon(anchor, days):
    target = horizon_at(anchor.isoformat(), days)
    session = trading_session(target)
    if not session['is_open']:
        target = stamp(session['next_open'])
        session = trading_session(target)
    deadline = min(target + MAX_GAP, stamp(session['close_at']) - timedelta(microseconds=1))
    return target, deadline


def evaluate_entries(immediate, confirmed, branch, published, fee, quotes, now):
    horizons = []
    for days in (1, 3):
        target, deadline = trading_horizon(published, days)
        exit_quote = next((p for p in quotes if target <= p[0] <= p[1] <= deadline), None)
        row = {'trading_days': days, 'target_at': target.isoformat(), 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if branch:
            row['confirmation_branch'] = branch
        if exit_quote:
            row['exit_quote_at'] = utc_text(exit_quote[0])
        for label, entry in (('immediate_10g', immediate), ('confirmed_10g', confirmed)):
            if not isinstance(entry, dict) and entry[1] > target:
                result = {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
            elif exit_quote is None and now < deadline and not isinstance(entry, dict):
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


def evaluate(value, published, expires, quotes, now):
    immediate, confirmed, branch = entries(value, published, expires, quotes, now)
    return evaluate_entries(immediate, confirmed, branch, published, number(value.get('input_sell_fee_rate')), quotes, now)


def replay_one_branch(value, condition, mode, published, expires, quotes, now):
    branch = review_branch({**condition, 'confirmation_mode': mode})
    fee = number(value.get('input_sell_fee_rate'))
    progress = {}
    immediate, confirmation, _ = entries({**value, 'review_conditions': [condition]}, published, expires, quotes, now,
                                         breakout_mode=mode, progress=progress)
    gap = progress.get('closest_trigger_gap_cny_per_gram')
    if gap is not None:
        progress['closest_trigger_gap_cny_per_gram'] = round(gap,2)
    outcomes = evaluate_entries(immediate, confirmation, branch, published, fee, quotes, now)
    evidence = (confirmation if isinstance(confirmation, dict) else
                {'status': 'confirmed', 'quoted_at': utc_text(confirmation[0]),
                 'observed_at': utc_text(confirmation[1]), 'buy_cny_per_gram': confirmation[2]})
    return {'branch': branch, 'threshold_cny': condition['price_cny'], 'confirmation': evidence,
            'observation_progress': progress,
            'horizons': [{'trading_days': row['trading_days'], 'target_at': row['target_at'],
                          'confirmed_10g': row['confirmed_10g']} for row in outcomes]}


def shallow_level_sensitivity(value, condition, archived_quotes, published, expires, quotes, now):
    """Test an alternative zone anchored to the analysis inputs available at the time."""
    anchor = stamp(archived_quotes.get('as_of'))
    lows = [low for w in archived_quotes.get('buy_price_windows_15m', []) if (low := number(w.get('low')))]
    if (archived_quotes.get('status') != 'available' or not anchor or anchor > published
            or anchor != stamp(value.get('input_quote_at')) or not lows):
        return unavailable('archived_bank_quotes_unavailable')
    sampled_low = min(lows)
    shift = round(sampled_low - condition['price_cny'], 2)
    if shift <= 0:
        return {'status': 'already_at_or_above_sampled_low', 'sampled_low_cny': sampled_low}
    review = condition['buy_review']
    zone = review['zone']
    candidate_review = {**review, 'recovery_price_cny': round(review['recovery_price_cny'] + shift, 2),
                        'max_buy_price_cny': round(review['max_buy_price_cny'] + shift, 2),
                        'zone': {**zone, 'floor_cny': round(zone['floor_cny'] + shift, 2)}}
    candidate = {**condition, 'price_cny': sampled_low, 'buy_review': candidate_review}
    result = {'sampled_low_cny': sampled_low, 'trigger_shift_cny_per_gram': shift,
              'candidate_max_buy_cny': candidate_review['max_buy_price_cny']}
    if not eligible(candidate, number(value.get('input_minimum_buy_grams'))):
        return {'status': 'shifted_zone_incompatible_with_original_terms', **result}
    payoff = bank_review_payoff(candidate_review['max_buy_price_cny'], GRAMS, zone, value.get('input_sell_fee_rate'))
    cutoff = watch_deadline({**value, 'expires_at': utc_text(expires)}, condition)
    if watch := value.get('buy_watch'):
        value = {**value, 'buy_watch': {**watch, 'expires_at': utc_text(cutoff)}}
    return {'status': 'replayed', **result, 'payoff_10g_at_candidate_cap_cny': payoff,
            'replay': replay_one_branch(value, candidate, None, published, min(expires, cutoff), quotes, now)}


def independent_branch_replay(value, published, expires, quotes, now, *, archived_quotes=None):
    """Use each condition alone and preserve the original observation window."""
    watch = value.get('buy_watch')
    if watch and stamp(watch.get('starts_at')) != published:
        return []
    minimum = number(value.get('input_minimum_buy_grams'))
    cases = []
    for condition in value.get('review_conditions', []):
        if not eligible(condition, minimum):
            continue
        modes = ('continuation', 'retest') if condition.get('bank_breakout') else (None,)
        for mode in modes:
            result = replay_one_branch(value, condition, mode, published, expires, quotes, now)
            if result['branch'] == 'shallow_pullback' and archived_quotes is not None:
                result['sampled_low_sensitivity'] = shallow_level_sensitivity(value, condition, archived_quotes,
                                                                             published, expires, quotes, now)
            cases.append(result)
    return cases


def review_branch(route):
    if route.get('bank_breakout'):
        mode = route.get('confirmation_mode')
        return 'breakout_continuation' if mode == 'continuation' else (
            'breakout_retest' if mode == 'retest' else 'breakout_observation')
    if route.get('buy_review'):
        if route.get('condition') == 'at_or_above':
            return 'breakout_entry_retest'
        return 'shallow_pullback' if route['buy_review'].get('zone') else 'pullback_recovery'
    return 'unclassified'


def advice_outcome(run_status, event_id, covered, value):
    from .jd_live_semantics import blocking_checks
    if run_status != 'completed':
        return run_status
    if covered is None:
        return 'coverage_unknown'
    if event_id not in covered:
        return 'not_covered'
    if not value:
        return 'no_advice'
    if blocking_checks(value):
        return 'needs_review'
    if value.get('action') == 'buy':
        return 'buy_suggested'
    if any(order.get('kind') == 'limit_buy' and not order.get('checks')
           for order in value.get('suggested_orders', [])):
        return 'conditional_buy_suggested'
    return 'no_buy_suggested'


def elapsed_seconds(start, end):
    return round((end - start).total_seconds(), 1) if start and end and end >= start else None


def first_observed_quote(conn, bank, start, cutoff):
    if cutoff < start:
        return None
    return conn.execute(
        'SELECT quoted_at,buy_price_cny,sell_price_cny,observed_at FROM jd_live_quote_samples '
        'WHERE bank=? AND quoted_at BETWEEN ? AND ? AND observed_at>=quoted_at AND observed_at<=? '
        'ORDER BY quoted_at LIMIT 1', (bank, utc_text(start), utc_text(cutoff), utc_text(cutoff))).fetchone()


def followup_quote(conn, bank, confirmed, price, minutes, now, *, fee=None, publication_quote=None):
    result = {'minutes': minutes}
    if not confirmed or price is None:
        return {**result, 'status': 'signal_unavailable'}
    target = confirmed + timedelta(minutes=minutes)
    session = trading_session(target)
    if not session['is_open']:
        return {**result, 'status': 'market_closed'}
    deadline = min(target + MAX_GAP, stamp(session['close_at']) - timedelta(microseconds=1))
    quote = first_observed_quote(conn, bank, target, min(deadline, now))
    if not quote:
        return {**result, 'status': 'pending' if now < deadline else 'quote_missing'}
    result.update(status='observed', quoted_at=quote[0], change_cny_per_gram=round(quote[1] - price, 2))
    if fee is not None and fee < 1:
        exit_net = quote[2] * (1 - fee)
        after_analysis = (round(GRAMS * (exit_net - publication_quote[1]), 2)
                          if publication_quote and stamp(publication_quote[3]) < target else None)
        result['hypothetical_10g_net_cny'] = {'at_confirmation': round(GRAMS * (exit_net - price), 2),
                                              'after_analysis': after_analysis, 'wait': 0.0}
    return result


def observed_review_horizons(conn, bank, confirmed, price, fee, published, publication_quote, now):
    """Mark fixed-size hypothetical additions at common trading-day horizons."""
    horizons = []
    for days in (1, 3):
        row = {'trading_days': days, 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if not confirmed or not price:
            row.update(at_confirmation_10g=unavailable('signal_unavailable'), after_analysis_10g=unavailable('signal_unavailable'))
            horizons.append(row)
            continue
        target, deadline = trading_horizon(confirmed, days)
        row['target_at'] = utc_text(target)
        cutoff = min(now, deadline)
        exit_quote = first_observed_quote(conn, bank, target, cutoff)
        if not exit_quote:
            status = 'pending' if now < deadline else 'horizon_quote_missing'
            row.update(at_confirmation_10g=unavailable(status), after_analysis_10g=unavailable(status))
            horizons.append(row)
            continue
        row['exit_quote_at'] = exit_quote[0]
        if fee is None or fee >= 1:
            row.update(at_confirmation_10g=unavailable('fee_unknown'), after_analysis_10g=unavailable('fee_unknown'))
            horizons.append(row)
            continue
        sampled = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                               'WHERE bank=? AND quoted_at>=? AND quoted_at<? AND observed_at>=quoted_at AND observed_at<=? '
                               'ORDER BY quoted_at', (bank, utc_text(confirmed), exit_quote[0], exit_quote[3]))
        quotes = [(stamp(quoted), stamp(seen), buy, sell) for quoted, seen, buy, sell in sampled]
        exit_point = stamp(exit_quote[0]), stamp(exit_quote[3]), exit_quote[1], exit_quote[2]
        quotes.append(exit_point)
        entry = quotes[0] if quotes[0][0] == confirmed and quotes[0][2] == price else unavailable('confirmation_quote_missing')
        if published is None:
            later = unavailable('analysis_not_published')
        elif published >= target or (publication_quote and stamp(publication_quote[3]) >= target):
            later = {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
        elif not publication_quote:
            later = unavailable('publication_quote_missing')
        else:
            quoted, buy, sell, seen = publication_quote
            later = stamp(quoted), stamp(seen), buy, sell
        row.update(at_confirmation_10g=mark(entry, exit_point, fee, quotes), after_analysis_10g=mark(later, exit_point, fee, quotes))
        horizons.append(row)
    return horizons


def observed_reviews(conn, bank, model, limit, now):
    """Actual review events and outcomes, with a separately labeled quote comparison."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('ai_runs','ai_views')")}
    if tables != {'ai_runs', 'ai_views'}:
        return {'status': 'history_unavailable', 'cases': [], 'by_branch': {}}
    rows = conn.execute("""
        SELECT r.id,r.run_id,r.status,r.started_at,r.completed_at,
               json_extract(v.view_json,'$._covered_event_ids'),
               a.id,a.created_at,a.advice_json,
               json_extract(s.value,'$.trigger_events'),
               json_extract(s.value,'$.event_coverage'),s.key IS NOT NULL
        FROM (SELECT * FROM ai_runs WHERE analyst=? AND started_at<=? ORDER BY id DESC LIMIT ?) r
        LEFT JOIN state s ON s.key='ai_model_input:'||r.run_id||':'||r.analyst
        LEFT JOIN ai_views v ON v.rowid=r.view_rowid
        LEFT JOIN jd_live_advice a ON a.view_id=r.view_rowid AND a.analyst=r.analyst AND a.bank=?
        ORDER BY r.id DESC
    """, (model, utc_text(now), limit, bank)).fetchall()
    cases = []
    summary = defaultdict(lambda: defaultdict(int))
    for attempt, run_id, status, start, finish, covered, advice_id, published, raw, events, sent, _ in rows:
        if not events or not sent:
            continue
        event_ids = set(json.loads(sent).get(model, []))
        value = json.loads(raw) if raw else {}
        fee = number(value.get('input_sell_fee_rate'))
        events = [e for e in json.loads(events) if e['id'] in event_ids and e['id'].startswith(ENTRY_EVENT_PREFIX)
                  and e.get('data', {}).get('bank') == bank]
        if not events:
            continue
        released = stamp(published)
        started = stamp(start)
        finished = stamp(finish)
        covered_ids = json.loads(covered) if covered is not None else None
        mark = first_observed_quote(conn, bank, released, min(now, released + MAX_GAP)) if released else None
        counted = set()
        for e in events:
            outcome = advice_outcome(status, e['id'], covered_ids, value)
            paths = []
            for route in e['data'].get('routes', []):
                branch = review_branch(route)
                confirmed = stamp(route.get('confirmed_at'))
                price = number(route.get('price_cny'))
                paths.append({'branch': branch, 'threshold_cny': route.get('threshold_cny'),
                              'confirmed_at': route.get('confirmed_at'), 'confirmed_buy_cny': price,
                              'seconds_to_start': elapsed_seconds(confirmed, started),
                              'seconds_to_completion': elapsed_seconds(confirmed, finished),
                              'quote_change_to_publication_cny': round(mark[1] - price, 2) if mark and price else None,
                              'quote_followups': [followup_quote(conn, bank, confirmed, price, minutes, now,
                                                                 fee=fee, publication_quote=mark)
                                                  for minutes in (15, 60)],
                              'trading_day_followups': observed_review_horizons(conn, bank, confirmed, price, fee,
                                                                                 released, mark, now)})
                if (branch, outcome) not in counted:
                    summary[branch][outcome] += 1
                    counted.add((branch, outcome))
            cases.append({'run_id': run_id, 'attempt_id': attempt, 'event_id': e['id'], 'started_at': start, 'completed_at': finish,
                          'analysis_seconds': elapsed_seconds(started, finished),
                          'advice_id': advice_id, 'advice_action': value.get('action'), 'outcome': outcome,
                          'post_publication_quote_at': mark[0] if mark else None, 'paths': paths})
    archived = sum(row[-1] for row in rows)
    return {'status': 'available' if archived else 'input_archives_unavailable',
            'examined_runs': len(rows), 'archived_runs': archived, 'cases': cases, 'by_branch': summary}


def report(conn, bank, model, limit, now):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone():
        return {'status': 'sampling_not_initialized', 'cases': []}
    cases = []
    recent = conn.execute('SELECT id,created_at,expires_at,advice_json,view_id FROM jd_live_advice '
                          'WHERE bank=? AND analyst=? ORDER BY id DESC LIMIT ?', (bank, model, limit)).fetchall()
    has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_views'").fetchone()
    for advice_id, created, expires, raw, view_id in recent:
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
        archived_quotes = {}
        if has_history and any(review_branch(c)=='shallow_pullback' for c in value.get('review_conditions', [])):
            entry = conn.execute('SELECT json_extract(s.value, ?) FROM ai_views v JOIN state s '
                                 "ON s.key='ai_model_input:'||v.run_id||':'||v.analyst WHERE v.rowid=? AND v.analyst=?",
                                 (f'$.live_readonly_accounts.accounts.{bank}.recent_bank_quotes', view_id, model)).fetchone()
            if entry and entry[0]:
                archived_quotes = json.loads(entry[0])
        cases.append({'advice_id': advice_id, 'published_at': published.isoformat(),
                      'horizons': evaluate(value, published, end, points, now),
                      'independent_branch_replay': independent_branch_replay(value, published, end, points, now,
                                                                            archived_quotes=archived_quotes)})
    return {'bank': bank, 'model': model, 'as_of': now.isoformat(), 'grams': GRAMS, 'cases': cases,
            'observed_reviews': observed_reviews(conn, bank, model, limit, now)}


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
