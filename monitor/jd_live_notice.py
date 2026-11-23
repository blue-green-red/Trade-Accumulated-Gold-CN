"""Compact human-readable real-account notifications, never executable instructions."""
import re
from .config import BJ
from .jd_live_conditions import stamp
from .jd_live_entry import same_order
from .jd_live_notice_rules import LABELS
from .trade_costs import number
from .jd_live_presentation import brief,basis


def time_text(value):
    at=stamp(value)
    return at.astimezone(BJ).strftime('%m-%d %H:%M') if at else '待确认'




def follow_up(value):
    """A short review checklist; the full conditional trading path stays on the account page."""
    path=value.get('position_path') or {}
    if not path or path.get('checks'):return ''
    boundary=number(path.get('boundary_price_cny'))
    conditions=[c for c in value.get('review_conditions',[]) if not c.get('checks')]
    exit_condition=None
    if boundary is not None and not any(c['condition']=='at_or_below' and c['price_cny']==boundary and not c.get('order_key') for c in conditions):
        exit_condition={'condition':'at_or_below','price_cny':boundary}
    if exit_condition:conditions=conditions[:3]+[exit_condition]
    lines=[]
    for c in conditions[:4]:
        direction='涨到' if c['condition']=='at_or_above' else '跌到'
        trigger=f"{direction}{c['price_cny']:g}元/克"
        if c['condition']=='at_or_below' and c['price_cny']==boundary and not c.get('order_key'):
            scope='现有' if path.get('exit_scope')=='plan' else '本批'
            reason=f'暂停加仓，核对挂单后按退出方案处理{scope}持仓'
        elif rule := c.get('bank_breakout'):
            trigger=f"突破{c['price_cny']:g}元/克"
            reason=f"持续走高或回踩守住满{rule['hold_minutes']}分钟后复评"
        elif review := c.get('buy_review'):
            if zone := review.get('zone'):
                interval=f"{zone['floor_cny']:g}–{c['price_cny']:g}元/克"
                trigger=f"突破{c['price_cny']:g}后回踩{interval}" if c['condition']=='at_or_above' else f"回调至{interval}"
            grams=review.get('grams')
            decision=f'复评{grams:g}克' if grams is not None else '复评是否加仓及数量'
            reason=(f"守住{review['recovery_price_cny']:g}元/克满{review['hold_minutes']}分钟，"
                    f"买价≤{review['max_buy_price_cny']:g}元/克时{decision}")
        else:
            raw=c.get('reason') or ''
            holding=re.match(r'^(?:比较|评估)保留(约?\d+(?:\.\d+)?)%与放弃追增',raw)
            entry=any(s.get('price_cny')==c['price_cny'] and s.get('price_role')=='entry_reference'
                      and s.get('preferred_action')=='reassess' and not s.get('checks') for s in value.get('downside_scenarios',[]))
            if holding:reason=f'重新评估是否维持{holding[1]}%仓位'
            elif entry and not c.get('order_key'):reason='重新评估是否加仓'
            else:reason=brief(raw,36).replace('…（详见账户页）','…').replace('比较','评估') or '重新分析，再决定是否买卖'
        scope='（指定挂单）' if c.get('order_key') else ''
        lines.append(f"• {trigger}{scope}：{reason}")
    if not lines:lines.append('下一次操作前，先更新账户并重新分析。')
    if path.get('manual_resolution_required'):
        lines.append('先确认旧买单已撤销或修改，再考虑新买单。')
    if watch := value.get('buy_watch'):
        lines.append(f"加仓观察截止{time_text(watch['expires_at'])}。")
    lines.append('到价只重新分析，不自动买卖；成交后更新账户并核对止盈止损数量。')
    return '之后怎么做：\n'+'\n'.join(lines) if lines else ''


def terms(order,*,multiline=False):
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
    separator='\n' if multiline else ' · '
    if multiline:prices=prices.replace(' / ','\n')
    return separator.join((f'{side}{quantity}{remaining}',prices,'截止'+time_text(order.get('expires_at'))))


def body(account,current,value,notice_reasons=(),proposal_withdrawn=False):
    action=value['action'];partial=value.get('notification_partial')
    drafts=value.get('suggested_orders',[])
    management=value.get('order_management',[])
    manual=[o for o in management if o['action'] in ('cancel','replace')]
    conclusions={'buy':'建议买入','sell':'建议卖出','hold':'继续持有，本轮不立即买卖','wait':'继续观望，本轮不立即买卖'}
    conclusion=conclusions[action]
    if value.get('immediate_buy_paused'):conclusion='即时买入建议已暂停'
    if value.get('immediate_buy_resumed'):conclusion='买入条件恢复，请按原建议核对'
    lines=['**本轮结论：'+conclusion+'。**']
    tasks=[conclusions[action]] if action in ('buy','sell') else []
    if drafts:tasks.append(f'{len(drafts)}笔新条件单建议')
    for operation,label in (('cancel','撤单'),('replace','改单')):
        count=sum(o['action']==operation for o in manual)
        if count:tasks.append(f'{count}笔{label}建议')
    if not tasks:lines.append('这次怎么做：暂无新增买卖、挂单或撤改建议。')
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
        line='这次怎么做：'+{'buy':'买入','sell':'卖出'}[action]+' '+quantity+'\n'+condition+price+note
        grams=number(value.get('grams'));entry=number(value.get('price_cny'))
        if action=='buy' and grams is not None and entry is not None:
            line+=f'\n按上述价格估算，需{grams*entry:,.0f}元。'
            held=number((value.get('input_state') or {}).get('total_grams'))
            assets=number((value.get('input_account_risk') or {}).get('gross_assets_cny'))
            if held is not None:
                line+=f'\n这次成交后，持仓约{held+grams:g}克'
                if assets and quote is not None:line+=f'，按分析参考价估算黄金仓位约{(held+grams)*quote/assets*100:.1f}%'
                line+='。'
        lines.append(line)
    for item in manual:
        original=item.get('input_order') or {}
        label={'cancel':'建议撤单','replace':'建议改单'}[item['action']]
        line=label+' · 原单'+terms(original,multiline=True)
        if item['action']=='replace':line+='\n→ 修改后'+terms(item.get('effective_replacement') or {},multiline=True)
        if item.get('reason'):line+='\n'+brief(item['reason'],80)
        lines.append(line)
    if manual:lines.append('撤改前核对还未成交的数量；撤改成功前，原单占用的资金或黄金仍不可重复使用。')
    keep=sum(o['action']=='keep' for o in management)
    if keep:lines.append(f'已有挂单：建议保留{keep}笔，原参数不变。')
    if any(o['kind']=='take_profit_stop_loss' for o in drafts):
        protection=account.get('protection_state') or {}
        state=protection.get('status')
        lines.append('已有持仓怎么处理：'+('目前没有已挂止盈止损单，建议按下面参数新挂。' if state=='none' else
            f"已读取{protection.get('count',0)}笔止盈止损单，需核对适用克数，避免重复挂单。" if state=='present' else
            '暂不能确认是否已有止盈止损单，请先核对，避免重复挂单。'))
    for order in drafts:
        kind=order['kind']
        side='buy' if kind=='limit_buy' else 'sell'
        line='建议预挂（尚未提交京东）：'+terms({**order,'action':side},multiline=True)
        lines.append(line)
        for review in value.get('limit_buy_reviews',[]):
            if review.get('checks') or review['decision']!='recommend' or not same_order(review,order):continue
            lines.append(f"买入后的参考目标价{review['target_price_cny']:g}元/克；风险退出参考价{review['risk_exit_cny']:g}元/克（不是已挂止损）。")
    if proposal_withdrawn:
        lines.append('本轮不再建议新挂上一轮条件单；已挂到京东的单以本轮挂单处理建议为准。')
    path_text=''
    if not partial:
        path_text=follow_up(value)
        if path_text:lines.append(path_text)
    if partial and not value.get('immediate_buy_resumed'):
        lines.append('仅推送已通过独立校验的部分；其余建议需核对，请查看账户页明细。')
    if not partial:
        explanation=basis(value)
        if explanation:
            lines.append('关键依据：\n'+'\n'.join('**'+item['label']+'**\n'+item['text'] for item in explanation))
    conditions=value.get('review_conditions',[])
    if conditions and not path_text:
        observations=[]
        for c in conditions:
            symbol={'at_or_above':'≥','at_or_below':'≤'}[c['condition']]
            scope='（仅针对指定挂单）' if c.get('order_key') else ''
            reason=brief(c.get('reason'),70)
            observations.append(f"银行报价 {symbol} {c['price_cny']:g}元/克{scope}"+('：'+reason if reason else ''))
        lines.append('什么时候重新分析：\n'+'\n'.join(observations)+'\n到价只触发重新分析，不执行交易；受交易时段、数据状态及本条建议有效期限制。')
    orders=account.get('pending_orders') or {}
    count=orders.get('total_count')
    complete=orders.get('complete')
    order_note=(f'当前有{count}笔待成交挂单' if complete and count is not None else
                f"已读取{len(orders.get('records',[]))}笔待成交明细，尚不能确认是否完整") if orders.get('status')=='fresh' else '条件单明细待更新'
    tracking=(current.get('execution_progress') or {}).get('status')
    progress={'pending':'发现对应或相近挂单，仍待成交','partially_filled':'对应或相近条件单已有部分成交','filled':'对应或相近条件单已成交'}.get(tracking)
    lines.append('委托查询：'+order_note+('；'+progress if progress else '')+'。')
    lines.append(f"时间与有效性：\n分析时间{time_text(current.get('created_at'))}\n"
                 f"建议有效至{time_text(current.get('expires_at'))}\n"
                 f"账户更新{time_text(account.get('fetched_at'))}\n"
                 f"报价更新{time_text((account.get('quote') or {}).get('quoted_at'))}\n建议 #{current['id']}（以上均为北京时间）")
    lines.append('真实账户建议，需本人手动确认；程序未自动下单。')
    bank=account['bank']
    lines.append(f'[查看完整分析与挂单明细](https://gold.goalgoal.fun/#strategies/live/{bank}/advice)')
    return '\n\n'.join(lines)
