"""Shared, self-contained message cards for analysis and real-account WeChat details."""
from html import escape
import re


def inline(text):
    """Only render the bold text and HTTP(S) links emitted by our notice formatter."""
    result=[];last=0
    pattern=r'\*\*([^\n]+?)\*\*|\[([^\n\]]+)\]\((https?://[^\s)]+)\)'
    for match in re.finditer(pattern,text):
        result.append(escape(text[last:match.start()]))
        if match[1] is not None:result.append('<strong>'+escape(match[1])+'</strong>')
        else:result.append('<a style="color:#24547a;text-decoration:underline" href="'+escape(match[3],quote=True)+'">'+escape(match[2])+'</a>')
        last=match.end()
    result.append(escape(text[last:]))
    return ''.join(result).replace('\n','<br>')


def section(paragraph):
    plain=paragraph[2:-2] if paragraph.startswith('**') and paragraph.endswith('**') else paragraph
    label,colon,content=plain.partition('：')
    known={'本轮结论','需要操作','提醒原因','低位买入','价格复评','简要依据','风险','委托查询','新增买卖建议','已有挂单',
           '建仓路径','概率判断','关键变化','关键依据','下一次观察','当前保护查询','盈亏测算','时间与有效性','有效性提示'}
    if not colon or label not in known:
        label=next((name for prefix,name in (('建议撤单','挂单处理'),('建议改单','挂单处理'),
                   ('建议预挂','条件单建议'),('有效至','有效期与数据时间')) if plain.startswith(prefix)),None)
        content=paragraph
    if label:
        background='#fff7e6' if label=='本轮结论' else '#f3f6fa' if label=='需要操作' else '#ffffff'
        return ('<section style="margin:0 0 16px;padding:12px 14px;border:1px solid #e5e9ee;'
                'border-radius:10px;background:'+background+'">'
                '<h2 style="font-size:15px;line-height:1.5;margin:0 0 7px;font-weight:700;color:#25364a">'+escape(label)+'</h2>'
                '<p style="margin:0;line-height:1.85">'+inline(content.strip())+'</p></section>')
    return '<p style="margin:0 0 16px;line-height:1.85">'+inline(paragraph)+'</p>'


def message(title,events):
    """One visible title for one event; bank headings remain distinct in a merged notice."""
    display_title=events[0]['title'] if len(events)==1 else title
    parts=['<article style="max-width:640px;margin:0 auto;padding:20px 16px;box-sizing:border-box;'
           'font-family:system-ui,-apple-system,sans-serif;font-size:15px;color:#263445;overflow-wrap:anywhere">',
           '<header><h1 style="margin:0 0 16px;font-size:24px;line-height:1.5;font-weight:700;color:#172b43">'+escape(display_title)+'</h1></header>',
           '<hr style="border:0;border-top:2px solid #c89b45;margin:0 0 20px"><main>']
    for i,event in enumerate(events):
        if len(events)>1:
            if i:parts.append('<hr style="border:0;border-top:1px solid #d8dfe7;margin:24px 0">')
            parts.append('<h2 style="font-size:20px;line-height:1.5;margin:0 0 16px">'+escape(event['title'])+'</h2>')
        paragraphs=[p.strip() for p in re.split(r'\n\s*\n',event['body']) if p.strip()]
        if event.get('notification_topic','').endswith(':analysis'):
            while paragraphs and re.fullmatch(r'#{1,6}\s+(?:Codex|Claude|AI)',paragraphs[0],re.IGNORECASE):paragraphs.pop(0)
        parts.extend(section(p) for p in paragraphs)
    parts.append('</main></article>')
    return ''.join(parts)


# Keep the existing renderer import compatible for older integrations.
real_advice=message
