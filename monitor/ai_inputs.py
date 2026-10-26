"""Compact redundant research text while preserving account values and trigger facts."""
from copy import deepcopy
import hashlib
import json


def news_key(row):
    return hashlib.sha256(json.dumps({k:row.get(k) for k in ('time_bj','text')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]



def market_context(context):
    """Select decision evidence without changing the monitoring snapshot or account ledger."""
    out=deepcopy(context)
    if out.get('trading_accounts'):
        signals=out.get('signals_tradingview')
        if isinstance(signals,dict):
            daily_weekly={'日线','周线','日','周','1d','1D','d','D','1w','1W','w','W'}
            out['signals_tradingview']={k:v for k,v in signals.items() if k in daily_weekly}
        out.pop('levels_by_code',None)
    jd=out.get('jd_research')
    if isinstance(jd,dict):
        jd.pop('quota',None)
        for kind in ('funds','orders'):
            entry=jd.get(kind)
            if isinstance(entry,dict):
                for key in ('attempted_at','refresh_status','retry_at','error_type','errors','error'):
                    entry.pop(key,None)
    return out

def compact_context(context):
    out=market_context(context)
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
