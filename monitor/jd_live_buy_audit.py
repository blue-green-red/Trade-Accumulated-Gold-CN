"""Read-only replay of a hypothetical bank-gold purchase, with a configurable size."""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import Settings
from .jd_live_buy_watch import MAX_GAP, advance_watch, eligible, utc_text, watch_deadline, watch_routes
from .jd_live_conditions import archived_bank_quotes, pullback_depth, stamp, zoned_pullback
from .jd_live_buy_guard import ENTRY_EVENT_PREFIX, confirmation_key, stored_price_condition
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


def entries(value, published, expires, quotes, now, *, grams=GRAMS, breakout_mode=None, progress=None):
    fee = number(value.get('input_sell_fee_rate'))
    minimum = number(value.get('input_minimum_buy_grams'))
    cash = number(value.get('input_participation_cash_cny'))
    blocked = None
    if minimum and minimum > grams:
        blocked = 'below_bank_minimum'
    elif (value.get('input_data_status') or {}).get('account') != 'fresh' or value.get('input_quote_status') != 'fresh':
        blocked = 'input_not_fresh'
    elif cash is None or fee is None or fee >= 1:
        blocked = 'cash_or_fee_unknown'

    actionable = [p for p in quotes if published <= p[0] <= p[1] < expires and p[1] <= now]
    first = next((p for p in actionable if p[1] - published <= MAX_GAP), None)
    if blocked:
        immediate = unavailable(blocked)
    elif first:
        immediate = first if first[2] * grams <= cash + 1e-8 else unavailable('insufficient_input_cash')
    else:
        immediate = unavailable('pending' if now < min(expires, published + MAX_GAP) else 'publication_quote_missing')

    from .jd_live_semantics import blocking_checks
    routes = [route for route in watch_routes(c for c in value.get('review_conditions', []) if eligible(c, minimum))
              if breakout_mode is None or route.get('confirmation_mode') == breakout_mode]
    if not routes or blocking_checks(value):
        return immediate, unavailable(blocked or 'no_valid_buy_review'), None

    watch_end = stamp((value.get('buy_watch') or {}).get('expires_at'))
    advice = {**value, 'expires_at': utc_text(expires)}
    deadlines = [watch_deadline(advice, route) for route in routes]
    last_deadline = max(deadlines)
    if last_deadline <= published:
        return immediate, unavailable(blocked or 'no_valid_buy_review'), None
    boundary = number((value.get('position_path') or {}).get('boundary_price_cny'))
    actionable = [p for p in quotes if published <= p[0] <= p[1] < last_deadline and p[1] <= now]
    states = [{} for _ in routes]
    unfunded = {}
    for point in actionable:
        if watch_end and boundary is not None and point[3] <= boundary:
            last_deadline = point[1]
            if progress is not None:
                progress['last_stage'] = 'position_boundary'
            break
        for i, route in enumerate(routes):
            if point[1] >= deadlines[i] or i in unfunded:
                continue
            states[i] = advance_watch(states[i], route, point[0], point[2])
            if progress is not None:
                buy, trigger = point[2], route['price_cny']
                gap = max(0., buy-trigger if route['condition']=='at_or_below' else trigger-buy)
                progress['closest_trigger_gap_cny_per_gram'] = min(
                    gap, progress.get('closest_trigger_gap_cny_per_gram', gap))
                progress['quote_samples_examined'] = progress.get('quote_samples_examined', 0) + 1
                progress['last_stage'] = states[i].get('stage') or 'awaiting_trigger'
            if states[i].get('stage') == 'confirmed':
                branch = review_branch(route)
                error = blocked or ('insufficient_input_cash' if point[2] * grams > cash + 1e-8 else None)
                if not error:
                    return immediate, point, branch
                unfunded[i] = {'branch': branch, 'quoted_at': utc_text(point[0]),
                               'observed_at': utc_text(point[1]), 'buy_cny_per_gram': point[2],
                               'paper_entry_status': error}
        if len(unfunded) == len(routes):
            break
    if blocked or len(unfunded) == len(routes):
        status = blocked or 'insufficient_input_cash'
    elif now < last_deadline:
        status = 'pending'
    elif not actionable or not complete_sampling(published, last_deadline, actionable):
        status = 'confirmation_quotes_missing'
    else:
        status = 'insufficient_input_cash' if unfunded else 'no_confirmation'
    confirmation = unavailable(status)
    if unfunded:
        confirmation['observed_price_confirmations'] = list(unfunded.values())
    return immediate, confirmation, None


def mark(entry, exit_quote, fee, quotes, grams):
    if isinstance(entry, dict):
        return entry.copy()
    quoted_at, observed_at, buy, _ = entry
    if not exit_quote:
        return {'status': 'horizon_quote_missing', 'entry_at': utc_text(observed_at), 'entry_cny_per_gram': buy}
    end_at = exit_quote[0]
    if end_at < quoted_at:
        return {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
    marks = [p for p in quotes if quoted_at <= p[0] <= end_at and observed_at <= p[1] <= exit_quote[1]]
    net = lambda sell: (sell * (1 - fee) - buy) * grams
    complete = complete_sampling(quoted_at, end_at, marks)
    adverse = drawdown = best = first_break_even = None
    if complete:
        peak = low = drawdown = 0.
        for _, seen, _, sell in marks:
            value = net(sell)
            peak = max(peak, value)
            low = min(low, value)
            drawdown = max(drawdown, peak - value)
            best = value if best is None else max(best, value)
            if value >= -1e-8 and (first_break_even is None or seen < first_break_even):
                first_break_even = seen
        adverse, drawdown = round(-low,2), round(drawdown,2)
    return {'status': 'valued', 'entry_at': utc_text(observed_at), 'entry_cny_per_gram': buy,
            'net_cny': round(net(exit_quote[3]), 2), 'sampled_adverse_excursion_cny': adverse,
            'sampled_best_net_cny': round(best, 2) if best is not None else None,
            'sampled_first_break_even_at': utc_text(first_break_even) if first_break_even else None,
            'sampled_max_drawdown_cny': drawdown, 'mark_samples': len(marks)}


def trading_horizon(anchor, days):
    target = horizon_at(anchor.isoformat(), days)
    session = trading_session(target)
    if not session['is_open']:
        target = stamp(session['next_open'])
        session = trading_session(target)
    deadline = min(target + MAX_GAP, stamp(session['close_at']) - timedelta(microseconds=1))
    return target, deadline


def evaluate_entries(immediate, confirmed, branch, published, fee, quotes, now, grams):
    horizons = []
    for days in (1, 3):
        target, deadline = trading_horizon(published, days)
        exit_quote = next((p for p in quotes if target <= p[0] <= p[1] <= min(now, deadline)), None)
        row = {'trading_days': days, 'target_at': target.isoformat(), 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if branch:
            row['confirmation_branch'] = branch
        if exit_quote:
            row['exit_quote_at'] = utc_text(exit_quote[0])
        for label, entry in ((f'immediate_{grams:g}g', immediate), (f'confirmed_{grams:g}g', confirmed)):
            if not isinstance(entry, dict) and entry[1] > target:
                result = {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
            elif exit_quote is None and now < deadline and not isinstance(entry, dict):
                result = unavailable('pending')
            else:
                result = mark(entry, exit_quote, fee, quotes, grams)
            if result['status'] == 'no_confirmation':
                result['net_cny'] = 0.0
            if result.get('status') == 'valued':
                delta = result['net_cny']
                result['missed_gain_by_waiting_cny'] = max(0., delta)
                result['loss_avoided_by_waiting_cny'] = max(0., -delta)
            row[label] = result
        horizons.append(row)
    return horizons


def evaluate(value, published, expires, quotes, now, *, grams=GRAMS):
    immediate, confirmed, branch = entries(value, published, expires, quotes, now, grams=grams)
    return evaluate_entries(immediate, confirmed, branch, published, number(value.get('input_sell_fee_rate')), quotes, now, grams)


def replay_one_branch(value, condition, mode, published, expires, quotes, now, grams):
    branch = review_branch({**condition, 'confirmation_mode': mode})
    fee = number(value.get('input_sell_fee_rate'))
    progress = {}
    immediate, confirmation, _ = entries({**value, 'review_conditions': [condition]}, published, expires, quotes, now,
                                         grams=grams, breakout_mode=mode, progress=progress)
    gap = progress.get('closest_trigger_gap_cny_per_gram')
    if gap is not None:
        progress['closest_trigger_gap_cny_per_gram'] = round(gap,2)
    outcomes = evaluate_entries(immediate, confirmation, branch, published, fee, quotes, now, grams)
    key = f'confirmed_{grams:g}g'
    evidence = (confirmation if isinstance(confirmation, dict) else
                {'status': 'confirmed', 'quoted_at': utc_text(confirmation[0]),
                 'observed_at': utc_text(confirmation[1]), 'buy_cny_per_gram': confirmation[2]})
    return {'branch': branch, 'threshold_cny': condition['price_cny'], 'confirmation': evidence,
            'observation_progress': progress,
            'horizons': [{'trading_days': row['trading_days'], 'target_at': row['target_at'],
                          key: row[key]} for row in outcomes]}


def shallow_level_sensitivity(value, condition, archived_quotes, published, expires, quotes, now, grams):
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
    candidate['pullback_depth'] = pullback_depth(candidate, archived_quotes, value.get('input_quote_at'), published)
    result = {'sampled_low_cny': sampled_low, 'trigger_shift_cny_per_gram': shift,
              'candidate_max_buy_cny': candidate_review['max_buy_price_cny']}
    if not eligible(candidate, number(value.get('input_minimum_buy_grams'))):
        return {'status': 'shifted_zone_incompatible_with_original_terms', **result}
    payoff = bank_review_payoff(candidate_review['max_buy_price_cny'], grams, zone, value.get('input_sell_fee_rate'))
    cutoff = watch_deadline({**value, 'expires_at': utc_text(expires)}, condition)
    if watch := value.get('buy_watch'):
        value = {**value, 'buy_watch': {**watch, 'expires_at': utc_text(cutoff)}}
    return {'status': 'replayed', **result, f'payoff_{grams:g}g_at_candidate_cap_cny': payoff,
            'replay': replay_one_branch(value, candidate, None, published, min(expires, cutoff), quotes, now, grams)}


def independent_branch_replay(value, published, expires, quotes, now, *, archived_quotes=None, grams=GRAMS):
    """Use each condition alone and preserve the original observation window."""
    watch = value.get('buy_watch')
    if watch and stamp(watch.get('starts_at')) != published:
        return []
    minimum = number(value.get('input_minimum_buy_grams'))
    cases = []
    for condition in value.get('review_conditions', []):
        if not eligible(condition, minimum):
            continue
        if archived_quotes and 'pullback_depth' not in condition and (depth := pullback_depth(
                condition, archived_quotes, value.get('input_quote_at'), published)):
            condition = {**condition, 'pullback_depth': depth}
        modes = ('continuation', 'retest') if condition.get('bank_breakout') else (None,)
        for mode in modes:
            result = replay_one_branch(value, condition, mode, published, expires, quotes, now, grams)
            if zoned_pullback(condition) and archived_quotes is not None:
                result['sampled_low_sensitivity'] = shallow_level_sensitivity(value, condition, archived_quotes,
                                                                             published, expires, quotes, now, grams)
            cases.append(result)
    return cases


def review_branch(route):
    if route.get('renewed_pullback'):
        return 'renewed_pullback'
    if route.get('trigger_kind') == 'price_touch':
        return {'at_or_above': 'buy_price_rise_touch', 'at_or_below': 'buy_price_drop_touch'}.get(
            route.get('condition'), 'buy_price_touch')
    if route.get('bank_breakout'):
        mode = route.get('confirmation_mode')
        return 'breakout_continuation' if mode == 'continuation' else (
            'breakout_retest' if mode == 'retest' else 'breakout_observation')
    if route.get('buy_review'):
        if route.get('condition') == 'at_or_above':
            return 'breakout_entry_retest'
        if not zoned_pullback(route):
            return 'pullback_recovery'
        depth = route.get('pullback_depth')
        return f'{depth}_pullback' if depth in ('shallow', 'deep') else 'zoned_pullback'
    return 'unclassified'


def observed_buy_event(event):
    if event['id'].startswith(ENTRY_EVENT_PREFIX):
        return True
    if not event['id'].startswith('live-price:'):
        return False
    data = event['data']
    return (not data.get('order_key') and (data.get('renewed_pullback')
            or data.get('quote_side') == 'buy' and not data.get('buy_review')))


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


def observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target, exit_quote,
                         grams, entry_checks):
    confirmation_key = f'at_confirmation_{grams:g}g'
    publication_key = f'after_analysis_{grams:g}g'
    if fee is None or fee >= 1:
        return {confirmation_key: unavailable('fee_unknown'), publication_key: unavailable('fee_unknown')}
    sampled = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                           'WHERE bank=? AND quoted_at>=? AND quoted_at<? AND observed_at>=quoted_at AND observed_at<=? '
                           'ORDER BY quoted_at', (bank, utc_text(confirmed), exit_quote[0], exit_quote[3]))
    quotes = [(stamp(quoted), stamp(seen), buy, sell) for quoted, seen, buy, sell in sampled]
    exit_point = stamp(exit_quote[0]), stamp(exit_quote[3]), exit_quote[1], exit_quote[2]
    quotes.append(exit_point)
    entry = quotes[0] if quotes[0][0] == confirmed and quotes[0][2] == price else unavailable('confirmation_quote_missing')
    confirm_error, publication_error = entry_checks
    if confirm_error:
        entry = unavailable(confirm_error)
    if published is None:
        later = unavailable('analysis_not_published')
    elif published >= target or (publication_quote and stamp(publication_quote[3]) >= target):
        later = {'status': 'no_entry_by_horizon', 'net_cny': 0.0}
    elif not publication_quote:
        later = unavailable('publication_quote_missing')
    elif publication_error:
        later = unavailable(publication_error)
    else:
        quoted, buy, sell, seen = publication_quote
        later = stamp(quoted), stamp(seen), buy, sell
    return {confirmation_key: mark(entry, exit_point, fee, quotes, grams),
            publication_key: mark(later, exit_point, fee, quotes, grams)}


def followup_quote(conn, bank, confirmed, price, minutes, now, *, fee=None, published=None, publication_quote=None,
                   grams=GRAMS, entry_checks=(None, None)):
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
        after_analysis = (round(grams * (exit_net - publication_quote[1]), 2)
                          if not entry_checks[1] and publication_quote and stamp(publication_quote[3]) < target else None)
        result[f'hypothetical_{grams:g}g_net_cny'] = {
            'at_confirmation': round(grams * (exit_net - price), 2) if not entry_checks[0] else None,
            'after_analysis': after_analysis, 'wait': 0.0}
    result.update(observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target, quote,
                                       grams, entry_checks))
    return result


def observed_review_horizons(conn, bank, confirmed, price, fee, published, publication_quote, now,
                             *, grams=GRAMS, entry_checks=(None, None)):
    """Mark fixed-size hypothetical additions at common trading-day horizons."""
    horizons = []
    entry_keys = (f'at_confirmation_{grams:g}g', f'after_analysis_{grams:g}g')
    for days in (1, 3):
        row = {'trading_days': days, 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if not confirmed or not price:
            row.update({key: unavailable('signal_unavailable') for key in entry_keys})
            horizons.append(row)
            continue
        target, deadline = trading_horizon(confirmed, days)
        row['target_at'] = utc_text(target)
        cutoff = min(now, deadline)
        exit_quote = first_observed_quote(conn, bank, target, cutoff)
        if not exit_quote:
            status = 'pending' if now < deadline else 'horizon_quote_missing'
            row.update({key: unavailable(status) for key in entry_keys})
            horizons.append(row)
            continue
        row['exit_quote_at'] = exit_quote[0]
        row.update(observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target,
                                        exit_quote, grams, entry_checks))
        horizons.append(row)
    return horizons


def observed_reviews(conn, bank, model, limit, now, *, grams=GRAMS):
    """Actual review events and outcomes, with a separately labeled quote comparison."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('ai_runs','ai_views')")}
    if tables != {'ai_runs', 'ai_views'}:
        return {'status': 'history_unavailable', 'cases': [], 'by_branch': {}}
    rows = conn.execute("""
        SELECT r.id,r.run_id,r.status,r.started_at,r.completed_at,
               json_extract(v.view_json,'$._covered_event_ids'),
               a.id,a.created_at,a.advice_json,
               json_extract(s.value,'$.trigger_events'),
               json_extract(s.value,'$.event_coverage'),
               json_extract(v.view_json,'$._valid_entry_confirmations'),json_extract(s.value,?),s.updated_at
        FROM (SELECT * FROM ai_runs WHERE analyst=? AND started_at<=? ORDER BY id DESC LIMIT ?) r
        LEFT JOIN state s ON s.key='ai_model_input:'||r.run_id||':'||r.analyst
        LEFT JOIN ai_views v ON v.rowid=r.view_rowid
        LEFT JOIN jd_live_advice a ON a.view_id=r.view_rowid AND a.analyst=r.analyst AND a.bank=?
        ORDER BY r.id DESC
    """, (f'$.live_readonly_accounts.accounts.{bank}', model, utc_text(now), limit, bank)).fetchall()
    cases = []
    summary = defaultdict(lambda: defaultdict(int))
    for attempt, run_id, status, start, finish, covered, advice_id, published, raw, events, sent, valid, source, archived_at in rows:
        if not events or not (archived_at:=stamp(archived_at)) or archived_at>now:
            continue
        finished=stamp(finish)
        if finished and finished>now:
            status='running'
            finish,finished,covered,advice_id,published,raw,valid=(None,)*7
        # Current Codex archives are scoped to the submitted events and omit the duplicate coverage list.
        sent_ids = set(json.loads(sent).get(model, [])) if sent is not None else None
        value = json.loads(raw) if raw else {}
        account = json.loads(source) if source else {}
        fee = number(account.get('sell_fee_rate', value.get('input_sell_fee_rate')))
        cash = number((account.get('new_buy_capacity_at_input') or {}).get(
            'cash_after_buy_orders_cny', value.get('input_participation_cash_cny')))
        minimum = number(account.get('minimum_buy_grams', value.get('input_minimum_buy_grams')))
        account_status = account.get('status', (value.get('input_data_status') or {}).get('account'))
        quote_status = (account.get('quote') or {}).get('status', value.get('input_quote_status'))
        if account_status not in (None, 'fresh') or quote_status not in (None, 'fresh'):
            capacity_error = 'input_not_fresh'
        elif minimum is not None and grams < minimum:
            capacity_error = 'below_bank_minimum'
        else:
            capacity_error = 'cash_unknown' if cash is None else None
        events = [e for e in json.loads(events)
                  if (sent_ids is None or e['id'] in sent_ids) and e.get('data', {}).get('bank') == bank
                  and observed_buy_event(e)]
        if not events:
            continue
        released = stamp(published)
        started = stamp(start)
        covered_ids = json.loads(covered) if covered is not None else None
        valid_confirmations = json.loads(valid) if valid is not None else {}
        mark = first_observed_quote(conn, bank, released, min(now, released + MAX_GAP)) if released else None
        counted = set()
        for e in events:
            outcome = advice_outcome(status, e['id'], covered_ids, value)
            confirmations = valid_confirmations.get(e['id'])
            valid_keys = {tuple(key) for key in confirmations} if confirmations is not None else None
            buy_basis = {confirmation_key(r) for r in value.get('entry_trigger_routes', [])}
            paths = []
            routes = e['data'].get('routes', [])
            if e['id'].startswith('live-price:'):
                data = e['data']
                condition, _, _ = stored_price_condition(conn, model, data)
                routes = [{**data, 'condition': (condition or {}).get('condition'),
                           'trigger_kind': 'price_touch', 'confirmed_at': data.get('quoted_at')}]
            origin = None
            if any(map(zoned_pullback, routes)):
                origin = conn.execute("SELECT view_id,created_at,json_extract(advice_json,'$.input_quote_at') "
                                      "FROM jd_live_advice WHERE id=? AND bank=? AND analyst=?",
                                      (e['data'].get('advice_id'), bank, model)).fetchone()
            evidence = archived_bank_quotes(conn, bank, model, origin[0]) if origin else {}
            for route in routes:
                confirmed = stamp(route.get('confirmed_at'))
                if evidence and confirmed and (published_at := stamp(origin[1])) and published_at <= confirmed:
                    if depth := pullback_depth(route, evidence, origin[2], published_at):
                        route = {**route, 'pullback_depth': depth}
                branch = review_branch(route)
                key = confirmation_key(route)
                if valid_keys is not None and key not in valid_keys:
                    path_outcome = 'signal_invalidated_at_publication'
                elif outcome in ('buy_suggested', 'conditional_buy_suggested') and buy_basis and key not in buy_basis:
                    path_outcome = 'not_buy_basis'
                else:
                    path_outcome = outcome
                price = number(route.get('price_cny'))
                entry_checks = tuple(capacity_error or ('insufficient_input_cash' if entry and grams * entry > cash + 1e-8 else None)
                                     for entry in (price, mark[1] if mark else None))
                paths.append({'branch': branch, 'outcome': path_outcome, 'threshold_cny': route.get('threshold_cny'),
                              **({'trigger_kind': route['trigger_kind']} if route.get('trigger_kind') else {}),
                              'confirmed_at': route.get('confirmed_at'), 'confirmed_buy_cny': price,
                              'seconds_to_start': elapsed_seconds(confirmed, started),
                              'seconds_to_completion': elapsed_seconds(confirmed, finished),
                              'quote_change_to_publication_cny': round(mark[1] - price, 2) if mark and price else None,
                              'quote_followups': [followup_quote(conn, bank, confirmed, price, minutes, now,
                                                                 fee=fee, published=released, publication_quote=mark,
                                                                 grams=grams, entry_checks=entry_checks)
                                                  for minutes in (15, 60)],
                              'trading_day_followups': observed_review_horizons(conn, bank, confirmed, price, fee,
                                                                                 released, mark, now, grams=grams,
                                                                                 entry_checks=entry_checks)})
                if (branch, path_outcome) not in counted:
                    summary[branch][path_outcome] += 1
                    counted.add((branch, path_outcome))
            cases.append({'run_id': run_id, 'attempt_id': attempt, 'event_id': e['id'], 'started_at': start, 'completed_at': finish,
                          'analysis_seconds': elapsed_seconds(started, finished),
                          'advice_id': advice_id, 'advice_action': value.get('action'), 'outcome': outcome,
                          'advice_buy_grams': number(value.get('grams')) if value.get('action') == 'buy' else None,
                          'post_publication_quote_at': mark[0] if mark else None, 'paths': paths})
    archived = sum(1 for row in rows if (at:=stamp(row[-1])) and at<=now)
    return {'status': 'available' if archived else 'input_archives_unavailable',
            'examined_runs': len(rows), 'archived_runs': archived, 'cases': cases, 'by_branch': summary}


def report(conn, bank, model, limit, now, *, grams=GRAMS):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone():
        return {'status': 'sampling_not_initialized', 'cases': []}
    cases = []
    recent = conn.execute('SELECT id,created_at,expires_at,advice_json,view_id FROM jd_live_advice '
                          'WHERE bank=? AND analyst=? AND julianday(created_at)<=julianday(?) '
                          'ORDER BY id DESC LIMIT ?', (bank, model, utc_text(now), limit)).fetchall()
    has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_views'").fetchone()
    for advice_id, created, expires, raw, view_id in recent:
        published, end = stamp(created), stamp(expires)
        if not published or not end or published > now:
            continue
        rows = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                            'WHERE bank=? AND quoted_at>=? AND observed_at>=quoted_at AND observed_at<=? ORDER BY quoted_at',
                            (bank, utc_text(published), utc_text(now))).fetchall()
        points = [(stamp(q), stamp(o), buy, sell) for q, o, buy, sell in rows]
        value = json.loads(raw)
        watch = value.get('buy_watch')
        if watch:
            replacement = conn.execute("SELECT created_at FROM jd_live_advice WHERE bank=? AND analyst=? "
                                       "AND id>? AND COALESCE(json_extract(advice_json,'$.buy_watch.id'),'')!=? "
                                       "AND julianday(created_at)<=julianday(?) ORDER BY id LIMIT 1",
                                       (bank, model, advice_id, watch['id'], utc_text(now))).fetchone()
            if replacement and (replacement_at:=stamp(replacement[0])) and replacement_at<=now:
                watch['expires_at'] = utc_text(min(stamp(watch['expires_at']), replacement_at))
        archived_quotes = {}
        if has_history and any(map(zoned_pullback, value.get('review_conditions', []))):
            archived_quotes = archived_bank_quotes(conn, bank, model, view_id)
            for condition in value['review_conditions']:
                if depth := pullback_depth(condition, archived_quotes, value.get('input_quote_at'), published):
                    condition['pullback_depth'] = depth
        cases.append({'advice_id': advice_id, 'published_at': published.isoformat(),
                      'horizons': evaluate(value, published, end, points, now, grams=grams),
                      'independent_branch_replay': independent_branch_replay(value, published, end, points, now,
                                                                            archived_quotes=archived_quotes, grams=grams)})
    return {'bank': bank, 'model': model, 'as_of': now.isoformat(), 'grams': grams, 'cases': cases,
            'observed_reviews': observed_reviews(conn, bank, model, limit, now, grams=grams)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', choices=('zheshang', 'minsheng'), default='zheshang')
    parser.add_argument('--model', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--limit', type=int, choices=range(1, 101), metavar='1..100', default=12)
    parser.add_argument('--grams', metavar='GRAMS', default=GRAMS, help='Assumed buy size, default: %(default)s g')
    parser.add_argument('--as-of', metavar='ISO8601', help='Historical report cutoff, including timezone')
    options = parser.parse_args()
    grams = number(options.grams)
    if not grams:
        parser.error('--grams must be a positive finite number')
    now = stamp(options.as_of) if options.as_of is not None else datetime.now(timezone.utc)
    if now is None:
        parser.error('--as-of must be an ISO 8601 timestamp with a timezone')
    conn = readonly(Settings().db_path)
    try:
        print(json.dumps(report(conn, options.bank, options.model, options.limit, now, grams=grams),
                         ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
