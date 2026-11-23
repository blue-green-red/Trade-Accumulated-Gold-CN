"""Read-only replay of a hypothetical bank-gold purchase, with a configurable size."""
import argparse
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from .config import BJ, Settings
from .jd_live_buy_watch import (BREAKOUT_MODES, MAX_GAP, WATCH_WINDOW, advance, advance_watch, bank_breakout_condition,
                               breakout_retest_price_status, buy_price_windows, eligible, utc_text,
                               plain_buy_discards, plain_buy_suppressions, watch_deadline, watch_routes)
from .jd_live_conditions import archived_bank_quotes, condition_key, price_met, pullback_depth, stamp, zoned_pullback
from .jd_live_buy_guard import ENTRY_EVENT_PREFIX, confirmation_key, plain_buy_observation, stored_price_condition
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


def observable_quotes(quotes):
    """Replay receipt order, dropping a delayed quote superseded by a newer one."""
    latest = None
    for point in sorted(quotes, key=lambda q: (q[1], -q[0].timestamp())):
        if latest is None or point[0] > latest:
            latest = point[0]
            yield point


def market_breakout_episodes(points):
    """Identify breakouts independently of when the model published recommendations."""
    window = deque()
    condition = None
    for point in points:
        at, received, buy, _ = point
        gap = bool(window and (at - window[-1][0] > MAX_GAP or received - window[-1][1] > MAX_GAP))
        if condition and (gap or received >= began + WATCH_WINDOW or buy < condition['price_cny']):
            ending = 'sampling_gap' if gap else 'window_expired' if received >= began + WATCH_WINDOW else 'fell_below_level'
            yield condition['price_cny'], began, confirmations, ending, point if ending == 'fell_below_level' else None
            condition = None
        if gap:
            window.clear()
        if condition is None and window:
            anchor = window[-1][0]
            while window[0][0] <= anchor - WATCH_WINDOW:
                window.popleft()
            if (anchor - window[0][0] >= WATCH_WINDOW - MAX_GAP
                    and buy > max(quote[2] for quote in window)):
                condition = bank_breakout_condition(buy_price_windows([(p[0], p[2]) for p in window], anchor))
                states = {mode: {} for mode in BREAKOUT_MODES}
                confirmations = {}
                began = received
        if condition:
            for mode, prior in states.items():
                if prior.get('stage') == 'confirmed':
                    continue
                step = states[mode] = advance(prior, condition, at, buy, breakout_mode=mode)
                if step.get('stage') == 'confirmed':
                    confirmations[mode] = point
        window.append(point)
        while window[0][0] <= at - WATCH_WINDOW:
            window.popleft()
    if condition:
        yield condition['price_cny'], began, confirmations, 'as_of', None


def market_breakout_horizon(points, times, entry, fee, now, *, minutes=None, days=None, loss_point=None):
    if minutes is not None:
        target = entry[1] + timedelta(minutes=minutes)
        session = trading_session(target)
        if not session['is_open']:
            return unavailable('market_closed')
        deadline = min(target + MAX_GAP, stamp(session['close_at']) - timedelta(microseconds=1))
    else:
        target, deadline = trading_horizon(entry[1], days)
    loss_exit = {}
    if loss_point and entry[1] < loss_point[1] <= target:
        loss_exit['exit_on_breakout_loss'] = {'observed_at': utc_text(loss_point[1]), 'sell_cny': loss_point[3],
                                             'net_cny': round(loss_point[3] * (1 - fee) - entry[2], 2)}
    start = bisect_left(times, entry[0])
    horizon = bisect_left(times, target, start)
    exit_point = next((point for point in points[horizon:bisect_right(times, deadline, horizon)]
                       if point[1] <= min(now, deadline)), None)
    if not exit_point:
        return {**unavailable('pending' if now < deadline else 'horizon_quote_missing'), **loss_exit}
    end = bisect_right(times, exit_point[0], horizon)
    samples = points[start:end]
    outcome = mark(entry, exit_point, fee, samples, 1)
    outcome.update(gross_price_change_cny=round(exit_point[3] - entry[2], 2),
                   sell_fee_cny=round(exit_point[3] * fee, 2), **loss_exit)
    if outcome['sampled_best_net_cny'] is not None:
        best_sell = max(sample[3] for sample in samples)
        outcome['sampled_best_gross_price_change_cny'] = round(best_sell - entry[2], 2)
    return outcome


def market_breakout_summary(observed):
    complete = [row for row in observed if row['sampled_best_net_cny'] is not None]
    losses = [row['exit_on_breakout_loss']['net_cny'] - row['net_cny']
              for row in observed if 'exit_on_breakout_loss' in row]
    return {'valued': len(observed), 'full_path_samples': len(complete),
            'positive_at_horizon': sum(row['net_cny'] > 0 for row in observed),
            'rallied_at_horizon_before_fee': sum(row['gross_price_change_cny'] > 0 for row in observed),
            'rallied_at_horizon_but_not_profit_after_fee': sum(row['gross_price_change_cny'] > 0 and row['net_cny'] <= 0
                                                             for row in observed),
            'sampled_break_even': sum(row['sampled_first_break_even_at'] is not None for row in complete),
            'sampled_rally_below_break_even': sum(row['sampled_best_gross_price_change_cny'] > 0
                                                 and row['sampled_first_break_even_at'] is None for row in complete),
            'exit_on_loss_comparison': {'pairs': len(losses), 'exit_better': sum(edge > 0 for edge in losses),
                                        'hold_better': sum(edge < 0 for edge in losses)}}


def market_breakout_baseline(conn, bank, now, fee):
    """Read-only net outcomes after independent, causally identified bank breakthroughs."""
    since = now - timedelta(days=7)
    rows = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                        'WHERE bank=? AND quoted_at>? AND quoted_at<=? AND observed_at BETWEEN quoted_at AND ? '
                        'ORDER BY quoted_at', (bank, utc_text(since - WATCH_WINDOW), utc_text(now), utc_text(now)))
    tape = ((stamp(quoted), stamp(seen), buy, sell) for quoted, seen, buy, sell in rows)
    points = list(observable_quotes(point for point in tape
                                    if point[1] - point[0] <= MAX_GAP and trading_session(point[0])['is_open']))
    times = [point[0] for point in points]
    horizons = {'15m': {'minutes': 15}, '60m': {'minutes': 60}, '1d': {'days': 1}, '3d': {'days': 3}}
    cases = []
    endings = defaultdict(int)
    unconfirmed_endings = defaultdict(int)
    for threshold, began, confirmations, ending, loss_point in market_breakout_episodes(points):
        if began < since:
            continue
        endings[ending] += 1
        if not confirmations:
            unconfirmed_endings[ending] += 1
        for mode, entry in confirmations.items():
            outcomes = {label: (market_breakout_horizon(points, times, entry, fee, now, loss_point=loss_point, **period)
                                if fee is not None else unavailable('fee_unknown')) for label, period in horizons.items()}
            cases.append({'branch': f'breakout_{mode}', 'threshold_cny': threshold, 'breakout_at': utc_text(began),
                          'confirmed_at': utc_text(entry[1]), 'confirmed_buy_cny': entry[2], 'outcomes_per_gram': outcomes})
    by_branch = {}
    for mode in BREAKOUT_MODES:
        branch = f'breakout_{mode}'
        selected = [row for row in cases if row['branch'] == branch]
        by_branch[branch] = {'confirmations': len(selected), 'horizons': {}}
        for label in horizons:
            observed = [r['outcomes_per_gram'][label] for r in selected if r['outcomes_per_gram'][label]['status'] == 'valued']
            by_branch[branch]['horizons'][label] = market_breakout_summary(observed)
    paired = defaultdict(dict)
    for row in cases:
        paired[row['breakout_at']][row['branch']] = row['confirmed_buy_cny']
    both = [pair for pair in paired.values() if len(pair) == 2]
    differences = [pair['breakout_continuation'] - pair['breakout_retest'] for pair in both]
    return {'from': utc_text(since), 'to': utc_text(now), 'fee_rate': fee, 'cases': cases, 'by_branch': by_branch,
            'episode_endings': endings, 'unconfirmed_episode_endings': unconfirmed_endings,
            'paired_entry_prices': {'episodes': len(both), 'retest_lower': sum(diff > 0 for diff in differences),
                                    'continuation_lower': sum(diff < 0 for diff in differences),
                                    'equal': sum(diff == 0 for diff in differences)},
            'sampled_dates_bj': sorted({p[0].astimezone(BJ).date().isoformat() for p in points if p[0] >= since})}


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

    first = next(observable_quotes(p for p in quotes
                                   if published <= p[0] <= p[1] < expires and p[1] <= min(now, published + MAX_GAP)), None)
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
    actionable = list(observable_quotes(p for p in quotes if published <= p[0] <= p[1] < last_deadline and p[1] <= now))
    states = [{} for _ in routes]
    unfunded = []
    rejected_episodes = defaultdict(set)
    for point in actionable:
        if watch_end and boundary is not None and point[3] <= boundary:
            last_deadline = point[1]
            if progress is not None:
                progress['last_stage'] = 'position_boundary'
            break
        for i, route in enumerate(routes):
            rejected = rejected_episodes.get(i)
            if point[1] >= deadlines[i] or (rejected and (blocked or not zoned_pullback(route))):
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
                episode = states[i].get('touched_at') or states[i]['confirmed_at']
                if rejected and episode in rejected:
                    continue
                branch = review_branch(route)
                error = blocked or ('insufficient_input_cash' if point[2] * grams > cash + 1e-8 else None)
                if not error:
                    if progress is not None and unfunded:
                        progress['earlier_unfunded_confirmations'] = unfunded
                    return immediate, point, branch
                rejected_episodes[i].add(episode)
                unfunded.append({'branch': branch, 'quoted_at': utc_text(point[0]),
                                 'observed_at': utc_text(point[1]), 'buy_cny_per_gram': point[2],
                                 'paper_entry_status': error})
        if blocked and len(rejected_episodes) == len(routes):
            break
    if blocked or len(rejected_episodes) == len(routes):
        status = blocked or 'insufficient_input_cash'
    elif now < last_deadline:
        status = 'pending'
    elif not actionable or not complete_sampling(published, last_deadline, actionable):
        status = 'confirmation_quotes_missing'
    else:
        status = 'insufficient_input_cash' if unfunded else 'no_confirmation'
    confirmation = unavailable(status)
    if unfunded:
        confirmation['observed_price_confirmations'] = unfunded
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
    marks = list(observable_quotes(p for p in quotes if quoted_at <= p[0] <= end_at and observed_at <= p[1] <= exit_quote[1]))
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
        exit_quote = next(observable_quotes(p for p in quotes if target <= p[0] <= p[1] <= min(now, deadline)), None)
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
    if earlier := progress.pop('earlier_unfunded_confirmations', None):
        evidence['earlier_unfunded_confirmations'] = earlier
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


def trim_observation_windows(windows, cutoff):
    while windows and windows[-1][1] >= cutoff:
        windows.pop()
    if windows and windows[-1][2] > cutoff:
        owner, start, _ = windows[-1]
        windows[-1] = owner, start, cutoff


def pullback_observation_windows(history):
    """Split renewed conditions into disjoint windows within each bank watch."""
    result = defaultdict(dict)
    plan_id = None
    active = {}

    def save():
        for key, windows in active.items():
            for owner, start, end in windows:
                result[owner][key] = start, end

    for advice_id, published, expires, value, _ in history:
        watch = value.get('buy_watch') or {}
        if watch.get('id') != plan_id:
            for windows in active.values():
                trim_observation_windows(windows, published)
            save()
            active = {}
            plan_id = watch.get('id')
        if not plan_id:
            continue
        advice = {**value, 'expires_at': utc_text(expires)}
        for condition in value.get('review_conditions', []):
            if not zoned_pullback(condition) or not (end := watch_deadline(advice, condition)):
                continue
            windows = active.setdefault(condition_key(condition), [])
            trim_observation_windows(windows, max(published, end))
            start = max(published, windows[-1][2]) if windows else published
            if start < end:
                windows.append((advice_id, start, end))
    save()
    return result


def independent_branch_replay(value, published, expires, quotes, now, *, archived_quotes=None, grams=GRAMS,
                              pullback_windows=None):
    """Use each condition alone and preserve the original observation window."""
    watch = value.get('buy_watch')
    reused = watch and stamp(watch.get('starts_at')) != published
    minimum = number(value.get('input_minimum_buy_grams'))
    cases = []
    for condition in value.get('review_conditions', []):
        if not eligible(condition, minimum):
            continue
        start, replay_value = published, value
        if watch and pullback_windows is not None and zoned_pullback(condition):
            window = pullback_windows.get(condition_key(condition))
            if not window:
                continue
            start, end = window
            replay_value = {**value, 'buy_watch': {**watch, 'expires_at': utc_text(end)}}
        elif reused:
            continue
        if archived_quotes and 'pullback_depth' not in condition and (depth := pullback_depth(
                condition, archived_quotes, value.get('input_quote_at'), published)):
            condition = {**condition, 'pullback_depth': depth}
        modes = ('continuation', 'retest') if condition.get('bank_breakout') else (None,)
        for mode in modes:
            result = replay_one_branch(replay_value, condition, mode, start, expires, quotes, now, grams)
            if replay_value is not value:
                result.update(observation_starts_at=utc_text(start), observation_ends_at=utc_text(end))
            if zoned_pullback(condition) and archived_quotes is not None:
                result['sampled_low_sensitivity'] = shallow_level_sensitivity(replay_value, condition, archived_quotes,
                                                                             start, expires, quotes, now, grams)
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
        'ORDER BY observed_at,quoted_at DESC LIMIT 1', (bank, utc_text(start), utc_text(cutoff), utc_text(cutoff))).fetchone()


def archived_quote_samples(conn, bank, model, now):
    """Use only time-verifiable quotes from exact archived model inputs."""
    path = f'$.live_readonly_accounts.accounts.{bank}'
    oldest = utc_text(now - timedelta(days=40))
    rows = conn.execute('''
        SELECT json_extract(value,?),json_extract(value,?),updated_at
        FROM state WHERE key LIKE ? AND updated_at BETWEEN ? AND ?
          AND json_extract(value,?)='fresh' AND json_extract(value,?)='available'
        ''', (f'{path}.quote', f'{path}.component_times.quote', f'ai_model_input:%:{model}',
              oldest, utc_text(now), f'{path}.status', f'{path}.coverage.quote'))
    samples, conflicts = {}, set()
    for raw, received, archived in rows:
        quote = json.loads(raw) if raw else {}
        if quote.get('status') != 'fresh':
            continue
        quoted, observed, saved = map(stamp, (quote.get('quoted_at'), received, archived))
        buy, sell = number(quote.get('buy_price_cny')), number(quote.get('sell_price_cny'))
        if (not all((quoted, observed, saved, buy, sell)) or not quoted <= observed <= min(saved, quoted + MAX_GAP)
                or not trading_session(quoted)['is_open']):
            continue
        label = utc_text(quoted)
        previous = samples.get(label)
        if previous and previous[1:3] != (buy, sell):
            conflicts.add(label)
        elif not previous or utc_text(observed) < previous[3]:
            samples[label] = (label, buy, sell, utc_text(observed), 'analysis_archive')
    conflicts.update(row[0] for row in conn.execute(
        'SELECT quoted_at FROM jd_live_quote_samples WHERE bank=? AND quoted_at>=? AND observed_at<=?',
        (bank, oldest, utc_text(now))))
    return {key: samples[key] for key in sorted(samples.keys() - conflicts)}


def first_review_quote(conn, bank, start, cutoff, archive):
    sample = first_observed_quote(conn, bank, start, cutoff)
    if sample is not None or not archive or cutoff < start:
        return sample
    start, cutoff = utc_text(start), utc_text(cutoff)
    return min((row for key, row in archive.items() if start <= key and row[3] <= cutoff),
               key=lambda row: (row[3], -stamp(row[0]).timestamp()), default=None)


def quote_source(sample):
    return (sample[4] if len(sample) > 4 else 'bank_quote_tape') if sample else None


def confirmation_sample(conn, bank, quoted, price, archived_at, archive):
    if quoted is None or price is None:
        return None
    timestamp = utc_text(quoted)
    row = conn.execute('SELECT quoted_at,buy_price_cny,sell_price_cny,observed_at '
                       'FROM jd_live_quote_samples WHERE bank=? AND quoted_at=? AND observed_at<=?',
                       (bank, timestamp, utc_text(archived_at))).fetchone() or archive.get(timestamp)
    return row if row and row[1] == price and timestamp <= row[3] <= utc_text(archived_at) else None


def observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target, exit_quote,
                         grams, entry_checks, confirmed_quote=None):
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
    entry = quotes[0] if quotes[0][0] == confirmed and quotes[0][2] == price else None
    if quote_source(confirmed_quote) == 'analysis_archive':
        entry = (confirmed, stamp(confirmed_quote[3]), confirmed_quote[1], confirmed_quote[2])
        quotes.insert(0, entry)
    if entry is None:
        entry = unavailable('confirmation_quote_missing')
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
        quoted, buy, sell, seen = publication_quote[:4]
        later = stamp(quoted), stamp(seen), buy, sell
        if quote_source(publication_quote) == 'analysis_archive':
            quotes.append(later)
            quotes.sort(key=lambda quote: quote[0])
    return {confirmation_key: mark(entry, exit_point, fee, quotes, grams),
            publication_key: mark(later, exit_point, fee, quotes, grams)}


def followup_quote(conn, bank, confirmed, price, minutes, now, *, fee=None, published=None, publication_quote=None,
                   grams=GRAMS, entry_checks=(None, None), confirmed_quote=None, archive=None):
    result = {'minutes': minutes}
    if not confirmed or price is None:
        return {**result, 'status': 'signal_unavailable'}
    target = (stamp(confirmed_quote[3]) if confirmed_quote else confirmed) + timedelta(minutes=minutes)
    session = trading_session(target)
    if not session['is_open']:
        return {**result, 'status': 'market_closed'}
    deadline = min(target + MAX_GAP, stamp(session['close_at']) - timedelta(microseconds=1))
    quote = first_review_quote(conn, bank, target, min(deadline, now), archive)
    if not quote:
        return {**result, 'status': 'pending' if now < deadline else 'quote_missing'}
    result.update(status='observed', quoted_at=quote[0], exit_quote_source=quote_source(quote),
                  change_cny_per_gram=round(quote[1] - price, 2))
    if fee is not None and fee < 1:
        exit_net = quote[2] * (1 - fee)
        after_analysis = (round(grams * (exit_net - publication_quote[1]), 2)
                          if not entry_checks[1] and publication_quote and stamp(publication_quote[3]) < target else None)
        result[f'hypothetical_{grams:g}g_net_cny'] = {
            'at_confirmation': round(grams * (exit_net - price), 2) if not entry_checks[0] else None,
            'after_analysis': after_analysis, 'wait': 0.0}
    result.update(observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target, quote,
                                       grams, entry_checks, confirmed_quote))
    return result


def observed_review_horizons(conn, bank, confirmed, price, fee, published, publication_quote, now,
                             *, grams=GRAMS, entry_checks=(None, None), confirmed_quote=None, archive=None):
    """Mark fixed-size hypothetical additions at common trading-day horizons."""
    horizons = []
    entry_keys = (f'at_confirmation_{grams:g}g', f'after_analysis_{grams:g}g')
    for days in (1, 3):
        row = {'trading_days': days, 'wait': {'status': 'valued', 'net_cny': 0.0}}
        if not confirmed or not price:
            row.update({key: unavailable('signal_unavailable') for key in entry_keys})
            horizons.append(row)
            continue
        target, deadline = trading_horizon(stamp(confirmed_quote[3]) if confirmed_quote else confirmed, days)
        row['target_at'] = utc_text(target)
        cutoff = min(now, deadline)
        exit_quote = first_review_quote(conn, bank, target, cutoff, archive)
        if not exit_quote:
            status = 'pending' if now < deadline else 'horizon_quote_missing'
            row.update({key: unavailable(status) for key in entry_keys})
            horizons.append(row)
            continue
        row['exit_quote_at'] = exit_quote[0]
        row['exit_quote_source'] = quote_source(exit_quote)
        row.update(observed_entry_marks(conn, bank, confirmed, price, fee, published, publication_quote, target,
                                        exit_quote, grams, entry_checks, confirmed_quote))
        horizons.append(row)
    return horizons


def compare_review_timings(episodes, grams):
    confirmation_key = f'at_confirmation_{grams:g}g'
    publication_key = f'after_analysis_{grams:g}g'
    by_branch = defaultdict(lambda: defaultdict(list))
    for path in episodes:
        horizons = [(f"{row['minutes']}m", row) for row in path['quote_followups']]
        horizons.extend((f"{row['trading_days']}d", row) for row in path['trading_day_followups'])
        for label, row in horizons:
            early, late = row.get(confirmation_key, {}), row.get(publication_key, {})
            pair = (early['net_cny'], late['net_cny']) if early.get('status') == late.get('status') == 'valued' else None
            by_branch[path['branch']][label].append(pair)

    result = {}
    for branch, horizons in by_branch.items():
        result[branch] = {}
        for label, pairs in horizons.items():
            matched = [pair for pair in pairs if pair is not None]
            count = len(matched)
            early_better = sum(early > late for early, late in matched)
            late_better = sum(late > early for early, late in matched)
            result[branch][label] = {
                'reviewed_episodes': len(pairs), 'paired_samples': count,
                'confirmation_better': early_better, 'after_analysis_better': late_better,
                'same': count - early_better - late_better,
                'confirmation_above_wait': sum(early > 0 for early, _ in matched),
                'after_analysis_above_wait': sum(late > 0 for _, late in matched),
                'mean_confirmation_edge_cny': round(sum(early - late for early, late in matched) / count, 2) if count else None,
            }
    return result


def bank_quotes_during_review(conn, bank, account, archived_at, released, publication_quote):
    quote = account.get('quote') or {}
    input_at = stamp(quote.get('quoted_at'))
    buy = number(quote.get('buy_price_cny'))
    if (account.get('status') != 'fresh' or quote.get('status') != 'fresh' or not buy
            or not input_at or input_at > archived_at):
        return {}
    data = {'input_quote_at': utc_text(input_at), 'input_buy_cny': buy}
    if released and archived_at < released:
        rows = conn.execute('SELECT quoted_at,observed_at,buy_price_cny FROM jd_live_quote_samples '
                            'WHERE bank=? AND quoted_at>? AND quoted_at<=? AND observed_at>=quoted_at '
                            'AND observed_at>? AND observed_at<=?',
                            (bank, utc_text(input_at), utc_text(released), utc_text(archived_at), utc_text(released)))
        points = ((stamp(quoted), stamp(seen), price) for quoted, seen, price in rows)
        prices = [price for _, _, price in observable_quotes(points)]
        if prices:
            data.update(samples_during_analysis=len(prices), lowest_buy_during_analysis_cny=min(prices),
                        highest_buy_during_analysis_cny=max(prices))
    if publication_quote:
        data['first_buy_after_publication_cny'] = publication_quote[1]
        data['buy_change_from_input_cny_per_gram'] = round(publication_quote[1] - buy, 2)
    return data


def retest_price_evidence(route, confirmed, quote_path):
    input_at = stamp(quote_path.get('input_quote_at'))
    if not confirmed or not input_at or confirmed > input_at:
        return {}
    if not (at_input := breakout_retest_price_status(route, quote_path.get('input_buy_cny'))):
        return {}
    data = {'retest_price_status_at_input': at_input}
    if sampled := breakout_retest_price_status(route, quote_path.get('highest_buy_during_analysis_cny')):
        data['sampled_above_retest_zone_during_analysis'] = sampled == 'above_retest_zone'
    if after := breakout_retest_price_status(route, quote_path.get('first_buy_after_publication_cny')):
        data['retest_price_status_after_publication'] = after
    return data


def observed_reviews(conn, bank, model, limit, now, *, grams=GRAMS, bank_inputs=None):
    """Actual review events and outcomes, with a separately labeled quote comparison."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('ai_runs','ai_views')")}
    if tables != {'ai_runs', 'ai_views'}:
        return {'status': 'history_unavailable', 'cases': [], 'by_branch': {}, 'timing_comparison': {}}
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
    episodes = {}
    archive = archived_quote_samples(conn, bank, model, now)
    for attempt, run_id, status, start, finish, covered, advice_id, published, raw, events, sent, valid, source, archived_at in rows:
        if not (archived_at:=stamp(archived_at)) or archived_at>now:
            continue
        finished=stamp(finish)
        if finished and finished>now:
            status='running'
            finish,finished,covered,advice_id,published,raw,valid=(None,)*7
        # Current Codex archives are scoped to the submitted events and omit the duplicate coverage list.
        sent_ids = set(json.loads(sent).get(model, [])) if sent is not None else None
        value = json.loads(raw) if raw else {}
        account = json.loads(source) if source else {}
        if bank_inputs is not None and status == 'completed' and advice_id and account.get('status') == 'fresh':
            quote = account.get('quote') or {}
            if (quote.get('status') == 'fresh' and (at := stamp(quote.get('quoted_at'))) and at <= archived_at
                    and (buy := number(quote.get('buy_price_cny')))):
                bank_inputs.append((attempt, archived_at, at, buy))
        if not events:
            continue
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
        mark = first_review_quote(conn, bank, released, min(now, released + MAX_GAP), archive) if released else None
        quote_path = bank_quotes_during_review(conn, bank, account, archived_at, released, mark)
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
                confirmed_quote = confirmation_sample(conn, bank, confirmed, price, archived_at, archive)
                observed_at = stamp(confirmed_quote[3]) if confirmed_quote else None
                confirmation_error, publication_error = (
                    capacity_error or ('insufficient_input_cash' if entry and grams * entry > cash + 1e-8 else None)
                    for entry in (price, mark[1] if mark else None))
                if observed_at is None:
                    confirmation_error = 'confirmation_quote_missing'
                entry_checks = confirmation_error, publication_error
                path = {
                    'branch': branch, 'outcome': path_outcome, 'threshold_cny': route.get('threshold_cny'),
                    **({'trigger_kind': route['trigger_kind']} if route.get('trigger_kind') else {}),
                    **({'condition_key': key[0]} if key[0] else {}),
                    **retest_price_evidence(route, confirmed, quote_path),
                    'confirmed_at': route.get('confirmed_at'), 'confirmed_buy_cny': price,
                    'confirmation_observed_at': utc_text(observed_at) if observed_at else None,
                    'confirmation_quote_source': quote_source(confirmed_quote),
                    'quote_delivery_seconds': elapsed_seconds(confirmed, observed_at),
                    'seconds_to_start': elapsed_seconds(observed_at, started),
                    'seconds_to_completion': elapsed_seconds(observed_at, finished),
                    'quote_change_to_publication_cny': round(mark[1] - price, 2) if mark and price else None,
                    'quote_followups': [followup_quote(conn, bank, confirmed, price, minutes, now, fee=fee,
                                                       published=released, publication_quote=mark, grams=grams,
                                                       entry_checks=entry_checks, confirmed_quote=confirmed_quote,
                                                       archive=archive)
                                        for minutes in (15, 60)],
                    'trading_day_followups': observed_review_horizons(conn, bank, confirmed, price, fee,
                                                                     released, mark, now, grams=grams,
                                                                     entry_checks=entry_checks,
                                                                     confirmed_quote=confirmed_quote, archive=archive),
                }
                paths.append(path)
                if released and confirmed and path_outcome in (
                        'buy_suggested', 'conditional_buy_suggested', 'no_buy_suggested', 'not_buy_basis'):
                    episode = (branch, key[0] or e['id'], confirmed)
                    publication = released, attempt
                    if episode not in episodes or publication < episodes[episode][0]:
                        episodes[episode] = (publication, path)
                if (branch, path_outcome) not in counted:
                    summary[branch][path_outcome] += 1
                    counted.add((branch, path_outcome))
            cases.append({'run_id': run_id, 'attempt_id': attempt, 'event_id': e['id'], 'started_at': start, 'completed_at': finish,
                          'origin_advice_id': e['data'].get('advice_id'),
                          'event_queued_at': e.get('observed_at'),
                          'analysis_seconds': elapsed_seconds(started, finished),
                          'advice_id': advice_id, 'advice_action': value.get('action'), 'outcome': outcome,
                          'advice_buy_grams': number(value.get('grams')) if value.get('action') == 'buy' else None,
                          'post_publication_quote_at': mark[0] if mark else None,
                          'post_publication_quote_source': quote_source(mark),
                          **({'bank_quote_path':quote_path} if quote_path else {}), 'paths': paths})
    archived = sum(1 for row in rows if (at:=stamp(row[-1])) and at<=now)
    return {'status': 'available' if archived else 'input_archives_unavailable',
            'examined_runs': len(rows), 'archived_runs': archived, 'cases': cases, 'by_branch': summary,
            'timing_comparison': compare_review_timings((path for _, path in episodes.values()), grams)}


def plain_buy_review_index(cases):
    result = defaultdict(list)
    for case in cases:
        for path in case['paths']:
            if path.get('trigger_kind') == 'price_touch' and path['branch'] != 'renewed_pullback' and path.get('condition_key'):
                result[(case['origin_advice_id'], path['condition_key'])].append({
                    'attempt_id': case['attempt_id'], 'observed_touch_at': path['confirmed_at'],
                    'event_queued_at': case.get('event_queued_at'), 'outcome': path['outcome']})
    return result


def plain_buy_queue_markers(conn, bank, model, now):
    """Recover exact queue markers still present in the price-monitor state."""
    markers = {}
    for key, raw, updated in conn.execute('SELECT key,value,updated_at FROM state WHERE key LIKE ?',
                                          (f'live-price-watch:{model}:%:{bank}:%',)):
        if not (saved := stamp(updated)) or saved > now:
            continue
        value = json.loads(raw)
        advice_id = value.get('advice_id')
        fingerprint = key.rsplit(':', 1)[-1]
        event = value.get('event_id') or ''
        base = f'live-price:{model}:{bank}:{advice_id}:{fingerprint}'
        if not advice_id or not (queued := stamp(value.get('triggered_at'))) or queued > saved:
            continue
        if event != base and not event.startswith(base+':'):
            continue
        idx = advice_id, fingerprint
        if idx not in markers or queued > markers[idx][0]:
            markers[idx] = queued, event
    return markers


def latched_at_model_input(condition, value, points, published):
    input_at = stamp(value.get('input_quote_at'))
    buy = number(value.get('input_buy_quote_cny'))
    if value.get('input_quote_status') != 'fresh' or not input_at or not buy or not price_met(condition, buy):
        return False
    left_side = False
    for quoted, _, buy, _ in points:
        if quoted <= input_at:
            continue
        if not price_met(condition, buy):
            left_side = True
        elif left_side and quoted >= published:
            return False
    return True


def plain_buy_quote_observations(advice_id, value, published, expires, quotes, now, reviews, bank_inputs,
                                *, markers=None, discards=None, suppressions=None):
    """Show whether each ordinary price level was reached on available quotes."""
    conditions = [item for item in value.get('review_conditions', []) if plain_buy_observation(item)]
    if not conditions or expires <= published:
        return []
    input_at = stamp(value.get('input_quote_at')) if value.get('input_quote_status') == 'fresh' else None
    earliest = min(input_at, published) if input_at else published
    tape = list(observable_quotes(p for p in quotes if earliest <= p[0] <= p[1] < expires and p[1] <= now))
    points = [p for p in tape if p[0] >= published]
    complete = now >= expires and bool(points) and complete_sampling(published, expires, points)
    available = []
    for condition in conditions:
        threshold = condition['price_cny']
        direction = condition['condition']
        row = {'condition': direction, 'threshold_cny': threshold, 'expires_at': utc_text(expires),
               'quote_samples': len(points)}
        gap = None
        if points:
            closest = (min if direction == 'at_or_below' else max)(p[2] for p in points)
            gap = threshold - closest if direction == 'at_or_above' else closest - threshold
            row.update(extreme_buy_cny=closest, distance_to_threshold_cny_per_gram=round(max(0., gap), 2))
        first = None
        if gap is not None and gap <= 0:
            status = 'threshold_sampled'
            first = next(p for p in points if price_met(condition, p[2]))
            row['first_threshold_sample_at'] = utc_text(first[0])
        elif now < expires:
            status = 'pending'
        else:
            status = 'threshold_not_sampled' if complete else 'quote_coverage_incomplete'
        key = advice_id, condition_key(condition)
        attempts = [attempt for attempt in reviews.get(key, [])
                    if (queued := stamp(attempt.get('event_queued_at')) or stamp(attempt['observed_touch_at']))
                    and published <= queued < expires and queued <= now]
        marker = (markers or {}).get(key)
        rejected = [event for event in (discards or {}).get(key, [])
                    if (queued := stamp(event['queued_at'])) and published <= queued < expires]
        if rejected:
            row['discarded_events'] = rejected
        suppressed = [event for event in (suppressions or {}).get(key, [])
                      if (evaluated := stamp(event['evaluated_at'])) and published <= evaluated < expires]
        if suppressed:
            row['suppressed_touches'] = suppressed
        if attempts:
            row.update(review_archive_status='review_archived', review_attempts=attempts)
        elif rejected:
            row['review_archive_status'] = 'queue_discarded'
        elif marker and published <= marker[0] < expires and marker[0] <= now:
            row.update(review_archive_status='queued_without_archived_review',
                       retained_queue_marker={'event_id': marker[1], 'queued_at': utc_text(marker[0])})
        elif first:
            other_reviews = [attempt for attempt, archived, quoted, buy in bank_inputs
                             if first[0] <= quoted <= archived and first[1] <= archived < expires and price_met(condition, buy)]
            if other_reviews:
                row.update(review_archive_status='price_seen_in_other_analysis', other_review_attempt_ids=other_reviews)
            elif suppressed:
                row['review_archive_status'] = 'touch_suppressed'
            else:
                row['review_archive_status'] = ('threshold_already_met_at_input' if latched_at_model_input(
                    condition, value, tape, published) else 'no_matching_archived_review')
        elif suppressed:
            row['review_archive_status'] = 'touch_suppressed'
        available.append({**row, 'status': status})
    return available


def report(conn, bank, model, limit, now, *, grams=GRAMS):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_quote_samples'").fetchone():
        return {'status': 'sampling_not_initialized', 'cases': []}
    cases = []
    recent = conn.execute('SELECT id,created_at,expires_at,advice_json,view_id FROM jd_live_advice '
                          'WHERE bank=? AND analyst=? AND julianday(created_at)<=julianday(?) '
                          'ORDER BY id DESC LIMIT ?', (bank, model, utc_text(now), limit + 1)).fetchall()
    history = [(advice_id, published, end, json.loads(raw), view_id) for advice_id, created, expires, raw, view_id in recent
               if (published := stamp(created)) and (end := stamp(expires)) and published <= now]
    windows = pullback_observation_windows(reversed(history))
    next_publications = {older[0]: newer[1] for newer, older in zip(history, history[1:])}
    bank_inputs = []
    observed = observed_reviews(conn, bank, model, limit, now, grams=grams, bank_inputs=bank_inputs)
    plain_reviews = plain_buy_review_index(observed['cases'])
    queue_markers = plain_buy_queue_markers(conn, bank, model, now)
    plain_discards = defaultdict(list)
    for event in plain_buy_discards(conn, model):
        if event['bank'] == bank and (discarded_at := stamp(event['discarded_at'])) and discarded_at <= now:
            plain_discards[(event['advice_id'], event['condition_key'])].append(event)
    plain_suppressions = defaultdict(list)
    for event in plain_buy_suppressions(conn, model):
        if event['bank'] == bank and (at := stamp(event['evaluated_at'])) and at <= now:
            plain_suppressions[(event['advice_id'], event['condition_key'])].append(event)
    has_history = conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_views'").fetchone()
    for advice_id, published, end, value, view_id in history[:limit]:
        input_at = stamp(value.get('input_quote_at')) if value.get('input_quote_status') == 'fresh' else None
        earliest = min(input_at, published) if input_at else published
        rows = conn.execute('SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny FROM jd_live_quote_samples '
                            'WHERE bank=? AND quoted_at>=? AND observed_at>=quoted_at AND observed_at<=? ORDER BY quoted_at',
                            (bank, utc_text(earliest), utc_text(now))).fetchall()
        points = [(stamp(q), stamp(o), buy, sell) for q, o, buy, sell in rows]
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
                      'plain_buy_quote_observations': plain_buy_quote_observations(
                          advice_id, value, published, min(end, next_publications.get(advice_id, end)), points, now,
                          plain_reviews, bank_inputs, markers=queue_markers, discards=plain_discards,
                          suppressions=plain_suppressions),
                      'independent_branch_replay': independent_branch_replay(value, published, end, points, now,
                                                                            archived_quotes=archived_quotes, grams=grams,
                                                                            pullback_windows=windows.get(advice_id, {}))})
    fee_reference = conn.execute("""SELECT id,json_extract(advice_json,'$.input_sell_fee_rate') AS fee
        FROM jd_live_advice WHERE bank=? AND analyst=? AND julianday(created_at)<=julianday(?)
        AND fee>=0 AND fee<1 ORDER BY id DESC LIMIT 1""", (bank, model, utc_text(now))).fetchone() or (None, None)
    market_breakouts = market_breakout_baseline(conn, bank, now, fee_reference[1])
    market_breakouts['fee_reference_advice_id'] = fee_reference[0]
    return {'bank': bank, 'model': model, 'as_of': now.isoformat(), 'grams': grams, 'cases': cases,
            'observed_reviews': observed, 'market_breakout_baseline': market_breakouts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank', choices=('zheshang', 'minsheng'), default='zheshang')
    parser.add_argument('--model', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--limit', type=int, choices=range(1, 501), metavar='1..500', default=12)
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
