"""Read-only wave-trading evidence from existing prices and caches; no network calls."""
import math
from datetime import datetime, timedelta, time, timezone
from .trading_hours import BJ, trading_session


def stamp(value):
    at=value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('timestamp must include timezone')
    return at


def number(value,positive=False):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError):return None
    return value if math.isfinite(value) and (not positive or value>0) else None


def daily_indicators(snap,as_of):
    tv=snap.get('tv') or {}
    if not isinstance(tv,dict):tv={}
    result={'source':'TradingView OANDA:XAUUSD','status':'unavailable','fetched_at':tv.get('fetched_at'),
            'note':'日线指标随未结束交易日变化；均线为美元/盎司，RSI为0–100，均非京东技术位。'}
    try:age=(as_of-stamp(tv.get('fetched_at'))).total_seconds()
    except (ValueError,TypeError,AttributeError):return result
    if tv.get('status')!='ok' or not -30<=age<=180:return result
    rsi=number(tv.get('rsi_1d'));rsi=rsi if rsi is not None and 0<=rsi<=100 else None
    values={'rsi':rsi,'ma20_usd':number(tv.get('sma20_1d'),True),'ma50_usd':number(tv.get('sma50_1d'),True)}
    result.update(values)
    result['status']='fresh' if all(v is not None for v in values.values()) else 'partial' if any(v is not None for v in values.values()) else 'unavailable'
    return result


def daily_reference(conn,as_of):
    from .accounts import last_completed_ny_day
    result={'source':'GC=F期货日线代理，非京东历史成交价','status':'unavailable','recent_sessions':[],
            'note':'仅开盘与收盘序列，不含最高/最低/成交量；按纽约17:00过滤未结束日期，不虚构完整OHLC。'}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='daily_bars'").fetchone():return result
    rows=conn.execute('SELECT day,xau_open,xau_close FROM daily_bars WHERE day<=? ORDER BY day DESC LIMIT 61',
                     (last_completed_ny_day(as_of).isoformat(),)).fetchall()
    rows=[r for r in rows if number(r['xau_close'],True) is not None]
    if not rows:return result
    result.update(status='available',last_day=rows[0]['day'],last_close_usd=round(rows[0]['xau_close'],2),
        changes_pct={f'{n}d':round((rows[0]['xau_close']/rows[n]['xau_close']-1)*100,2) for n in (5,20,60) if len(rows)>n},
        recent_sessions=[{'date':r['day'],'open_usd':round(float(r['xau_open']),2) if number(r['xau_open'],True) is not None else None,
                          'close_usd':round(float(r['xau_close']),2)} for r in reversed(rows[:30])])
    return result


def calendar_window(events,as_of,limit=32):
    result=[]
    for event in events or []:
        if not isinstance(event,dict):continue
        try:at=stamp(event.get('time_utc'))
        except (ValueError,TypeError,AttributeError):continue
        if not as_of-timedelta(days=1)<=at<=as_of+timedelta(days=7):continue
        row=dict(event)
        if at>as_of:row.update(actual=None,released=False)
        result.append(row)
    result.sort(key=lambda e:(e.get('tier',3),stamp(e['time_utc'])<as_of,stamp(e['time_utc'])))
    selected=result[:limit]
    return {'days_ahead':7,'days_back':1,'total_in_window':len(result),'truncated':len(result)>limit,
            'events':sorted(selected,key=lambda e:stamp(e['time_utc']))}


def day_window(day):
    start=datetime.combine(day,time(),BJ);end=start+timedelta(days=1)
    if day.weekday()==0:start=start.replace(hour=9)
    elif day.weekday()==5:end=start.replace(hour=2,minute=30)
    elif day.weekday()==6:return None
    return start,end


def jd_daily_samples(conn,as_of):
    result={'source':'京东有效报价本地采样','timezone':'Asia/Shanghai','as_of':as_of.isoformat(),'status':'unavailable','days':[],
            'note':'北京时间自然日切分，首/末笔不是官方开/收盘；高低价仅为采样极值。缺口按分钟档估算，不等于接口故障，不补造缺失价格。'}
    rows=conn.execute('SELECT ts,jd_price FROM ticks WHERE julianday(ts)>=julianday(?) AND julianday(ts)<=julianday(?) AND jd_price>0 ORDER BY ts',
                     ((as_of-timedelta(days=30)).isoformat(),as_of.isoformat())).fetchall()
    groups={}
    for r in rows:
        try:at=stamp(r['ts']).astimezone(BJ)
        except (ValueError,TypeError,AttributeError):continue
        price=number(r['jd_price'],True)
        if price is None or not trading_session(at)['is_open']:continue
        groups.setdefault(at.date(),[]).append((at,price))
    dates=sorted(groups)[-10:]
    for day in dates:
        items=sorted(groups[day]);start,end=day_window(day);cutoff=min(as_of.astimezone(BJ),end)
        expected=max(0,int((cutoff-start).total_seconds()//60)+(1 if cutoff<end else 0))
        observed=len({at.replace(second=0,microsecond=0) for at,_ in items})
        gaps=[(b[0]-a[0]).total_seconds()/60 for a,b in zip(items,items[1:])]
        result['days'].append({'date_bj':day.isoformat(),'first_price_cny':items[0][1],'last_price_cny':items[-1][1],
            'sampled_low_cny':min(p for _,p in items),'sampled_high_cny':max(p for _,p in items),
            'samples':len(items),'first_sample_at':items[0][0].isoformat(),'last_sample_at':items[-1][0].isoformat(),
            'observed_minutes':observed,'expected_minutes':expected,'missing_minute_samples':max(0,expected-observed),
            'max_gap_minutes':round(max(gaps,default=0),1),'session_ended':as_of.astimezone(BJ)>=end,
            'partial':as_of.astimezone(BJ)<end or observed<expected})
    missing=[]
    if dates:
        day=dates[0];today=as_of.astimezone(BJ).date()
        while day<=today:
            window=day_window(day)
            if window and window[0]<as_of and day not in groups:missing.append(day.isoformat())
            day+=timedelta(days=1)
        result.update(status='available',missing_dates=missing)
    return result


def input_summary(context):
    if not ('daily_indicators' in context or 'jd_daily_samples' in context or 'recent_sessions' in (context.get('daily_trend_reference') or {})):return None
    r=context.get('daily_trend_reference') or {};j=context.get('jd_daily_samples') or {};m=context.get('macro') or {}
    return {'as_of':context.get('snapshot_at'),'daily_reference':{'status':r.get('status'),'last_day':r.get('last_day'),'sessions':len(r.get('recent_sessions') or [])},
            'daily_indicators':context.get('daily_indicators') or {'status':'unavailable'},
            'jd_daily':{'status':j.get('status'),'days':len(j.get('days') or []),'missing_dates':j.get('missing_dates') or []},
            'calendar':{'events':len(m.get('calendar') or []),'status':(m.get('status') or {}).get('calendar','unknown'),**(m.get('calendar_window') or {})}}
