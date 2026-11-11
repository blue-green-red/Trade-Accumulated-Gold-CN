"""Independent lower-entry reviews and verified arithmetic; no order submission."""
from typing import Literal,Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel,Field
from .trade_costs import number
from .jd_live_risk import quote_price,effect
from .jd_live_conditions import stamp


class LimitBuyReview(BaseModel):
    decision:Literal['recommend','not_worthwhile','needs_review','unavailable']
    entry_price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    target_price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    risk_exit_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False,description='新增买入的风险退出参考，不自动设置任何持仓止损')
    grams:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    expires_at:Optional[str]=Field(default=None,max_length=40,description='候选方案带时区截止时间；未知null')
    basis:str=Field(min_length=1,max_length=180,description='各价位及幅度依据；保留/改动旧候选的新增事实，百分比也须解释，不把风险预算或压力价直接当入场依据')
    reason:str=Field(min_length=1,max_length=180,description='参与/等待的具体取舍；需复评时给可验证前提、截止和不同结果对应行动，不仅写价格单无法辨因；不强制交易')


def metrics(review,source):
    entry,target,exit_price,grams=(number(review.get(k)) for k in ('entry_price_cny','target_price_cny','risk_exit_cny','grams'))
    fee=number(source.get('sell_fee_rate'));cash=number(source.get('cash_cny'));holding=number(source.get('total_grams'))
    if entry is None or target is None or exit_price is None or not 0<exit_price<entry<target or fee is None or fee>=1:return None
    up=target*(1-fee)/entry-1;down=exit_price*(1-fee)/entry-1
    result={'net_upside_pct':round(up*100,4),'net_exit_return_pct':round(down*100,4)}
    if grams is None:return result
    result.update(budget_cny=round(grams*entry,2),new_buy_upside_cny=round(grams*(target*(1-fee)-entry),2),
                  new_buy_exit_pnl_cny=round(grams*(exit_price*(1-fee)-entry),2))
    arrival=effect(source,entry,'buy',grams)
    if arrival:result['position_after_pct']=arrival['position_after_pct']
    current=quote_price(source)
    if current is not None and cash is not None and holding is not None and grams*entry<=cash+1e-8:
        current_net=cash+holding*current*(1-fee)
        future_net=cash-grams*entry+(holding+grams)*exit_price*(1-fee)
        if current_net>0:
            result['account_at_exit_change_cny']=round(future_net-current_net,2)
            result['account_at_exit_change_pct']=round((future_net/current_net-1)*100,4)
    return result


def same_order(review,order):
    return (order.get('kind')=='limit_buy' and review.get('entry_price_cny')==order.get('price_cny')
            and review.get('grams')==order.get('grams') and stamp(review.get('expires_at'))==stamp(order.get('expires_at')))


def signature(value):
    """Only changes to an actually recommended new draft are actionable strategy changes."""
    value=value or {}
    fields=('entry_price_cny','target_price_cny','risk_exit_cny','grams')
    return sorted(tuple(r.get(k) for k in fields) for r in value.get('limit_buy_reviews',[])
                  if r.get('decision')=='recommend' and not r.get('checks') and all(r.get(k) is not None for k in fields)
                  and any(not o.get('checks') and same_order(r,o) for o in value.get('suggested_orders',[])))


def validate(value,source,now,require=False):
    warnings=[];reviews=value.get('limit_buy_reviews',[]);orders=value.get('suggested_orders',[])
    price=quote_price(source);cash=number(source.get('cash_cny'))
    for review in reviews:
        checks=[];entry=review.get('entry_price_cny');target=review.get('target_price_cny');exit_price=review.get('risk_exit_cny')
        if entry is not None and price is not None and entry>=price:checks.append('低位入场价须低于本轮银行报价')
        if entry is not None and target is not None and target<=entry:checks.append('目标价须高于候选入场价')
        if entry is not None and exit_price is not None and exit_price>=entry:checks.append('新增风险退出参考须低于候选入场价')
        if review.get('expires_at') is not None and (stamp(review['expires_at']) is None or stamp(review['expires_at'])<=now):checks.append('候选方案期限须含时区且晚于当前时间')
        if entry is not None and review.get('grams') is not None and cash is not None and entry*review['grams']>cash+1e-8:checks.append('候选买入金额超过本户已知现金')
        if review['decision']=='recommend':
            if any(review.get(k) is None for k in ('entry_price_cny','target_price_cny','risk_exit_cny','grams','expires_at')):checks.append('推荐提前挂单须完整给出入场、目标、新增退出、数量和期限')
            fee=number(source.get('sell_fee_rate'))
            if fee is None or fee>=1:checks.append('本户卖费未知，无法核对候选收益风险')
            matching=[o for o in orders if same_order(review,o)]
            if not matching:checks.append('推荐评估缺少同参数限价买单')
            elif any(o.get('checks') for o in matching):checks.append('对应买单的报价、资金占用或期限需核对')
        review['checks']=checks
        review['metrics']=metrics(review,source)
        warnings.extend('限价评估：'+c for c in checks)
    for order in orders:
        if order['kind']!='limit_buy':continue
        matching=[r for r in reviews if same_order(r,order) and r['decision']=='recommend' and not r.get('checks')]
        if require and not matching:
            check='条件买单缺少同参数、明确推荐且完整的独立限价评估'
            order.setdefault('checks',[]).append(check);warnings.append('条件单建议：'+check)
    return warnings


DECISIONS={'recommend':'建议提前挂出','not_worthwhile':'候选机会不值得参与','needs_review':'仍需复评，不建议提前挂出','unavailable':'关键信息不足'}


def format_reviews(value):
    lines=[]
    for r in value.get('limit_buy_reviews',[]):
        if r.get('checks'):continue
        lines.append('低位买入评估：'+DECISIONS[r['decision']])
        known=[f'{label}{r[key]:g}元/克' for key,label in (('entry_price_cny','入场≤'),('target_price_cny','目标'),('risk_exit_cny','新增退出参考')) if r.get(key) is not None]
        if r.get('grams') is not None:known.append(f"{r['grams']:g}克")
        expires=stamp(r.get('expires_at'))
        if expires:known.append('候选截止'+expires.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M 北京时间'))
        if known:lines.append(' · '.join(known))
        lines.extend((r['basis'],r['reason']))
        m=r.get('metrics') or {}
        if m:lines.append(f"新增扣费目标收益{m['net_upside_pct']:+.2f}% / 退出收益{m['net_exit_return_pct']:+.2f}%（假设，不是保证）。")
    return '\n\n'.join(lines)+'\n\n' if lines else ''
