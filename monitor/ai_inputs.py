"""Compact redundant research text while preserving account values and trigger facts."""
from copy import deepcopy
import hashlib
import json


def news_key(row):
    return hashlib.sha256(json.dumps({k:row.get(k) for k in ('time_bj','text')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]


def fill_cause(reason):
    """A fill is a ledger fact, not another copy of an old model's price thesis."""
    if reason in ('持仓止损','持仓止盈') or reason.startswith('实验初始化'):
        return reason
    return 'AI委托成交'


def compare_previous_pullbacks(account):
    from .jd_live_conditions import stamp
    from .trade_costs import number

    evidence = account.get('recent_bank_quotes') or {}
    quote = account.get('quote') or {}
    sample_at = stamp(evidence.get('as_of'))
    lows = [low for window in evidence.get('buy_price_windows_15m', []) if (low := number(window.get('low')))]
    usable = (account.get('status') == 'fresh' and quote.get('status') == 'fresh'
              and evidence.get('status') == 'available' and sample_at is not None
              and sample_at == stamp(quote.get('quoted_at')) and bool(lows))
    sampled_low = min(lows) if usable else None
    field = 'trigger_below_sampled_1h_low_cny_per_gram'
    for advice in account.get('previous_advice', []):
        for condition in advice.get('review_conditions', []):
            condition.pop(field, None)
            if (sampled_low is None or condition.get('condition') != 'at_or_below' or condition.get('quote_side') != 'buy'
                    or not (condition.get('buy_review') or {}).get('zone')):
                continue
            trigger = number(condition.get('price_cny'))
            if trigger and (gap := round(sampled_low - trigger, 2)) > 0:
                condition[field] = gap


def prepare_bank_entry_context(out):
    from .jd_live_buy_guard import ENTRY_EVENT_PREFIX
    from .jd_live_conditions import stamp
    from .trade_costs import bank_review_payoff, cost_reference, number

    accounts = (out.get('live_readonly_accounts') or {}).get('accounts', {})
    for account in accounts.values():
        compare_previous_pullbacks(account)
        account.pop('entry_review_payoffs_at_input', None)
        if account.get('status') != 'fresh' or (account.get('quote') or {}).get('status') != 'fresh':
            account.pop('new_buy_capacity_at_input', None)
    for event in out.get('trigger_events', []):
        if not event.get('id', '').startswith(ENTRY_EVENT_PREFIX):
            continue
        data = event.get('data') or {}
        account = accounts.get(data.get('bank')) or {}
        quote = account.get('quote') or {}
        buy = number(quote.get('buy_price_cny'))
        fee = account.get('sell_fee_rate')
        quoted_at = stamp(quote.get('quoted_at'))
        fresh = (account.get('status') == 'fresh' and quote.get('status') == 'fresh'
                 and buy is not None and buy > 0 and quoted_at is not None)
        for route in data.get('routes', []):
            for key in ('position_boundary_cny', 'signal_expires_at', 'source_advice_id'):
                route.pop(key, None)
            confirmed = stamp(route.get('confirmed_at'))
            condition = route.get('condition_key')
            if not fresh or not condition or not confirmed or quoted_at < confirmed:
                continue
            if route.get('bank_breakout'):
                breakout = number(route.get('threshold_cny'))
                if breakout is None or buy < breakout:
                    continue
                retest = cost_reference(breakout, fee)
                if retest is None:
                    continue
                payoff = {'at_breakout_level_net_cny_per_gram': round(retest['net_sell_price_cny'] - buy, 2)}
            else:
                review = route.get('buy_review') or {}
                zone = review.get('zone')
                recovery, cap = number(review.get('recovery_price_cny')), number(review.get('max_buy_price_cny'))
                if not zone or recovery is None or cap is None or not recovery <= buy <= cap:
                    continue
                payoff = bank_review_payoff(buy, review.get('grams'), zone, fee,
                                           current_sell=quote.get('sell_price_cny'), holding=account.get('total_grams'))
            if payoff is not None:
                account.setdefault('entry_review_payoffs_at_input', {})[condition] = payoff


def market_context(context):
    """Select decision evidence without changing the monitoring snapshot or account ledger."""
    out=deepcopy(context)
    prepare_bank_entry_context(out)
    if out.get('trading_accounts') or out.get('live_readonly_accounts'):
        signals=out.get('signals_tradingview')
        if isinstance(signals,dict):
            daily_weekly={'日线','周线','日','周','1d','1D','d','D','1w','1W','w','W'}
            out['signals_tradingview']={k:v for k,v in signals.items() if k in daily_weekly}
        out.pop('levels_by_code',None)
    jd=out.get('jd_research')
    if isinstance(jd,dict):
        jd.pop('quota',None)
        for kind in ('funds','orders'):
            entry=jd.get(kind)
            if isinstance(entry,dict):
                for key in ('attempted_at','refresh_status','retry_at','error_type','errors','error'):
                    entry.pop(key,None)
    return out


def compact_live_history(account):
    """Use references for duplicate snapshots while keeping differing historical facts."""
    risk = account.get('account_risk')
    trade = account.get('recent_trade')
    for advice in account.get('previous_advice', []):
        sizing = advice.get('sizing_context') or {}
        last_trade = advice.get('last_trade_advice') or {}
        for key in ('input_state', 'input_account_risk'):
            original = sizing.get(key)
            if (isinstance(original, dict) and original and 'same_as' not in original and last_trade.get(key) == original
                    and (key != 'input_account_risk' or original != risk)):
                last_trade[key] = {'same_as': f'本条.sizing_context.{key}'}
        for snapshot in (sizing, last_trade):
            if risk and snapshot.get('input_account_risk') == risk:
                snapshot['input_account_risk'] = {'same_as': '本账户.account_risk'}
            state = snapshot.get('input_state') or {}
            if trade and state.get('recent_trade') == trade:
                state['recent_trade'] = {'same_as': '本账户.recent_trade'}


def compact_context(context):
    out=market_context(context)
    covered_news={news_key(e['data']) for e in out.get('trigger_events',[]) if isinstance(e.get('data'),dict) and e['data'].get('text') and e['data'].get('time_bj')}
    seen=set();news=[]
    for row in out.get('jin10_flash_recent',[]):
        key=news_key(row)
        if key in seen or key in covered_news: continue
        seen.add(key);news.append(row)
    if 'jin10_flash_recent' in out: out['jin10_flash_recent']=news
    jd=out.get('jd_research')
    if isinstance(jd,dict): jd.pop('quota',None)  # operational counters are not market evidence
    macro=out.get('macro')
    if isinstance(macro,dict): macro.pop('errors',None)
    for account in (out.get('trading_accounts') or {}).get('accounts',{}).values():
        if 'recent_fills' in account:
            account['recent_fills']=account['recent_fills'][:3]
            for fill in account['recent_fills']:
                if fill.get('reason'): fill['reason']=fill_cause(fill['reason'])
    for account in (out.get('live_readonly_accounts') or {}).get('accounts', {}).values():
        compact_live_history(account)
    return out
