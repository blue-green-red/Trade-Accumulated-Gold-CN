"""Compact human-readable real-account notifications, never executable instructions."""
import re
from .config import BJ
from .jd_live_conditions import stamp
from .jd_live_entry import same_order
from .jd_live_notice_rules import LABELS
from .trade_costs import number


def time_text(value):
    at=stamp(value)
    return at.astimezone(BJ).strftime('%m-%d %H:%M') if at else '待确认'


def brief(text,limit=160):
    text=re.sub(r'(?<![A-Za-z])[ETR](?![A-Za-z])',
                lambda m:{'E':'入场价','T':'目标价','R':'风险参考价'}[m[0]],str(text or ''))
    text=re.sub(r'(?<![A-Za-z])MA(20|50)(?!\d)',lambda m:m[1]+'日均线',text)
    text=' '.join(text.split())
    if len(text)<=limit:return text
    end=max(text.rfind(mark,0,limit) for mark in ('。','！','？','；'))
    return text[:end+1]+'…（详见账户页）' if end>=limit//2 else text[:limit]+'…（详见账户页）'


def terms(order):
    grams=order.get('grams');price=order.get('price_cny')
    quantity=f'{grams:g}克' if grams is not None else '数量待确认'
    side={'buy':'买入','sell':'卖出'}.get(order.get('action'),'')
    if order.get('kind')=='take_profit_stop_loss':
        loss=order.get('loss_price_cny')
        prices=('止盈 ≥ '+(f'{price:g}元/克' if price is not None else '待确认')+
                ' / 止损 ≤ '+(f'{loss:g}元/克' if loss is not None else '待确认'))
    else:
        symbol={'limit_buy':'≤','limit_sell':'≥'}.get(order.get('kind')) or {'at_or_below':'≤','at_or_above':'≥','reference':'参考价'}.get(order.get('price_condition'),'参考价')
        prices=f'{symbol} {price:g}元/克' if price is not None else '价格待确认'
    filled=order.get('filled_grams')
    remaining=(f'（已成交{filled:g}克）' if filled is not None and filled>0 else
               '（部分成交，剩余量待核对）' if order.get('status')=='partially_filled' else '')
    return f'{side}{quantity}{remaining} · {prices} · 截止{time_text(order.get("expires_at"))}'


def body(account,current,value,notice_reasons=(),proposal_withdrawn=False):
    action=value['action'];partial=value.get('notification_partial')
    drafts=value.get('suggested_orders',[])
    management=value.get('order_management',[])
    manual=[o for o in management if o['action'] in ('cancel','replace')]
    conclusions={'buy':'建议买入','sell':'建议卖出','hold':'继续持有，本轮不立即买卖','wait':'继续观望，本轮不立即买卖'}
    lines=['**本轮结论：'+conclusions[action]+'。**']
    tasks=[conclusions[action]] if action in ('buy','sell') else []
    if drafts:tasks.append(f'{len(drafts)}笔新条件单建议')
    for operation,label in (('cancel','撤单'),('replace','改单')):
        count=sum(o['action']==operation for o in manual)
        if count:tasks.append(f'{count}笔{label}建议')
    lines.append('需要操作：请逐项核对并手动确认'+'、'.join(tasks)+'。' if tasks else
                 '需要操作：暂无新增买卖、挂单或撤改建议。')
    context=[]
    position=number(value.get('input_position_pct'));quote=number(value.get('input_quote_cny'))
    if position is not None:context.append(f'分析时黄金仓位{position:.1f}%')
    if quote is not None:context.append(f'分析参考价{quote:g}元/克')
    if context:lines.append(' · '.join(context))
    if notice_reasons:lines.append('提醒原因：'+'；'.join(LABELS[r] for r in notice_reasons)+'。')
    if action in ('buy','sell'):
        quantity=f"{value['grams']:g}克" if value.get('grams') is not None else '数量待确认'
        condition={'at_or_above':'价格≥','at_or_below':'价格≤','reference':'参考价','none':'未设价格条件'}[value['price_condition']]
        price=f"{value['price_cny']:g}元/克" if value.get('price_cny') is not None else ''
        note='（参考价，不是触发条件）' if value['price_condition']=='reference' else ''
        lines.append('新增买卖建议：'+{'buy':'买入','sell':'卖出'}[action]+' '+quantity+' · '+condition+price+note)
    for item in manual:
        original=item.get('input_order') or {}
        label={'cancel':'建议撤单','replace':'建议改单'}[item['action']]
        line=label+' · 原单'+terms(original)
        if item['action']=='replace':line+='\n→ 修改后'+terms(item.get('effective_replacement') or {})
        if item.get('reason'):line+='\n'+brief(item['reason'],80)
        lines.append(line)
    if manual:lines.append('撤改前请核对剩余量及成交状态；尚未操作，不视为释放资金或克数。')
    keep=sum(o['action']=='keep' for o in management)
    if keep:lines.append(f'已有挂单：建议保留{keep}笔，原参数不变。')
    if any(o['kind']=='take_profit_stop_loss' for o in drafts):
        protection=account.get('protection_state') or {}
        state=protection.get('status')
        lines.append('当前保护查询：'+('未发现已挂组合止盈止损单；以下为新挂建议。' if state=='none' else
            f"已读取{protection.get('count',0)}笔组合止盈止损单，不代表覆盖全部持仓；以下方案需核对。" if state=='present' else
            '挂单明细不足以确认是否已有保护；请先核对，以下仅为建议。'))
    for order in drafts:
        kind=order['kind']
        side='buy' if kind=='limit_buy' else 'sell'
        lines.append('建议预挂（尚未提交京东）：'+terms({**order,'action':side}))
        if order.get('reason'):lines.append(brief(order['reason'],80))
        for leg,label in (('trigger','本笔成交'),('stop_loss','本笔止损成交')):
            effect=(order.get('account_effects') or {}).get(leg) or {}
            if effect.get('position_pnl_cny') is not None:
                lines.append(f"{label}：按买入均价估算、已扣卖费的盈亏{effect['position_pnl_cny']:+.2f}元；整户相对分析时估值变化{effect['account_change_cny']:+.2f}元。")
        for review in value.get('limit_buy_reviews',[]):
            if review.get('checks') or review['decision']!='recommend' or not same_order(review,order):continue
            lines.append(f"评估目标{review['target_price_cny']:g}元/克 · 新增退出参考{review['risk_exit_cny']:g}元/克（仅风险参考，不是已挂止损）。")
    if proposal_withdrawn:
        lines.append('本轮不再建议新挂上一轮条件单；已挂到京东的单以本轮挂单处理建议为准。')
    for review in value.get('limit_buy_reviews',[]):
        if review.get('checks') or review['decision']=='recommend':continue
        label={'needs_review':'仅待复评，暂不建议提前挂单；候选价格不代表买入指令',
               'not_worthwhile':'本轮不建议参与该候选方案','unavailable':'信息不足，暂未形成可挂单方案'}[review['decision']]
        lines.append('低位买入：'+label+'。\n'+brief(review.get('reason'),100))
    conditions=value.get('review_conditions',[])
    if conditions:
        observations=[]
        for c in conditions:
            symbol={'at_or_above':'≥','at_or_below':'≤'}[c['condition']]
            scope='（仅针对指定挂单）' if c.get('order_key') else ''
            observations.append(f"银行报价 {symbol} {c['price_cny']:g}元/克{scope}")
        lines.append('价格复评：'+'；'.join(observations)+'。\n到价只触发重新分析，不执行交易；受交易时段、数据状态及本条建议有效期限制。')
    if partial:
        lines.append('仅推送已通过独立校验的部分；其余建议需核对，请查看账户页明细。')
    else:
        reason=value.get('reason') if action in ('buy','sell') else (value.get('reassessment') or {}).get('thesis') or value.get('reason')
        if reason:lines.append('简要依据：'+brief(reason))
        if value.get('risk_note'):lines.append('风险：'+brief(value['risk_note'],90))
    orders=account.get('pending_orders') or {}
    order_note=f"已读取{len(orders.get('records',[]))}笔待成交明细" if orders.get('status')=='fresh' else '条件单明细待更新'
    tracking=(current.get('execution_progress') or {}).get('status')
    progress={'pending':'发现对应或相近挂单，仍待成交','partially_filled':'对应或相近条件单已有部分成交','filled':'对应或相近条件单已成交'}.get(tracking)
    lines.append('委托查询：'+order_note+('；'+progress if progress else '')+'。')
    lines.append(f"有效至{time_text(current.get('expires_at'))} · 建议 #{current['id']}\n"
                 f"账户更新{time_text(account.get('fetched_at'))} · 报价更新{time_text((account.get('quote') or {}).get('quoted_at'))}（以上均为北京时间）")
    lines.append('真实账户建议，需本人手动确认；程序未自动下单。')
    bank=account['bank']
    lines.append(f'[查看完整分析与挂单明细](https://gold.goalgoal.fun/#strategies/live/{bank}/advice)')
    return '\n\n'.join(lines)
