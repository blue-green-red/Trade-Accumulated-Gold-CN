"""Consistent sale cashflows and performance attribution; never decides whether to trade."""
import math

PROMPT = """\n成本与波段比较（模拟、真实及公众内容共用）：每克卖出净回款N(P)=P*(1-f)-s，f为卖费、s为每克价差。新买入看N(T)/E-1；持有相对现卖看N(T)/N(P)-1，不能只在持有路径扣费。卖后无费买回同克数须≤N(P)；分批按金额计费，不按笔数倍增。
比较一次参与、分批、等待在先涨、再跌和失效时的扣费净值，计累计投入、均价及整仓损失。未知概率不编造，盈亏比≤1、预算富余、未收复均线均非单独决策依据。
账户按participation_preference选择：wait在证据分歧时偏等待，balanced均衡比较，participate在获利假设有事实支持、整仓损失可控且无足以否定该规模参与的具体反证时优先有限参与；已有小仓不自动完成参与目标。反证须对应交易假设、期限和规模；弱日线、鹰派背景或未确认利好不单独等同假设失效，多项证据可联合支持等待。明确不利预期、假设失效、执行缺口或风控限制仍可选0，不伪造机会。
模拟/真实账户按各户净资产、现仓与机会独立选择有实质参与作用的目标仓位，再推金额并核对确认现金及整仓风险；旧试买金额不作默认。reason用同一E/T/R比较0、更小及更大追加的扣费净值与整仓损失，解释规模取舍，再说明事件影响。选0给最强参与理由与具体反证；缺确认或小额不提高胜率不足以单独否决。预算是上限，不要求用满，无固定金额、最低仓位或每轮买入要求，允许有据等待，不强制买入。
participation_review是纸上对照：1%/5%净资产（限确认现金）、报价±2%的扣费差额，非建议仓位或阈值。reason至少引用一组上下行情景金额，解释对当前暴露的取舍。waiting为输入报价到当前的1%持有对照，分价格贡献、退出成本和观察分钟；短观察且费用主导的负差额不证明波段等待更优，也不用事后上涨惩罚等待。小额改变绝对损失与上涨参与，不创造胜率优势。
路径在事件仍无新增结论时，分别给报价上涨/回调后的参与条件及规模或放弃理由，不能把新增消息确认设为唯一切换条件。只写“再评/重算”及时间不够，说明什么变化改变当轮取舍。无合理情景不凑价或交易；后批须重评，浮亏不自动追加，中性/延期/缺数也独立决策。
T/R先按同一交易假设与期限选择，不倒推价格支持买或不买。T解释本周期可达路径、当轮证据及近端/延伸用途，均线映射不等于可达。R区分假设失效与自主损失限制，结合现有且时效合适的波动/结构、期限及全仓损失解释为何选此幅度，只说“自选”不够。同规模对照一组有依据的邻近T/R，简述动作是否敏感；无替代依据直说，不凑价。缺数据不编ATR或京东技术位，也不设新禁买前提。
stop_price/target_price是生效保护，position_comparison仅算现仓净额，均非新交易默认价。沿用或改价给当轮依据，旧边界已触须重审假设；采样首末/极值不单独支持缩短波段目标或扩大退出幅度。不强制买入、改价或持有到目标，目标非涨幅上限。"""


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
