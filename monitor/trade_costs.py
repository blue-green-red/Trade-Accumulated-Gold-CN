"""Consistent sale cashflows and performance attribution; never decides whether to trade."""
import math

PROMPT = """\n成本与波段比较（模拟、真实及公众内容共用）：每克卖出净回款N(P)=P*(1-f)-s，f为卖费、s为每克价差。新买入看N(T)/E-1；持有相对现卖看N(T)/N(P)-1，不能只在持有路径扣费。卖后无费买回同克数须≤N(P)；分批按金额计费，不按笔数倍增。
比较一次参与、分批与等待在先涨、再跌和假设失效时的扣费净值；分批计累计投入、均价及整仓损失，不只看首笔盈亏比，也不以摊低成本掩盖坏机会。依据含收益、损失、情景可能性；未知概率不编造，盈亏比≤1非单独禁买理由。高风险有合理获利依据且损失可承受时偏向提前参与，信号分歧优先用规模和节奏处理；预算足够非买入理由，利率回落或收复均线非通用前提。
分开本轮动作与条件成立后的仓位目标：有可行扩仓情景时给“本轮→改善后”1—2步条件及规模，不只重复现仓；无可行情景说明具体障碍及解除条件，不凑更高目标。可先涨后加或再跌再买，首笔不求最低点，后批须重评，浮亏不自动追加。
等待或暂停追加须解释哪个事实使较小一笔也不值得，并计买贵/踏空代价。若等事件，说明何种结果改变哪条收益/风险假设、其信息价值为何优于先参与；事件待公布非禁买条件。仅列弱趋势、无新增利好或“等PPI”不够；中性、延期或仍缺数时独立选择行动及最迟复评点，不自动续等。条件已发生先更新事实；允许合理等待，不强制买入。
stop_price/target_price为生效保护，position_comparison仅算已有持仓净额，均非新交易默认价。沿用旧价给当前依据，调整目标或提前退出说明日线/事件假设变化，采样极值不能单独支持缩短目标或退出。不强制改价、持有到目标；目标不是涨幅上限。"""


def number(value):
    if value is None or isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError,OverflowError):return None
    return value if math.isfinite(value) and value>=0 else None


def cost_reference(price,fee,spread=0):
    price,fee,spread=number(price),number(fee),number(spread)
    if price is None or price<=0 or fee is None or fee>=1 or spread is None:return None
    net=price*(1-fee)-spread
    if net<=0:return None
    return {'net_sell_price_cny':round(net,6)}


def position_comparison(grams,price,fee,spread=0,target=None,stop=None):
    ref=cost_reference(price,fee,spread);grams=number(grams)
    if ref is None or grams is None:return None
    # Keep full precision in cashflows; round only values presented to the model.
    current=float(price)*(1-float(fee))-float(spread)
    result={**ref,'position_liquidation_cny':round(grams*current,2)}
    if grams>0:
        scenarios={}
        for name,level in (('target',target),('stop',stop)):
            if cost_reference(level,fee,spread) is None:continue
            future=float(level)*(1-float(fee))-float(spread)
            scenarios[name]={'price_cny':float(level),'delta_vs_sell_now_cny':round(grams*(future-current),2),
                             'holding_return_pct':round((future/current-1)*100,4)}
        if scenarios:result['scenarios']=scenarios
    return result


def pnl_breakdown(account,price,settings,buys,sells,fees):
    """Infer historical spread from the ledger, rather than applying today's setting to old fills."""
    gross_realized=sells-buys+account['cost']
    paid_spread=gross_realized-account['realized_pnl']-fees
    if abs(paid_spread)<1e-7:paid_spread=0.
    grams=account['grams']
    valid=number(price) is not None and float(price)>0
    if valid:price=float(price)
    floating=grams*price-account['cost'] if valid else 0. if grams<1e-9 else None
    exit_fee=grams*price*settings.sell_fee_rate if valid else 0. if grams<1e-9 else None
    exit_spread=grams*settings.spread_cny
    gross=gross_realized+floating if floating is not None else None
    net=gross-fees-paid_spread-exit_fee-exit_spread if gross is not None and exit_fee is not None else None
    return {'price_pnl_cny':gross,'unrealized_price_pnl_cny':floating,'paid_fees_cny':fees,
            'paid_spread_cny':paid_spread,'estimated_exit_fee_cny':exit_fee,
            'estimated_exit_spread_cny':exit_spread,'net_pnl_cny':net}


def sum_breakdowns(accounts):
    fields=('price_pnl_cny','unrealized_price_pnl_cny','paid_fees_cny','paid_spread_cny',
            'estimated_exit_fee_cny','estimated_exit_spread_cny','net_pnl_cny')
    values=[a.get('pnl_breakdown') or {} for a in accounts]
    return {k:sum(v[k] for v in values) if values and all(v.get(k) is not None for v in values) else None for k in fields}
