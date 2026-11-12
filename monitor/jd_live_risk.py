"""Deterministic bank-account valuations and hypothetical effects; never places orders."""
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
