"""Plain display text from saved advice; never changes decisions or calls a model."""
import re


def brief(text,limit=160):
    text=re.sub(r'(?<![A-Za-z])E(\d+(?:\.\d+)?)/T(\d+(?:\.\d+)?)/R(\d+(?:\.\d+)?)',
                lambda m:f'买入参考价{m[1]}元/克，目标价{m[2]}元/克，退出参考价{m[3]}元/克',str(text or ''))
    text=re.sub(r'([+]\d+(?:\.\d+)?)(元|%)?/(-\d+(?:\.\d+)?)(元|%)?',
                lambda m:f'{m[1]}{m[2] or m[4] or ""}（上涨情景）、{m[3]}{m[4] or m[2] or ""}（下跌情景）',text)
    text=re.sub(r'(?<![A-Za-z])[ETR](?![A-Za-z])',
                lambda m:{'E':'入场价','T':'目标价','R':'风险参考价'}[m[0]],str(text or ''))
    text=re.sub(r'(?<![A-Za-z])MA(20|50)(?!\d)',lambda m:m[1]+'日均线',text)
    text=re.sub(r'(比较(?:降至|减至)|考虑(?:降至|减至)|考虑是否减至|减仓(?:至|到)|(?:黄金)?(?:总)?仓位(?:降至|降到))(约?\s*\d+(?:\.\d+)?)%',
                lambda m:('重新分析是否把' if m[1].startswith(('比较','考虑')) else '把')+f'黄金仓位降到{m[2]}%（即保留{m[2]}%，不是卖出{m[2]}%）',text)
    for old,new in (('到边界或任一保护退出后','金价到退出参考价，或止盈止损单成交后'),
                    ('暂停旧追加方案','停止按旧方案继续买入'),
                    ('先核对成交、撤改旧买单并刷新账户再复评','先核对实际成交，撤销或修改未成交买单，更新账户后重新分析'),
                    ('独立京东委托不保证成交先后','京东各笔挂单独立执行，不能保证先买后卖或先卖后买'),
                    ('边界终止整套','到退出参考价时停止这套加仓计划'),('整套退出边界','停止加仓并重新考虑卖出的价格'),
                    ('先人工撤改追加','先撤销或修改尚未成交的买单'),('宏观未同步走强','美元和美债收益率没有同时上升'),
                    ('整仓损失','全部持仓可能亏损的金额'),('保护数量','止盈止损单的克数'),('本轮无已挂保护','目前没有已挂止盈止损单'),
                    ('实物承接','实物买盘支撑'),('有限修复','一定幅度的反弹'),('流动性压力','市场资金紧张'),
                    ('可比较补至','考虑是否加至'),('比较补至','考虑是否加至'),('比较降至','考虑是否减至'),('先刷新','先更新账户'),('再复评','再重新分析'),('刷新重评','更新账户后重新分析'),('自主风险边界','需要重新考虑退出的价格'),('总仓','黄金仓位'),('复评','重新分析'),('现仓','现有持仓'),
                    ('宏观同步走强','美元和美债收益率同时上升'),('全仓风险','全部持仓可能亏损的金额'),
                    ('整仓风险','全部持仓可能亏损的金额'),('本批','这批'),('后批','下一次买入'),
                    ('现卖净额','现在卖出并扣除手续费后的金额'),('整仓','全部黄金持仓'),
                    ('不追增','不追着上涨继续买'),('新快照','更新后的账户数据')):
        text=text.replace(old,new)
    text=' '.join(text.split())
    if len(text)<=limit:return text
    end=max(text.rfind(mark,0,limit) for mark in ('。','！','？','；'))
    return text[:end+1]+'…（详见账户页）' if end>=limit//2 else text[:limit]+'…（详见账户页）'



def full_text(text):
    # Explanations are displayed in full, not clipped to notification preview length.
    return brief(text, float('inf'))


def basis(value):
    reassessment=value.get('reassessment') or {}
    fields=(('买卖理由',reassessment.get('thesis')),
            ('数量与方案比较',value.get('reason')),
            ('目标价和退出价怎么选',reassessment.get('price_basis')),
            ('与上轮相比',reassessment.get('change_summary')))
    result=[];seen=set()
    for label,text in fields:
        text=full_text(text)
        if not text or text in seen:continue
        seen.add(text);result.append({'label':label,'text':text})
    return result


def plan_steps(path):
    if not path or path.get('checks'):return []
    text=full_text(path.get('continuation'))
    return [s.strip() for s in re.split(r'(?<=[。；])',text) if s.strip()]


def presentation(value):
    path=value.get('position_path') or {}
    return {'basis':basis(value),'plan_steps':plan_steps(path),'execution_note':full_text(path.get('execution_note'))}
