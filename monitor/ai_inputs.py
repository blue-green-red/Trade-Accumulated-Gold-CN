"""Compact redundant research text while preserving account values and trigger facts."""
from copy import deepcopy
import hashlib
import json


def news_key(row):
    return hashlib.sha256(json.dumps({k:row.get(k) for k in ('time_bj','text')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]


def compact_context(context):
    out=deepcopy(context)
    covered_news={news_key(e['data']) for e in out.get('trigger_events',[]) if isinstance(e.get('data'),dict) and e['data'].get('text') and e['data'].get('time_bj')}
    seen=set();news=[]
    for row in out.get('jin10_flash_recent',[]):
        key=news_key(row)
        if key in seen or key in covered_news: continue
        seen.add(key);news.append(row)
    if 'jin10_flash_recent' in out: out['jin10_flash_recent']=news
    jd=out.get('jd_research')
    if isinstance(jd,dict): jd.pop('quota',None)  # operational counters are not market evidence
    macro=out.get('macro')
    if isinstance(macro,dict): macro.pop('errors',None)
    for account in (out.get('trading_accounts') or {}).get('accounts',{}).values():
        if 'recent_fills' in account:
            account['recent_fills']=account['recent_fills'][:3]
            for fill in account['recent_fills']:
                if fill.get('reason'): fill['reason']=fill['reason'][:120]
    return out
