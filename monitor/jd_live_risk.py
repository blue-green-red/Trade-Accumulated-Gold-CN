"""Deterministic bank-account valuations and hypothetical effects; never places orders."""
from math import floor

from .trade_costs import number


def quote_price(source):
    quote=source.get('quote') or {}
    price=number(quote.get('sell_price_cny'))
    return price if source.get('status')=='fresh' and quote.get('status')=='fresh' and price and price>0 else None


def protection_state(source):
    """Observed combination orders only; a suggested order is never proof of protection."""
    orders=source.get('pending_orders') or {}
    fresh=source.get('status')=='fresh' and orders.get('status')=='fresh'
    complete=fresh and bool(orders.get('complete'))
    count=sum(o.get('kind')=='take_profit_stop_loss' and o.get('status','pending') in ('pending','partially_filled')
              and bool(number(o.get('loss_price_cny'))) for o in orders.get('records',[]))
    return {'status':('present' if count else 'none' if complete else 'unknown') if fresh else 'unknown',
            'count':count if fresh else None,'complete':complete,
            'observed_at':(source.get('component_times') or {}).get('pending_orders')}


def account_risk(source,profile):
    cash=number(source.get('cash_cny'));grams=number(source.get('total_grams'))
    price=quote_price(source);fee=number(source.get('sell_fee_rate'))
    result={k:None for k in ('gold_value_cny','gross_assets_cny','net_assets_cny','risk_budget_cny',
                            'gold_down_1pct_net_loss_cny','gold_down_1pct_nav_loss_pct')}
    position=None
    if source.get('status')!='fresh' or grams is None:return position,result
    gold=grams*price if price is not None else 0. if grams==0 else None
    if gold is not None:result['gold_value_cny']=round(gold,2)
    if cash is None:return position,result
    if cash==0 and grams>0:position=100.
    if gold is None:return position,result
    gross=cash+gold
    if gross>0:position=round(gold/gross*100,4)
    result['gross_assets_cny']=round(gross,2)
    if grams>0 and (fee is None or fee>=1):return position,result
    net_gold=gold*(1-fee) if grams else 0.
    net=cash+net_gold
    result['net_assets_cny']=round(net,2)
    risk=number(profile.get('trade_risk_pct'))
    if risk is not None and risk<=100:result['risk_budget_cny']=round(net*risk/100,2)
    result['gold_down_1pct_net_loss_cny']=round(net_gold*.01,2)
    if net>0:result['gold_down_1pct_nav_loss_pct']=round(net_gold*.01/net*100,4)
    return position,result


def effect(source,price,action=None,grams=0):
    """Same net-liquidation basis at both prices; no second subtraction of sale fees."""
    current=quote_price(source);price=number(price);cash=number(source.get('cash_cny'))
    holding=number(source.get('total_grams'));fee=number(source.get('sell_fee_rate'));grams=number(grams)
    if current is None or price is None or price<=0 or cash is None or holding is None or fee is None or fee>=1 or grams is None:return None
    net_now=cash+holding*current*(1-fee)
    if net_now<=0:return None
    changed=cash+holding*price*(1-fee)-net_now
    after_cash,after_grams=cash,holding
    if action=='buy':
        if grams*price>cash+1e-8:return None
        after_cash-=grams*price;after_grams+=grams
    elif action=='sell':
        if grams>holding+1e-8:return None
        after_cash+=grams*price*(1-fee);after_grams-=grams
    after_gross=after_cash+after_grams*price
    result={'price_cny':price,'account_change_cny':round(changed,2),
            'account_change_pct':round(changed/net_now*100,4),
            'position_after_pct':round(after_grams*price/after_gross*100,4) if after_gross>0 else None,
            'sale_fee_cny':round(grams*price*fee,2) if action=='sell' else 0.}
    cost=number(source.get('average_cost_cny'))
    if action=='sell' and cost is not None:
        result['position_pnl_cny']=round(grams*(price*(1-fee)-cost),2)
    return result


def annotate(value,source):
    for scenario in value.get('downside_scenarios',[]):
        scenario['account_effect']=effect(source,scenario['price_cny'])
    for order in value.get('suggested_orders',[]):
        side='buy' if order['kind']=='limit_buy' else 'sell'
        order['account_effects']={'trigger':effect(source,order['price_cny'],side,order['grams'])}
        if order['kind']=='take_profit_stop_loss':
            order['account_effects']['stop_loss']=effect(source,order.get('loss_price_cny'),side,order['grams'])


def buy_size_metrics(advice, entry, grams, *, target=None, exit_price=None):
    """Recalculate a proposed batch at its entry price using the saved bank inputs."""
    state = advice.get('input_state') or {}
    assets = advice.get('input_account_risk') or {}
    cash, holding = number(state.get('cash_cny')), number(state.get('total_grams'))
    available = number(advice.get('input_participation_cash_cny'))
    net = number(assets.get('net_assets_cny'))
    data = {'entry_price_cny': entry, 'suggested_grams': grams}
    if available is not None:
        data['cash_cap_grams'] = floor(available / entry * 100) / 100
    gross_at_entry = cash + holding * entry if cash is not None and holding is not None else None
    sized = grams
    if target is not None and gross_at_entry:
        target_grams = max(0., (target / 100 * gross_at_entry) / entry - holding)
        data.update(target_position_pct=target, target_add_grams=round(target_grams, 4))
        if sized is None:
            sized = target_grams
    if sized is not None:
        spending = sized * entry
        data['spending_cny'] = round(spending, 2)
        if net:
            data['spending_nav_pct'] = round(spending / net * 100, 4)
        if gross_at_entry and spending <= cash + 1e-8:
            data['position_after_pct'] = round((holding + sized) * entry / gross_at_entry * 100, 4)

    fee = number(advice.get('input_sell_fee_rate'))
    if not exit_price or exit_price >= entry or fee is None or fee >= 1:
        return data
    net_at_exit = exit_price * (1 - fee)
    added_loss = entry - net_at_exit
    data.update(exit_price_cny=exit_price, added_loss_cny_per_gram=round(added_loss, 4))
    current_sell = number(advice.get('input_quote_cny'))
    budget = number(assets.get('risk_budget_cny'))
    if holding is None or not current_sell or not net:
        return data
    held_loss = holding * (current_sell - exit_price) * (1 - fee)
    if budget is not None:
        remaining_risk = max(0., budget - max(0., held_loss))
        data['risk_cap_grams'] = floor(remaining_risk / added_loss * 100) / 100
    if sized is not None and cash is not None and sized * entry <= cash + 1e-8:
        account_delta = -(held_loss + sized * added_loss)
        data.update(account_at_exit_change_cny=round(account_delta, 2),
                    account_at_exit_change_pct=round(account_delta / net * 100, 4))
    return data


def buy_sizing(advice):
    """Keep immediate quantities and conditional target illustrations distinct."""
    if ((advice.get('input_data_status') or {}).get('account') != 'fresh'
            or advice.get('input_quote_status') != 'fresh'):
        return []
    path = advice.get('position_path') or {}
    target = number(path.get('target_position_pct')) if not path.get('checks') else None
    if target is not None and target > 100:
        target = None
    result = []
    if advice.get('action') == 'buy' and (grams := number(advice.get('grams'))):
        reference = number(advice.get('price_cny')) or 0
        quote = number(advice.get('input_buy_quote_cny')) or 0
        entry = reference if advice.get('price_condition') == 'at_or_below' else max(reference, quote)
        if entry:
            exit_price = number(path.get('boundary_price_cny')) if not path.get('checks') else None
            result.append({'kind': 'immediate', **buy_size_metrics(advice, entry, grams, target=target, exit_price=exit_price)})
    candidates = [review for review in advice.get('limit_buy_reviews', []) if not review.get('checks')
                  and review.get('decision') in ('recommend', 'needs_review') and number(review.get('entry_price_cny'))]
    candidate_target = target if advice.get('action') in ('hold', 'wait') and len(candidates) == 1 else None
    for review in candidates:
        grams = number(review.get('grams'))
        if grams or candidate_target is not None:
            result.append({'kind': review['decision'], **buy_size_metrics(advice, float(review['entry_price_cny']),
                grams, target=candidate_target, exit_price=number(review.get('risk_exit_cny')))})
    return result


def validate_downside(value,source,require=False):
    warnings=[];seen=set();price=quote_price(source)
    scenarios=value.get('downside_scenarios',[])
    for scenario in scenarios:
        checks=[];level=scenario['price_cny']
        if level in seen:checks.append('重复的下跌情景价位')
        seen.add(level)
        if price is not None and level>=price:checks.append('下跌情景价须低于本轮银行报价')
        scenario['checks']=checks
        warnings.extend('下跌情景：'+c for c in checks)
    for order in value.get('suggested_orders',[]):
        if order['kind']!='take_profit_stop_loss':continue
        matching=[s for s in scenarios if abs(s['price_cny']-(order.get('loss_price_cny') or 0))<1e-8 and not s.get('checks')]
        check=None
        if not matching and require:check='组合止损缺少同价的下跌加仓/持有/退出比较'
        elif matching and not any(s['price_role'] in ('thesis_invalidation','risk_limit') and s['preferred_action']=='sell' for s in matching):
            check='组合止损与下跌情景选择不一致，压力测试或入场参考不能直接当作卖出依据'
        if check:
            order.setdefault('checks',[]).append(check);warnings.append('条件单建议：'+check)
    return warnings


ROLES={'stress_test':'压力测试','thesis_invalidation':'假设失效','risk_limit':'自选风险限制','entry_reference':'入场观察'}
ACTIONS={'buy':'加仓','hold':'继续持有','sell':'减仓/退出','reassess':'先复评'}


def format_downside(value):
    lines=[]
    for s in value.get('downside_scenarios',[]):
        if s.get('checks'):continue
        lines.append(f"下跌情景 {s['price_cny']:g}元/克 · {ROLES.get(s['price_role'],s['price_role'])} · 倾向{ACTIONS.get(s['preferred_action'],s['preferred_action'])}（不是已挂单）")
        lines.extend([s['basis'],'加仓：'+s['buy_case'],'持有：'+s['hold_case'],'退出：'+s['sell_case']])
        metrics=s.get('account_effect') or {}
        if metrics.get('account_change_pct') is not None:lines.append(f"维持现仓到该价，账户净值变动约{metrics['account_change_pct']:+.2f}%（一致扣卖费估值，未计跳价）。")
    return '\n\n'.join(lines)+'\n\n' if lines else ''
