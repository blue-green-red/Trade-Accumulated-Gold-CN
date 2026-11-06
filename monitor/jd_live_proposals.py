"""Conditional-order drafts for manual confirmation; never calls a trading endpoint."""
import json
from typing import Literal,Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel,Field
from .jd_live_conditions import stamp


class SuggestedOrder(BaseModel):
    kind:Literal['limit_buy','limit_sell','take_profit_stop_loss']
    grams:float=Field(gt=0,allow_inf_nan=False)
    price_cny:float=Field(gt=0,allow_inf_nan=False,description='限价买入上限、限价卖出下限或组合单止盈价，元/克')
    loss_price_cny:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False,description='仅组合止盈止损单填写止损价，其余null')
    expires_at:str=Field(max_length=40,description='含时区的ISO8601条件单截止时间，独立于建议有效期')
    reason:str=Field(min_length=1,max_length=240,description='价位来源/假设及该数量的作用，避免重复市场摘要')


def proposal_signature(value):
    # Rolling the advisory deadline or rewriting prose is not a new trading idea.
    fields=('kind','grams','price_cny','loss_price_cny')
    return sorted(json.dumps({k:o.get(k) for k in fields},sort_keys=True) for o in (value or {}).get('suggested_orders',[]))


def validate_proposals(value,source,now):
    proposals=value.get('suggested_orders',[])
    if not proposals:return []
    warnings=[];seen=set()
    part=source.get('pending_orders') or {}
    management={o['order_key']:o for o in value['order_management']}
    reserved_sell=(value.get('grams') or 0) if value['action']=='sell' else 0
    reserved_buy=(value.get('grams') or 0)*(value.get('price_cny') or 0) if value['action']=='buy' else 0
    if value['action']=='buy' and (not value.get('grams') or not value.get('price_cny')):
        warnings.append('条件单建议：本轮即时买入预算未明确，需先核对与条件单共同占用')
    for order in part.get('records',[]):
        if order.get('status') not in ('pending','partially_filled'):continue
        item=management.get(order['order_key'],{})
        effective=item.get('effective_replacement') if item.get('action')=='replace' and not item.get('checks') else None
        grams=max(0,(order.get('grams') or 0)-(order.get('filled_grams') or 0))
        if effective and (item.get('replacement') or {}).get('grams') is not None:grams=max(grams,effective.get('grams') or 0)
        if order.get('action')=='sell':reserved_sell+=grams or 0
        elif order.get('action')=='buy':reserved_buy+=max((grams or 0)*(order.get('price_cny') or 0),(grams or 0)*((effective or order).get('price_cny') or 0))
    quote=source.get('quote') or {};price=quote.get('sell_price_cny')
    for order in proposals:
        checks=[];expiry=stamp(order['expires_at'])
        if expiry is None or expiry<=now:checks.append('条件单截止时间须含时区且晚于当前时间')
        if source.get('status')!='fresh' or quote.get('status')!='fresh':checks.append('账户或银行报价过期，需刷新后确认挂单')
        if part.get('status')!='fresh' or not part.get('complete'):checks.append('待成交委托明细不完整或过期，需核对占用')
        signature=proposal_signature({'suggested_orders':[order]})[0]
        if signature in seen:checks.append('重复的条件单建议')
        seen.add(signature)
        for pending in part.get('records',[]):
            if pending.get('status') not in ('pending','partially_filled'):continue
            if proposal_signature({'suggested_orders':[pending]})==[signature]:
                checks.append('已有相同挂单，应使用挂单处理建议')
        if order['kind']=='limit_buy':
            reserved_buy+=order['grams']*order['price_cny']
            if order.get('loss_price_cny') is not None:checks.append('限价单不应附组合止损价')
            if source.get('cash_cny') is None:checks.append('本户可用资金未知，不能确认条件买单预算')
            elif reserved_buy>source['cash_cny']+1e-8:checks.append('条件买单与其他买入合计超过本户已知可用资金')
            if quote.get('buy_price_cny') is not None and quote['buy_price_cny']<=order['price_cny']:checks.append('当前已满足买入条件，需刷新报价确认是否仍适合挂单')
        else:
            reserved_sell+=order['grams']
            if order['grams']>(source.get('sellable_grams') or 0)+1e-8:checks.append('条件卖单超过当前可卖克数，不预支撤单释放量')
            if source.get('total_grams') is not None and reserved_sell>source['total_grams']+1e-8:checks.append('条件卖单与已有卖出合计超过本户持仓')
            if order['kind']=='take_profit_stop_loss':
                loss=order.get('loss_price_cny')
                if loss is None or loss>=order['price_cny']:checks.append('组合单须填写低于止盈价的止损价')
                if price is not None and (price>=order['price_cny'] or (loss is not None and price<=loss)):checks.append('当前已满足组合单条件，需刷新报价确认')
            else:
                if order.get('loss_price_cny') is not None:checks.append('限价单不应附组合止损价')
                if price is not None and price>=order['price_cny']:checks.append('当前已满足卖出条件，需刷新报价确认是否仍适合挂单')
        order['checks']=checks
        warnings.extend('条件单建议：'+check for check in checks)
    return warnings


def format_proposals(value):
    lines=[]
    for order in value.get('suggested_orders',[]):
        label={'limit_buy':'限价买入 ≤','limit_sell':'限价卖出 ≥','take_profit_stop_loss':'止盈止损卖出，止盈 ≥'}[order['kind']]
        line=f"建议预挂：{order['grams']:g}克 · {label} {order['price_cny']:g}元/克"
        if order['kind']=='take_profit_stop_loss' and order.get('loss_price_cny') is not None:line+=f" / 止损 ≤ {order['loss_price_cny']:g}元/克"
        expiry=stamp(order.get('expires_at'))
        line+=' · 条件单截止'+(expiry.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M 北京时间') if expiry else '待确认')
        lines.append(line+'\n'+order['reason'])
        for leg,label in (('trigger','触发后'),('stop_loss','止损卖出后')):
            metrics=(order.get('account_effects') or {}).get(leg) or {}
            if metrics.get('position_after_pct') is not None:
                lines.append(f"{label}黄金仓位约{metrics['position_after_pct']:.1f}%（单笔独立估算）。")
    return ('**以下仅为待本人确认的挂单建议，尚未提交京东。**\n\n'+'\n\n'.join(lines)+'\n\n') if lines else ''
