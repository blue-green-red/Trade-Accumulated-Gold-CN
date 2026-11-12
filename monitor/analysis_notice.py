"""Compact summaries of completed views; no new model calls or trading instructions."""
from .jd_live_notice import time_text
from .trade_costs import number

BIAS={'bullish':'偏多','bearish':'偏空','neutral':'中性'}


def compact(value,limit):
    text=' '.join(str(value or '').split())
    if len(text)<=limit:return text
    end=max(text.rfind(mark,0,limit) for mark in ('。','！','？','；'))
    return text[:end+1]+'…（详见分析页）' if end>=limit//2 else text[:limit]+'…（详见分析页）'


def probability_lines(view):
    lines=[]
    for suffix,horizon,threshold in (('3d','3个交易日','0.4%'),('3m','3个月','4%')):
        up,down=(number(view.get('p_'+side+'_'+suffix)) for side in ('up','down'))
        if up is None or down is None or up>1 or down>1 or up+down>1+1e-8:
            lines.append(horizon+'：概率待确认')
        else:lines.append(f'{horizon}：涨 {up:.0%} · 跌 {down:.0%}（幅度超{threshold}）')
    return '\n'.join(lines)+'\n国际现货金价的模型估计，其余为区间内波动。'


def fields(view,model_name):
    short=BIAS.get(view.get('bias_short'),'待确认');long=BIAS.get(view.get('bias_long'),'待确认')
    lines=[f'本轮结论：短期{short} · 中长期{long}。\n'+compact(view.get('summary'),180)]
    lines.append('概率判断：\n'+probability_lines(view))
    change=((view.get('public_content') or {}).get('analysis') or {}).get('change_summary')
    if change:lines.append('关键变化：'+compact(change,140))
    drivers=[]
    for d in (view.get('drivers') or [])[:3]:
        label=' · '.join(str(d[k]) for k in ('direction','factor') if d.get(k))
        evidence=compact(d.get('evidence'),140)
        drivers.append('• '+label+('：'+evidence if evidence else ''))
    if drivers:lines.append('关键依据：\n'+'\n'.join(drivers))
    if view.get('hard_risk_event'):
        # Never omit an explicitly flagged hard event to meet the ordinary summary length.
        lines.append('风险：'+str(view.get('hard_risk_note') or '本轮标记重大事件风险，详情请查看分析页。'))
    events=[]
    for item in (view.get('upcoming_events') or [])[:2]:
        events.append('• '+str(item.get('time_bj') or '时间待确认')+' '+str(item.get('name') or '事件待确认')+
                      ('\n'+compact(item['impact'],90) if item.get('impact') else ''))
    lines.append('下一次观察：\n'+('\n'.join(events) if events else '本轮未列出具名后续事件，关注新的行情与事件变化。'))
    guard=view.get('_price_guard') or {}
    if guard.get('invalidated_at'):lines.append('有效性提示：分析期间行情已明显变化，已排队复评；本条仅供了解原快照判断。')
    lines.append('时间与有效性：\n分析时间'+time_text(view.get('_completed_at') or view.get('_ts'))+
                 '\n行情快照'+time_text(view.get('_snapshot_at'))+'（北京时间）\n'
                 '有效性：本轮快照参考；行情或事件变化后需重评。')
    lines.append('[查看完整分析、概率与日程](https://gold.goalgoal.fun/#ai/research)')
    return {'title':f'📊{model_name}行情分析｜短期{short}','body':'\n\n'.join(lines)}
