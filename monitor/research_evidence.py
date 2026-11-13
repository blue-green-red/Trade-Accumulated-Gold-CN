"""Bounded public news selection and literal retrieval reuse; no model opinions or networking."""
from datetime import datetime,timedelta,timezone
import ipaddress
import json
import re
from urllib.parse import urlsplit,urlunsplit

from .config import BJ

STATE='public_retrieval_evidence:v1'
TOPICS={
    'policy':r'美联储|央行|加息|降息|利率|鲍威尔|沃勒|哈玛克|哈马克|\bfed\b|hammack|waller|rate.hike|rate.cut',
    'data':r'CPI|PPI|PCE|非农|就业|通胀|inflation|payroll',
    'geopolitics':r'战争|停火|伊朗|俄乌|制裁|袭击|iran|war|sanction|ceasefire',
    'flows':r'ETF|央行购金|黄金储备|资金流|持仓|购金|gold.reserve',
}
VOICE=re.compile(r'哈玛克|哈马克|沃勒|鲍威尔|古尔斯比|鲍曼|戴利|洛根|威廉姆斯|hammack|waller|powell|goolsbee|bowman|daly|logan|williams',re.I)
ALIASES=dict(zip(('哈玛克','哈马克','沃勒','鲍威尔','古尔斯比','鲍曼','戴利','洛根','威廉姆斯'),
                 ('hammack','hammack','waller','powell','goolsbee','bowman','daly','logan','williams')))
MAX_NEWS=18
NEWS_CHARS=2400
INPUT_CHARS=2600


def stamp(value):
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        return t if isinstance(t,datetime) and t.tzinfo else None
    except (ValueError,TypeError):return None


def topics(text):return {k for k,p in TOPICS.items() if re.search(p,text,re.I)}


def selected_text(text,maximum=240):
    """Keep complete relevant clauses, including qualifications late in roundup articles."""
    text=str(text).strip()
    if len(text)<=maximum:return text,False
    parts=[' '.join(p.split()) for p in re.split(r'\n+|(?<=[。！？；;!?])\s*|(?<=[.!?])\s+|(?=\d+[.、]\s|[①-⑩])',text) if p.strip()]
    scored=sorted(range(len(parts)),key=lambda i:(bool(VOICE.search(parts[i])),
        bool(re.search(r'\bbut\b|however|gradual|smaller|\bnot\b|但是|不过|并不|渐进|更小',parts[i],re.I)),len(topics(parts[i])),-i),reverse=True)
    chosen=[];used=0
    for i in scored:
        extra=len(parts[i])+(3 if chosen else 0)
        if used+extra<=maximum:
            chosen.append(i);used+=extra
    if chosen and any(topics(parts[i]) for i in chosen):
        return ' … '.join(parts[i] for i in sorted(chosen)),True
    return text[:maximum],True


def select_news(flashes,as_of):
    at=stamp(as_of)
    if not at:return []
    eligible=[];seen=set()
    for row in sorted(flashes,key=lambda r:str(r.get('time_bj','')),reverse=True):
        try:when=datetime.strptime(row['time_bj'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ)
        except (KeyError,ValueError,TypeError):continue
        text=str(row.get('text') or '');key=''.join(text.split());tags=topics(text)
        age=(at-when).total_seconds()
        if not key or key in seen or age<0 or age>86400:continue
        if not row.get('important') and 'policy' not in tags and age>21600:continue
        seen.add(key);eligible.append((row,tags))
    chosen=[]
    # Reserve distinct policy voices and two observations for other major themes.
    voices=set()
    for i,(row,tags) in enumerate(eligible):
        voice=VOICE.search(row['text']);key=voice.group().lower() if voice else 'other'
        key=ALIASES.get(key,key)
        if 'policy' in tags and key not in voices and len(voices)<4:
            chosen.append(i);voices.add(key)
    for topic in ('data','geopolitics','flows'):
        have=sum(topic in eligible[i][1] for i in chosen)
        for i,(_,tags) in enumerate(eligible):
            if have>=2:break
            if topic in tags and i not in chosen:chosen.append(i);have+=1
    # Fill remaining slots with the newest relevant observations.
    chosen+= [i for i in range(len(eligible)) if i not in chosen][:MAX_NEWS-len(chosen)]
    result=[];size=0
    for i in chosen:
        row,_=eligible[i];text,truncated=selected_text(row['text'],min(240,NEWS_CHARS-size))
        if not text or size+len(text)>NEWS_CHARS:continue
        value={'time_bj':row['time_bj'],'important':bool(row.get('important')),'text':text}
        if truncated:value['text_truncated']=True
        result.append(value);size+=len(text)
        if size>=NEWS_CHARS:break
    return sorted(result,key=lambda r:r['time_bj'],reverse=True)


def public_url(value):
    """Only reusable public article links; no credentials, API endpoints or local addresses."""
    try:
        p=urlsplit(value)
        if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,80,443):return None
        host=p.hostname.lower()
        if '.' not in host or host.startswith('api.') or host.endswith(('.local','.internal')):return None
        try:
            if not ipaddress.ip_address(host).is_global:return None
        except ValueError:pass
        if p.query and not all(k.lower().startswith(('utm_','trk','ref','source')) for k in (q.split('=',1)[0] for q in p.query.split('&'))):return None
        if len(value)>1000 or re.search(r'[\x00-\x20\x7f]',value):return None
        return urlunsplit((p.scheme,p.netloc,p.path or '/','',''))
    except (ValueError,TypeError,AttributeError):return None


def extracted(kind,value):
    """Whitelist literal external results; never ingest model output, reasons or accounts."""
    if kind=='runtime_event':
        item=value.get('item') or {}
        if value.get('type')!='item.completed' or item.get('type')!='web_search':return []
        result=[]
        for r in item.get('results',[]):
            if not isinstance(r,dict):continue
            published=r.get('published_at')
            dated=re.match(r'Date:\s*(\w{3} \w{3} \d{1,2} \d{2}:\d{2}:\d{2} \d{4} UTC)',r.get('snippet') or '')
            if not published and dated:
                try:published=datetime.strptime(dated[1],'%a %b %d %H:%M:%S %Y UTC').replace(tzinfo=timezone.utc).isoformat()
                except ValueError:pass
            result.append({'url':r.get('url'),'title':r.get('title'),'text':r.get('snippet'),
                'published_at':published,'kind':'search_excerpt'})
        return result
    if kind=='public_page_fetch':
        r=value.get('result') or {}
        if r.get('error'):return []
        return [{'url':r.get('url'),'title':r.get('title'),'text':r.get('text'),
            'published_at':r.get('published_at'),'kind':'page_excerpt'}]
    return []


def build(conn,as_of,*,persist=True):
    """Reuse recent archived public retrievals across models without another network/model call."""
    from .analysis_archive import decode
    at=stamp(as_of)
    if not at:return {}
    state=conn.execute('SELECT value FROM state WHERE key=?',(STATE,)).fetchone()
    try:existing=json.loads(state[0]) if state else []
    except (ValueError,TypeError):existing=[]
    existing=[r for r in existing if isinstance(r,dict) and public_url(r.get('url')) and
        r.get('kind') in ('page_excerpt','search_excerpt') and isinstance(r.get('text'),str) and
        stamp(r.get('first_observed_at')) and stamp(r.get('last_observed_at'))] if isinstance(existing,list) else []
    pool={r['url']:r for r in existing if stamp(r.get('first_observed_at')) and stamp(r.get('last_observed_at')) and
          timedelta(0)<=at-stamp(r['first_observed_at'])<=timedelta(hours=48) and stamp(r['last_observed_at'])<=at}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'ai_analysis_archives','ai_analysis_artifacts'}<=tables:
        archives=conn.execute("SELECT id,view_id,run_id FROM ai_analysis_archives WHERE status='completed' "
            "AND julianday(completed_at)<=julianday(?) ORDER BY id DESC LIMIT 12",(at.isoformat(),)).fetchall()
        for aid,view_id,run_id in reversed(archives):
            cited=set()
            if view_id and 'ai_views' in tables:
                view=conn.execute('SELECT sources_json FROM ai_views WHERE rowid=? AND run_id=?',(view_id,run_id)).fetchone()
                if view:cited={public_url(s.get('url')) for s in json.loads(view[0] or '[]')}
            for observed,kind,payload in conn.execute("SELECT ts,kind,payload FROM ai_analysis_artifacts WHERE archive_id=? "
                    "AND kind IN ('runtime_event','public_page_fetch') ORDER BY id",(aid,)):
                when=stamp(observed)
                if not when or not timedelta(0)<=at-when<=timedelta(hours=48):continue
                for r in extracted(kind,decode(payload)):
                    url=public_url(r['url'])
                    if not url or not r.get('text'):continue
                    if stamp(r.get('published_at')) and stamp(r['published_at'])>at:continue
                    old=pool.get(url);text,_=selected_text(r['text'],700 if r['kind']=='page_excerpt' else 360)
                    # A later search snippet must not replace a fuller captured page.
                    if old and old['kind']=='page_excerpt' and r['kind']=='search_excerpt':
                        pool[url]={**old,'last_observed_at':observed,'cited':old.get('cited') or url in cited};continue
                    pool[url]={'url':url,'title':str(r.get('title') or '')[:160],'text':text,'kind':r['kind'],
                        'published_at':r.get('published_at') or (old or {}).get('published_at'),
                        'first_observed_at':min(observed,old['first_observed_at']) if old else observed,
                        'last_observed_at':observed,'cited':url in cited or bool((old or {}).get('cited'))}
    values=sorted(pool.values(),key=lambda r:(bool(r.get('cited')),r['last_observed_at'],r['kind']=='page_excerpt'),reverse=True)[:32]
    # Snapshot/replay readers must never erase entries observed after their historical as-of.
    future=[r for r in existing if stamp(r.get('last_observed_at')) and stamp(r['last_observed_at'])>at]
    retained={r['url']:r for r in future}
    for r in values:retained.setdefault(r['url'],r)
    saved=list(retained.values())[:32]
    if persist and saved!=existing:
        with conn:conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) '
            'DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(STATE,json.dumps(saved,ensure_ascii=False),at.isoformat()))
    result={'items':[],'scope':'公共检索摘录；收录时间不是事件时间，搜索摘要未经正文核对，不含账户建议'}
    for r in values:
        item={k:r[k] for k in ('url','title','text','kind','published_at','first_observed_at')}
        trial={**result,'items':result['items']+[item]}
        if len(json.dumps(trial,ensure_ascii=False,separators=(',',':')))>INPUT_CHARS:continue
        result=trial
        if len(result['items'])==4:break
    return result if result['items'] else {}
