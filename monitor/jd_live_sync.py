"""Small read-only collector. Browser cookies stay in RAM; credentials never enter SQLite or model input."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import socket
import subprocess
import time
import urllib.request
from zoneinfo import ZoneInfo

import requests
from .jd_live_readonly import PRODUCTS,ensure_schema,parse_account,record_success,record_failure,number
from .store import Store

ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'monitor/data/jd-remote-browser/chrome-profile'
QUERY_PATHS={
 'indexDataStd':'/gw2/generic/jrm/newh5/m/indexDataStd',
 'indexData':'/gw2/generic/jrm/newh5/m/indexData',
 'quote':'/gw2/generic/hj/h5/m/cfGetLatestPriceInfo',
 'recent_trade':'/gw2/generic/CfGoldWeb/newh5/m/cfQueryLastTrade',
 'pending_count':'/gw2/generic/hj/h5/m/cfQueryConditionWaittingNum',
 'pending_orders':'/gw2/generic/hj/h5/m/queryConditionalRuleList',
 'order_records':'/gw2/generic/jrm/h5/m/queryStdTradeListResourceNew',
}
CASH_PATHS={'zheshang':'/gw2/generic/jrm/h5/m/queryStdAssetsBalV2','minsheng':'/gw/generic/hj/h5/m/queryAccountAssetsV2'}
HISTORY_PATHS={
 'holding_details':{'zheshang':'/gw2/generic/hj/h5/m/queryStdSingleHoldingList','minsheng':'/gw2/generic/hj/h5/m/queryMsSingleHoldingList'},
 'closed_positions':{'zheshang':'/gw2/generic/hj/h5/m/queryStdLiquidationRecord','minsheng':'/gw2/generic/hj/h5/m/queryMsLiquidationRecord'},
}

class BrowserBusy(Exception):pass


def browser_busy(conn):
    for row in conn.execute("SELECT value FROM state WHERE key LIKE 'ai_runtime:%'"):
        value=json.loads(row[0])
        if value.get('running'):return True
    return False


async def load_cookies():
    import websockets
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));proc=None
    def version(port):
        with opener.open(f'http://127.0.0.1:{port}/json/version',timeout=.5) as r:return json.load(r)
    try:
        try:info=version(9225) # An active, dedicated interactive login browser.
        except OSError:
            if not PROFILE.exists():raise PermissionError('JD profile missing')
            if (PROFILE/'SingletonLock').is_symlink():
                try:
                    pid=int(os.readlink(PROFILE/'SingletonLock').rsplit('-',1)[1]);os.kill(pid,0)
                except (OSError,ValueError):pass
                else:raise BrowserBusy('Dedicated profile in use')
            with socket.socket() as sock:sock.bind(('127.0.0.1',9226))
            env=dict(os.environ)
            # The browser needs neither model/API credentials nor proxies.
            for key in list(env):
                if any(x in key.upper() for x in ('PROXY','TOKEN','PASSWORD','SECRET','API_KEY')):env.pop(key,None)
            proc=subprocess.Popen(['/usr/bin/google-chrome','--headless=new','--no-sandbox','--disable-dev-shm-usage',
                '--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update',
                '--disable-sync','--disable-gpu','--enable-low-end-device-mode','--renderer-process-limit=1',
                '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9226','--user-data-dir='+str(PROFILE),'about:blank'],
                env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            for _ in range(100):
                if proc.poll() is not None:raise BrowserBusy('Dedicated profile unavailable')
                try:info=version(9226);break
                except OSError:await asyncio.sleep(.25)
            else:raise TimeoutError('Browser startup timeout')
        async with websockets.connect(info['webSocketDebuggerUrl'],max_size=4194304,proxy=None) as ws:
            await ws.send(json.dumps({'id':1,'method':'Storage.getCookies'}))
            while True:
                reply=json.loads(await asyncio.wait_for(ws.recv(),15))
                if reply.get('id')==1:break
            if 'error' in reply:raise PermissionError('JD session unavailable')
            return [c for c in reply['result']['cookies'] if c['domain'].lstrip('.') in ('jdjygold.com','api.jdjygold.com','m.jdjygold.com','jd.com','ms.jr.jd.com')]
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)


class ReadonlyClient:
    def __init__(self):self.session=None
    def close(self):
        if self.session:self.session.close()
        self.session=None
    def recover(self):
        self.close();cookies=asyncio.run(load_cookies())
        if not cookies:raise PermissionError('JD session expired')
        self.session=requests.Session();self.session.trust_env=False
        for c in cookies:self.session.cookies.set(c['name'],c['value'],domain=c['domain'],path=c.get('path','/'),secure=c.get('secure',True))
    def query(self,operation,bank):
        # Callers choose a fixed query operation, never a URL supplied by a user/model.
        sku=PRODUCTS[bank][0];params={'productSku':sku}
        if operation=='order_records':params.update(pageIndex=1,pageSize=20,tradeType='0',notNeedSku=True,startTime=None,endTime=None,lastDataTime=None)
        if operation=='cash_balance':
            params['operationType']=0
        if operation in HISTORY_PATHS:
            url='https://ms.jr.jd.com'+HISTORY_PATHS[operation][bank]
            params.update(sortOrder='0',sortType='0',pageIndex=1,pageSize=20,queryTime='')
            referer='https://jdjr.jd.com/'
        else:url='https://api.jdjygold.com'+(CASH_PATHS[bank] if operation=='cash_balance' else QUERY_PATHS[operation]);referer='https://m.jdjygold.com/'
        response=self.session.post(url,
            headers={'Referer':referer,'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'},
            data={'reqData':json.dumps(params,separators=(',',':'))},timeout=(8,15),allow_redirects=False)
        if response.status_code in (401,403):raise PermissionError('JD session expired')
        response.raise_for_status();payload=response.json()
        if payload.get('success') is not True or str(payload.get('resultCode'))!='0':raise PermissionError('JD query rejected')
        return payload


def component(payload):
    result=payload.get('resultData') or {}
    code=str(result.get('code',''))
    if not code or set(code)!={'0'}:raise ValueError('JD component query rejected')
    return result.get('data')


def vendor_time(stamp):
    try:
        numeric=float(stamp)
        return datetime.fromtimestamp(numeric/(1000 if numeric>1e11 else 1),timezone.utc).isoformat()
    except (ValueError,TypeError,OverflowError,OSError):
        try:
            dt=datetime.fromisoformat(str(stamp))
            return (dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo('Asia/Shanghai'))).astimezone(timezone.utc).isoformat()
        except ValueError:return None


def parse_quote(payload,bank):
    data=component(payload)
    if not isinstance(data,dict) or str(data.get('productSku'))!=PRODUCTS[bank][0]:raise ValueError('Quote bank mismatch')
    buy,sell=(number(data[k],True) for k in ('buyPrice','sellPrice'))
    if min(buy,sell)<=0:raise ValueError('Empty bank quote')
    return {'buy_price_cny':buy,'sell_price_cny':sell,'quoted_at':vendor_time(data.get('time'))}


def parse_history(payload,operation):
    result=payload.get('resultData') or {};data=result.get('datas')
    if not isinstance(data,dict):raise ValueError('History data missing')
    key='holdingList' if operation=='holding_details' else 'liquidationList'
    rows=data.get(key)
    if not isinstance(rows,list):raise ValueError('History list missing')
    numeric=('buyPrice','orderGram','totalGram','availableGram','frozenGram','cumulativePAndL','sellAvgPrice','fee')
    output=[]
    for row in rows[:20]:
        if not isinstance(row,dict):raise ValueError('History row invalid')
        item={k:number(row[k]) for k in numeric if row.get(k) is not None}
        stamp=row.get('createTime' if operation=='holding_details' else 'liquidationTime')
        item['time']=vendor_time(stamp)
        output.append(item)
    return {'records':output,'total_count':int(number(data['totalCount'],True)) if data.get('totalCount') is not None else None,
            'limit':20,'scope':'first_page_not_complete_execution_history'}


def parse_cash(payload,bank):
    result=payload.get('resultData') or {};data=result.get('datas') or {}
    key='availBalance' if bank=='zheshang' else 'availableBalance'
    success=str(result.get('errorCode'))=='00000' if bank=='zheshang' else data.get('code')=='DS00000'
    if not success or key not in data:raise ValueError('Cash-balance query rejected')
    total=data.get('balance' if bank=='zheshang' else 'totalBalance')
    return {'cash_cny':number(data[key],True),'cash_balance_cny':number(total,True) if total is not None else None,
            'cash_source':'jd_electronic_account_api','cash_scope':'bank_gold_electronic_account_not_linked_bank_card'}


def refresh(store,client,groups=None,now=None):
    groups=set(groups or ('core','quote','orders','history'))
    observed_now=lambda:now or datetime.now(timezone.utc)
    for bank,(_,_,endpoint) in PRODUCTS.items():
        try:
            before=store.conn.execute('SELECT data_json,fetched_at FROM jd_live_snapshots WHERE bank=?',(bank,)).fetchone()
            previous=json.loads(before[0]) if before else {}
            data=dict(previous);coverage=dict(data.get('coverage',{}));times=dict(data.get('component_times',{}))
            errors=dict(data.get('component_errors',{}))
            current=set(groups)
            if not before:current.update(('core','quote','orders','history'))
            if 'core' in current:
                core=parse_account(bank,client.query(endpoint,bank))
                data.update({k:v for k,v in core.items() if k not in ('quote','recent_trade','pending_order_count','orders')})
                times['account']=observed_now().isoformat()
                if any(previous.get(k)!=data.get(k) for k in ('total_grams','sellable_grams','frozen_grams','average_cost_cny')):current.add('history')
            operations=[]
            if 'quote' in current:operations.append('quote')
            if 'core' in current:operations.extend(('recent_trade','cash_balance'))
            if 'orders' in current:operations.extend(('pending_orders','order_records'))
            if 'history' in current:operations.extend(('holding_details','closed_positions'))
            for operation in operations:
                try:
                    raw=client.query(operation,bank)
                    at=observed_now().isoformat()
                    if operation=='quote':data['quote']=parse_quote(raw,bank)
                    elif operation=='pending_orders':
                        from .jd_live_orders import parse_list,record,history
                        pending,_=parse_list(raw,bank)
                        seen={r['order_key'] for r in pending['records']}
                        updates=list(pending['records'])
                        if pending['complete']:
                            for old in history(store.conn,bank,100):
                                if old['order_key'] not in seen and old['status'] in ('pending','partially_filled'):
                                    updates.append({**old,'status':'unknown'})
                        record(store.conn,bank,updates,at)
                        data['pending_orders']=pending
                    elif operation=='order_records':
                        from .jd_live_orders import trade_history,record,history
                        old={o['order_key']:o for o in history(store.conn,bank,100)}
                        records=[{**old.get(o['order_key'],{}),**o} for o in trade_history(raw,bank)]
                        partial={o['order_key']:o for o in records if o['status']=='partially_filled'}
                        record(store.conn,bank,records,at)
                        if data.get('pending_orders'):
                            data['pending_orders']={**data['pending_orders'],'records':[
                                {**order,**({'status':'partially_filled','filled_grams':partial[order['order_key']].get('filled_grams')} if order['order_key'] in partial else {})}
                                for order in data['pending_orders'].get('records',[])]}
                        data['order_outcomes']=[{k:o.get(k) for k in ('order_key','status','filled_grams')} for o in records]
                    elif operation=='cash_balance':data.update(parse_cash(raw,bank))
                    elif operation in HISTORY_PATHS:data[operation]=parse_history(raw,operation)
                    elif operation=='pending_count':
                        value=component(raw)['waittingConditionalNum'];n=number(value,True)
                        if n!=int(n):raise ValueError('Invalid order count')
                        data['pending_order_count']=int(n)
                    else:
                        value=component(raw)
                        if not value:data['recent_trade']=None
                        elif isinstance(value,dict):
                            data['recent_trade']={k:value.get(k) for k in ('transactionTime','buyTypeStr','lastTradeType','orderStatus','orderGram','orderAmount')}
                            data['recent_trade']['transactionTime']=vendor_time(value.get('transactionTime'))
                            labels={'buy':'买入','sell':'卖出','BUY':'买入','SELL':'卖出','买入':'买入','卖出':'卖出'}
                            data['recent_trade']['trade_label']=next((labels[value[k]] for k in ('transactionType','lastTradeType','buyTypeStr') if value.get(k) in labels),None)
                        else:raise ValueError('Invalid recent trade')
                    coverage[operation]='available'
                    errors.pop(operation,None)
                    times[operation]=observed_now().isoformat()
                except Exception as exc:
                    coverage[operation]='unavailable:'+type(exc).__name__
                    errors[operation]={'type':type(exc).__name__,'code':getattr(exc,'sqlite_errorname',None),'at':observed_now().isoformat()}
                    if operation=='pending_orders' and not isinstance(exc,sqlite3.Error):
                        try:
                            value=component(client.query('pending_count',bank))['waittingConditionalNum'];n=number(value,True)
                            if n!=int(n):raise ValueError('Invalid order count')
                            data['pending_order_count']=int(n);coverage['pending_count']='available';times['pending_count']=observed_now().isoformat()
                        except Exception as fallback:coverage['pending_count']='unavailable:'+type(fallback).__name__
            if coverage.get('pending_orders')=='available':
                data['pending_order_count']=data['pending_orders']['total_count']
            data.update(coverage=coverage,component_times=times,component_errors=errors)
            meta=store.conn.execute('SELECT public_status,checked_at FROM jd_live_readonly WHERE bank=?',(bank,)).fetchone()
            record_success(store.conn,bank,data,times['account'] if 'core' in current else before[1])
            if 'core' not in current and meta and meta[0]!='fresh':record_failure(store.conn,bank,meta[0],meta[1])
        except PermissionError:record_failure(store.conn,bank,'login_required')
        except Exception:record_failure(store.conn,bank,'unavailable')
    from .jd_live_review import request_reviews
    request_reviews(store,observed_now(),after_sync=True)


def request_refresh(conn,components=None,source='manual'):
    from .jd_live_readonly import age
    all_components={'core','quote','orders','history'}
    wanted=set(components) if components is not None else all_components
    if not wanted or not wanted<=all_components:raise ValueError('Invalid refresh components')
    now=datetime.now(timezone.utc)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("SELECT value FROM state WHERE key='jd_live_refresh'").fetchone()
        old=json.loads(row[0]) if row else {}
        elapsed=age(old.get('requested_at'),now)
        coalesced=elapsed<30 or (old.get('status') in ('queued','running') and elapsed<180)
        if coalesced:
            old_components=set(old.get('components') or all_components)
            if old.get('status') not in ('queued','running') or wanted<=old_components:return old
            # Preserve a full manual refresh if it supersedes an analysis-only request.
            wanted|=old_components
        value={'requested_at':now.isoformat(),'status':'queued','source':source}
        if wanted!=all_components:value['components']=sorted(wanted)
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('jd_live_refresh',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(json.dumps(value),now.isoformat()))
    return value


def sync_once(store,client,groups=None):
    if client.session is None:
        if browser_busy(store.conn):raise BrowserBusy('Model is running')
        from .runtime_resources import slot
        database=Path(store.conn.execute('PRAGMA database_list').fetchone()[2])
        with slot(database) as acquired:
            if not acquired:raise BrowserBusy('Model resource slot in use')
            client.recover()
    refresh(store,client,groups)
    # An expired session is recovered at the next scheduled/manual refresh, never by sending SMS.
    if store.conn.execute("SELECT COUNT(*) FROM jd_live_readonly WHERE public_status='login_required'").fetchone()[0]:
        client.close();return 'login_required'
    if store.conn.execute("SELECT COUNT(*) FROM jd_live_readonly WHERE public_status='unavailable'").fetchone()[0]:return 'unavailable'
    return 'completed'


def run(once=False):
    from .config import Settings
    settings=Settings();store=Store(settings.db_path);ensure_schema(store.conn);client=ReadonlyClient()
    interval=max(60,float(os.environ.get('GOLD_JD_LIVE_REFRESH_SECONDS','300')))
    intervals={'quote':max(60,float(os.environ.get('GOLD_JD_LIVE_QUOTE_SECONDS','60'))),
               'core':interval,'orders':interval,'history':max(interval,float(os.environ.get('GOLD_JD_LIVE_HISTORY_SECONDS','1800')))}
    due_at={k:0 for k in intervals};last_request=None;stopping=False;failures=0;was_open=None
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            request=store.get_state('jd_live_refresh',{});wanted=request.get('requested_at')
            manual=wanted and wanted!=last_request
            groups=set(request.get('components') or intervals) if manual else {k for k in intervals if time.monotonic()>=due_at[k]}
            from .trading_hours import trading_session
            opened=trading_session()['is_open']
            if opened and was_open is False:groups.update(('core','orders','quote'))
            was_open=opened
            if not opened and not manual:
                groups.discard('quote')
                if 'core' in groups:groups.update(('orders',))
            if groups:
                status='completed'
                store.set_state('jd_live_sync_status',{'status':'running','at':datetime.now(timezone.utc).isoformat(),'interval_seconds':interval,'intervals':intervals,'components':sorted(groups)})
                try:status=sync_once(store,client,groups)
                except BrowserBusy:status='waiting_for_resources'
                except PermissionError:
                    status='login_required'
                    for bank in PRODUCTS:record_failure(store.conn,bank,'login_required')
                except Exception:
                    status='unavailable'
                    for bank in PRODUCTS:record_failure(store.conn,bank,'unavailable')
                last_request=wanted
                failures=failures+1 if status in ('login_required','unavailable') else 0
                store.set_state('jd_live_sync_status',{'status':status,'at':datetime.now(timezone.utc).isoformat(),'interval_seconds':interval,'intervals':intervals,'components':sorted(groups)})
                if manual and store.get_state('jd_live_refresh',{}).get('requested_at')==wanted:
                    store.set_state('jd_live_refresh',{**request,'status':status,'finished_at':datetime.now(timezone.utc).isoformat()})
                for kind in groups:due_at[kind]=time.monotonic()+(60 if status=='waiting_for_resources' else intervals[kind] if trading_session()['is_open'] or manual else max(1800,intervals[kind]))
                if failures:
                    retry_delay=min(1800,300*2**min(failures-1,3))
                    due_at={k:max(t,time.monotonic()+retry_delay) for k,t in due_at.items()}
                if once:break
            if once:break
            time.sleep(5)
    finally:client.close();store.conn.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true');run(parser.parse_args().once)
