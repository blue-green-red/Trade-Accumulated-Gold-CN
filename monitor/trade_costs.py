"""Consistent sale cashflows and performance attribution; never decides whether to trade."""
import math

PROMPT = """\n成本与波段比较（模拟、真实及公众内容共用）：每克卖出净回款N(P)=P*(1-f)-s，f为卖费、s为每克价差。新买入看N(T)/E-1；持有相对现卖看N(T)/N(P)-1，不能只在持有路径扣费。卖后无费买回同克数须≤N(P)；分批按金额计费，不按笔数倍增。
比较一次参与、分批与等待在先涨、再跌和假设失效时的扣费净值；分批计累计投入、均价及整仓损失，不只看首笔盈亏比，也不以摊低成本掩盖坏机会。依据含收益、损失、情景可能性；未知概率不编造，盈亏比≤1非单独禁买理由。高风险有合理获利依据且损失可承受时偏向提前参与，信号分歧优先用规模和节奏处理；预算足够非买入理由，利率回落或收复均线非通用前提。
模拟/真实账户先决策、后看事件：可追加时reason先在事件仍未知的当前状态，用同一E/T/R比较不追加、自选小额及常规一笔的金额和整仓损失；再解释事件结果如何调整规模或退出，不能用利好后的情景替代当前比较。选择0须说明当前证据为何使两个非零规模都不值得，计买贵/踏空代价；弱趋势、没有新催化或旧建议里的等待条件不能单独成为门槛。不要求编造概率、固定盈亏比或强制买入。
路径区分本轮动作与后续目标，给“本轮→后续”1—2步条件及规模；分别评估报价变化和事件信息，不预设必须先有利好。无可行扩仓情景说明障碍，不凑买点或更高目标；首笔不求最低点，后批须重评，浮亏不自动追加。若仍等事件，说明其信息价值为何优于当前可行规模，中性/延期/缺数时独立选择行动与复评截止，不自动续等；已发生条件先更新事实。
T/R先按同一交易假设与期限选择，不倒推价格支持买或不买。T解释本周期可达路径、当轮证据及近端/延伸用途，均线映射不等于可达。R区分假设失效与自主损失限制，结合现有且时效合适的波动/结构、期限及全仓损失解释为何选此幅度，只说“自选”不够。同规模对照一组有依据的邻近T/R，简述动作是否敏感；无替代依据直说，不凑价。缺数据不编ATR或京东技术位，也不设新禁买前提。
stop_price/target_price是生效保护，position_comparison仅算现仓净额，均非新交易默认价。沿用或改价给当轮依据，旧边界已触须重审假设；采样极值非单独退出依据。不强制买入、改价或持有到目标，目标非涨幅上限。"""


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
