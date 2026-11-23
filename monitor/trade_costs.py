"""Consistent sale cashflows and performance attribution; never decides whether to trade."""
import math

PROMPT = """\n成本与波段比较（模拟、真实及公众内容共用）：每克卖出净回款N(P)=P*(1-f)-s，f为卖费、s为每克价差。新买入看N(T)/E-1；持有相对现卖看N(T)/N(P)-1，不能只在持有路径扣费。卖后无费买回同克数须≤N(P)；分批按金额计费，不按笔数倍增。
比较一次参与、分批、等待在先涨、再跌和失效时的扣费净值，计累计投入、均价及整仓损失。未知概率不编造，盈亏比≤1、预算富余、未收复均线均非单独决策依据。
账户按participation_preference选择：wait在证据分歧时偏等待，balanced均衡比较，participate在获利假设有事实支持、整仓损失可控且无足以否定该规模参与的具体反证时优先有限参与；已有小仓不自动完成参与目标。反证须对应交易假设、期限和规模；弱日线、鹰派背景或未确认利好不单独等同假设失效，多项证据可联合支持等待。明确不利预期、假设失效、执行缺口或风控限制仍可选0，不伪造机会。
账户按净资产、现仓与机会选择目标规模，推算金额并核对确认现金、整仓风险；旧金额或凑整仓位不作默认。reason以同一E/T/R比较0、更小及更大追加，分开现仓贡献与新增扣费收益/损失，点明多买这部分的额外得失及支持其规模的事实；目标仓位和预算可承受不能代替买入优势。未来24小时有重要事件，比较一次参与、部分参与、公布后再定，明确首选、为何不选另两项及切换条件；允许提前布局，不默认等待或分批。规模明显变化对照previous_advice解释新事实或观点重估、旧规模为何不再合适及整仓损失增减；旧建议非成交或上限。选0给最强参与理由与具体反证；缺确认或小额不提高胜率不足以单独否决。遵循本户数量限制，预算不必用满，无固定仓位或每轮交易要求。
participation_review是纸上对照：1%/5%净资产（限确认现金）、报价±2%的扣费差额，非建议仓位或阈值。示例金额不必复述，优先说明本轮实际数量的增量价值。waiting为输入报价到当前的1%持有对照，分价格贡献、退出成本和观察分钟；短观察且费用主导的负差额不证明波段等待更优，也不用事后上涨惩罚等待。小额改变绝对损失与上涨参与，不创造胜率优势。
路径在事件仍无新增结论时，分别给报价上涨/回调后的参与条件及规模或放弃理由，不能把新增消息确认设为唯一切换条件。只写“再评/重算”及时间不够，说明什么变化改变当轮取舍。无合理情景不凑价或交易；后批须重评，浮亏不自动追加，中性/延期/缺数也独立决策。
T/R先按同一交易假设和期限选择，不倒推支持动作。T用本轮事实解释期限内实现所需的市场路径，区分证据与假设；均线映射或旧目标本身不证明可达，沿用仍须当轮依据。R区分假设失效与自选损失限制，结合时效合适的波动/结构、期限和整仓损失解释幅度。同规模检验有依据的较弱反弹及邻近退出情景下动作是否改变，不能只用近乎相同目标充当敏感性比较；无替代依据直说，不凑价、不编ATR或京东技术位，不设新禁买前提。
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


def bank_review_unit_payoff(entry, zone, fee, *, current_sell=None, holding=None):
    entry = number(entry)
    target = cost_reference(zone.get('weak_target_cny'), fee)
    stop = cost_reference(zone.get('risk_exit_cny'), fee)
    if not entry or not target or not stop:
        return None
    exit_net = stop['net_sell_price_cny']
    result = {'weak_target_net_cny_per_gram': target['net_sell_price_cny'] - entry,
              'risk_exit_net_cny_per_gram': exit_net - entry}
    holding_now = cost_reference(current_sell, fee)
    holding = number(holding)
    if holding_now is not None and holding is not None:
        result['hold_only_at_exit_delta_cny'] = holding * (exit_net - holding_now['net_sell_price_cny'])
    return result


def bank_review_payoff(entry, grams, zone, fee, *, current_sell=None, holding=None):
    grams = number(grams)
    unit = bank_review_unit_payoff(entry, zone, fee, current_sell=current_sell, holding=holding)
    if not grams or unit is None:
        return None
    added_at_exit = unit['risk_exit_net_cny_per_gram'] * grams
    result = {'weak_target_net_cny': round(unit['weak_target_net_cny_per_gram'] * grams, 2),
              'risk_exit_net_cny': round(added_at_exit, 2)}
    if (held := unit.get('hold_only_at_exit_delta_cny')) is not None:
        result.update(hold_only_at_exit_delta_cny=round(held, 2),
                      hold_plus_candidate_at_exit_delta_cny=round(held + added_at_exit, 2))
    return result


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
