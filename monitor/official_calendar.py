"""Cached Federal Reserve release schedules. No model, credentials or invented results."""
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
import re
import urllib.request
from zoneinfo import ZoneInfo

ET=ZoneInfo('America/New_York');BJ=ZoneInfo('Asia/Shanghai')
MONTHS=('','January','February','March','April','May','June','July','August','September','October','November','December')
KINDS=(('FOMC Minutes','FOMC Minutes'),('G.17 - Industrial Production','Industrial Production MoM'),('Beige Book','Fed Beige Book'))


def kind(title):
    text=(title or '').lower()
    if 'fomc' in text and 'minute' in text:return 'minutes'
    if 'industrial production' in text:return 'industrial'
    if 'beige book' in text:return 'beige'
    return None


class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__();self.depth=0;self.panel=None;self.field=None;self.row={};self.rows=[]
    def handle_starttag(self,tag,attrs):
        if tag!='div':return
        self.depth+=1;classes=dict(attrs).get('class','').split()
        if 'panel-body' in classes:self.panel=self.depth;self.row={}
        if self.panel:
            for c,k in (('col-xs-2','time'),('col-xs-7','title'),('col-xs-3','days')):
                if c in classes:self.field=(self.depth,k);self.row.setdefault(k,[])
    def handle_data(self,data):
        if self.field:self.row[self.field[1]].append(data)
    def handle_endtag(self,tag):
        if tag!='div':return
        if self.field and self.field[0]==self.depth:self.field=None
        if self.panel==self.depth:
            self.rows.append({k:' '.join(' '.join(v).split()) for k,v in self.row.items()});self.panel=None
        self.depth=max(0,self.depth-1)


def parse(html,year,month,url):
    parser=CalendarParser();parser.feed(html);events=[]
    for row in parser.rows:
        title=next((name for prefix,name in KINDS if row.get('title','').startswith(prefix)),None)
        match=re.fullmatch(r'(\d{1,2}):(\d{2})\s*([ap])\.?m\.?',row.get('time',''),re.I)
        if not title or not match:continue
        hour=int(match[1])%12+(12 if match[3].lower()=='p' else 0)
        for day in re.findall(r'\b\d{1,2}\b',row.get('days','')):
            try:at=datetime(year,month,int(day),hour,int(match[2]),tzinfo=ET).astimezone(timezone.utc)
            except ValueError:continue
            events.append({'time_utc':at.isoformat(),'time_bj':at.astimezone(BJ).strftime('%m-%d %H:%M'),
                'country':'US','title':title,'tier':2,'actual':None,'forecast':None,'previous':None,'released':False,
                'source':'Federal Reserve','source_url':url,'schedule_only':True})
    if not events:raise ValueError('Official calendar structure unavailable')
    return events


def _fetch(url):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=10) as response:
        return response.read(1000000).decode('utf-8')


def supplement(store,now):
    start=now-timedelta(days=1);end=now+timedelta(days=7);months={(start.year,start.month),(end.year,end.month),(now.year,now.month)}
    events=[];statuses=[]
    for year,month in sorted(months):
        key=f'official-calendar:fed:{year}-{month:02}';cached=store.get_state(key,{})
        def age(field):
            try:return (now-datetime.fromisoformat(cached[field])).total_seconds()
            except (KeyError,ValueError,TypeError):return float('inf')
        if age('fetched_at')>=21600 and age('checked_at')>=1800:
            cached['checked_at']=now.isoformat()
            url=f'https://www.federalreserve.gov/newsevents/{year}-{MONTHS[month]}.htm'
            try:
                cached.update(events=parse(_fetch(url),year,month,url),fetched_at=now.isoformat(),error=None)
            except Exception as exc:cached['error']=type(exc).__name__
            store.set_state(key,cached)
        valid=0<=age('fetched_at')<86400
        statuses.append({'month':f'{year}-{month:02}','status':'cached' if valid and cached.get('error') else 'available' if valid else 'unavailable',
                         'fetched_at':cached.get('fetched_at'),'error':cached.get('error')})
        if valid:
            for event in cached.get('events',[]):
                at=datetime.fromisoformat(event['time_utc'])
                if start<=at<=end:events.append({**event,'schedule_fetched_at':cached['fetched_at']})
    return events,statuses


def merge(primary,official):
    result=[dict(e) for e in primary]
    for event in official:
        matches=[e for e in result if kind(e.get('title'))==kind(event['title'])
                 and abs((datetime.fromisoformat(e['time_utc'])-datetime.fromisoformat(event['time_utc'])).total_seconds())<120]
        if matches:
            for e in matches:e.update(schedule_source=event['source'],schedule_url=event['source_url'])
        else:result.append(dict(event))
    return sorted(result,key=lambda e:e['time_utc'])
