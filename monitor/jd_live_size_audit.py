"""Offline comparison of total position sizes at the same observed bank entry."""
from collections import defaultdict
from math import floor

from .trade_costs import number

TARGETS = (5, 10, 20, 40, 60, 100)


def position_sizes(advice, account, entry, fee, route):
    statuses = advice.get('input_data_status') or {}
    quote = account.get('quote') or {}
    orders = account.get('pending_orders') or {}
    if (account.get('status', statuses.get('account')) != 'fresh'
            or quote.get('status', advice.get('input_quote_status')) != 'fresh'
            or orders.get('complete', statuses.get('orders_complete')) is not True
            or orders.get('status', statuses.get('orders')) not in ('fresh', None)):
        return {'status': 'input_not_fresh_or_complete'}

    state = advice.get('input_state') or {}
    risk = advice.get('input_account_risk') or account.get('account_risk') or {}
    capacity = account.get('new_buy_capacity_at_input') or {}
    cash = number(account.get('cash_cny', state.get('cash_cny')))
    available = number(capacity.get('cash_after_buy_orders_cny', advice.get('input_participation_cash_cny')))
    held = number(account.get('total_grams', state.get('total_grams')))
    if cash is None or available is None or held is None or fee is None or fee >= 1:
        return {'status': 'cash_holdings_or_fee_unknown'}
    gross = cash + held * entry
    if gross <= 0:
        return {'status': 'empty_account'}

    path = advice.get('position_path') or {}
    zone = (route.get('buy_review') or {}).get('zone') or {}
    exit_price = number(zone.get('risk_exit_cny'))
    if exit_price is None and not path.get('checks'):
        exit_price = number(path.get('boundary_price_cny'))
    sell = number(quote.get('sell_price_cny', advice.get('input_quote_cny')))
    budget = number(risk.get('risk_budget_cny'))
    net = number(risk.get('net_assets_cny'))
    minimum = number(account.get('minimum_buy_grams', advice.get('input_minimum_buy_grams')))
    pending = capacity.get('pending_buy_exposure') or advice.get('input_pending_buy_exposure') or {}
    pending_grams, pending_cost = number(pending.get('grams')), number(pending.get('cost_cny'))
    if pending_grams is None and pending_cost is None and state.get('pending_order_count') == 0:
        pending_grams = pending_cost = 0.
    base_exit_loss = added_exit_loss = None
    if (exit_price and exit_price < entry and sell and budget is not None
            and pending_cost is not None and pending_grams is not None):
        exit_net = exit_price * (1 - fee)
        hold_exit_loss = max(0., held * (sell - exit_price) * (1 - fee))
        pending_exit_loss = max(0., pending_cost - pending_grams * exit_net)
        base_exit_loss = hold_exit_loss + pending_exit_loss
        added_exit_loss = entry - exit_net

    sizes = {}
    starting_position_pct = held * entry / gross * 100
    for target in TARGETS:
        grams = floor(max(0., gross * target / 100 / entry - held) * 100 + 1e-8) / 100
        spending = grams * entry
        if starting_position_pct >= target - 1e-8:
            status = 'at_or_above_target'
        elif minimum is None:
            status = 'minimum_unknown'
        elif grams == 0 or grams < minimum:
            status = 'below_bank_minimum'
        elif spending > min(cash, available) + 1e-8:
            status = 'insufficient_input_cash'
        else:
            status = 'fundable'
        size = {'status': status, 'additional_grams': grams, 'spending_cny': round(spending, 2)}
        if base_exit_loss is not None and grams > 0:
            loss = base_exit_loss + grams * added_exit_loss
            size.update(planned_full_position_loss_cny=round(loss, 2),
                        within_risk_reference=loss <= budget + 1e-8)
        sizes[str(target)] = size
    return {'status': 'available', 'entry_cny_per_gram': entry, 'input_net_assets_cny': net,
            'starting_position_pct': round(starting_position_pct, 4),
            'exit_reference_cny': exit_price, 'targets_pct': sizes}


def position_marks(sizing, mark):
    if mark.get('status') != 'valued':
        return {}
    net = sizing.get('input_net_assets_cny')
    outcomes = {}
    for target, size in sizing['targets_pct'].items():
        if size['status'] != 'fundable':
            continue
        grams = size['additional_grams']
        profit = grams * mark['net_cny_per_gram']
        gross = grams * mark['gross_cny_per_gram']
        adverse = mark['sampled_adverse_excursion_cny_per_gram']
        outcomes[target] = {'additional_gross_cny': round(gross, 2),
                            'additional_net_cny': round(profit, 2),
                            'additional_nav_pct': round(100 * profit / net, 5) if net else None,
                            'sampled_additional_adverse_cny': round(grams * adverse, 2) if adverse is not None else None}
    return outcomes


def summarize_position_sizes(paths):
    grouped = defaultdict(lambda: defaultdict(list))
    for path in paths:
        sizing = path.get('position_size_at_publication') or {}
        if sizing.get('status') != 'available':
            continue
        for rows, unit, field in ((path['quote_followups'], 'm', 'minutes'),
                                  (path['trading_day_followups'], 'd', 'trading_days')):
            for row in rows:
                grouped[path['branch']][f'{row[field]}{unit}'].append(
                    (sizing, row.get('position_size_marks', {}), path.get('outcome')))
    summary = {}
    for branch, horizons in grouped.items():
        summary[branch] = {}
        for horizon, episodes in horizons.items():
            pairs = {}
            for low, high in zip(TARGETS, TARGETS[1:]):
                lower, upper = str(low), str(high)
                deltas, nav_deltas, adverse_deltas = [], [], []
                risk_known = within_risk = gross_up_but_net_unprofitable = 0
                no_buy_pairs = no_buy_then_net_profitable = 0
                for sizing, marks, outcome in episodes:
                    if lower not in marks or upper not in marks:
                        continue
                    small, large = marks[lower], marks[upper]
                    deltas.append(large['additional_net_cny'] - small['additional_net_cny'])
                    if small['additional_nav_pct'] is not None and large['additional_nav_pct'] is not None:
                        nav_deltas.append(large['additional_nav_pct'] - small['additional_nav_pct'])
                    gross_up_but_net_unprofitable += large['additional_gross_cny'] > 0 and large['additional_net_cny'] <= 0
                    if outcome == 'no_buy_suggested':
                        no_buy_pairs += 1
                        no_buy_then_net_profitable += large['additional_net_cny'] > 0
                    if small['sampled_additional_adverse_cny'] is not None and large['sampled_additional_adverse_cny'] is not None:
                        adverse_deltas.append(large['sampled_additional_adverse_cny'] - small['sampled_additional_adverse_cny'])
                    risk_ok = sizing['targets_pct'][upper].get('within_risk_reference')
                    risk_known += risk_ok is not None
                    within_risk += risk_ok is True
                pairs[f'{low}_to_{high}'] = {
                    'paired_samples': len(deltas), 'larger_earned_more': sum(delta > 1e-8 for delta in deltas),
                    'larger_lost_more': sum(delta < -1e-8 for delta in deltas), 'exit_risk_known': risk_known,
                    'gross_up_but_net_unprofitable': gross_up_but_net_unprofitable,
                    'no_buy_paired_samples': no_buy_pairs, 'no_buy_then_net_profitable': no_buy_then_net_profitable,
                    'larger_within_risk_reference': within_risk, 'complete_adverse_samples': len(adverse_deltas),
                    'mean_extra_net_cny': round(sum(deltas) / len(deltas), 2) if deltas else None,
                    'mean_extra_net_nav_pct': round(sum(nav_deltas) / len(nav_deltas), 5) if nav_deltas else None,
                    'mean_extra_adverse_cny': round(sum(adverse_deltas) / len(adverse_deltas), 2) if adverse_deltas else None,
                }
            summary[branch][horizon] = {'size_inputs_available': len(episodes), 'pairs': pairs}
    return summary
