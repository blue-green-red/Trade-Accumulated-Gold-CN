"""Small read-only collector. Browser cookies stay in RAM; credentials never enter SQLite or model input."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import signal
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


def refresh(store,client):
    for bank,(_,_,endpoint) in PRODUCTS.items():
        try:
            before=store.conn.execute('SELECT data_json FROM jd_live_snapshots WHERE bank=?',(bank,)).fetchone()
            data=parse_account(bank,client.query(endpoint,bank));coverage={}
            for operation in ('quote','recent_trade','pending_count','holding_details','closed_positions','cash_balance'):
                try:
                    raw=client.query(operation,bank)
                    if operation=='quote':data['quote']=parse_quote(raw,bank)
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
                except Exception as exc:coverage[operation]='unavailable:'+type(exc).__name__
            data['coverage']=coverage;record_success(store.conn,bank,data)
            previous=json.loads(before[0]) if before else {}
            fields=('total_grams','sellable_grams','frozen_grams','average_cost_cny','cash_cny')
            changed=any(previous.get(k)!=data.get(k) for k in fields)
            from .controls import MODELS,active
            from .ai_events import enqueue
            revision=store.conn.execute('SELECT revision FROM jd_live_snapshots WHERE bank=?',(bank,)).fetchone()[0]
            for model in MODELS:
                if not active(store.conn,model) or not store.get_state('ai_status:'+model,{}).get('available'):continue
                has_advice=store.conn.execute('SELECT 1 FROM jd_live_advice WHERE bank=? AND analyst=? LIMIT 1',(bank,model)).fetchone()
                if changed or not has_advice:
                    enqueue(store.conn,model,('真实账户持仓变化：' if before and changed else '真实账户首次分析：')+PRODUCTS[bank][1],0,
                        event_id='live-account:'+bank+':'+revision,payload={'bank':bank,'snapshot_revision':revision})
        except PermissionError:record_failure(store.conn,bank,'login_required')
        except Exception:record_failure(store.conn,bank,'unavailable')


def request_refresh(conn):
    from .jd_live_readonly import age
    now=datetime.now(timezone.utc)
    row=conn.execute("SELECT value FROM state WHERE key='jd_live_refresh'").fetchone()
    old=json.loads(row[0]) if row else {}
    elapsed=age(old.get('requested_at'),now)
    if elapsed<30 or (old.get('status') in ('queued','running') and elapsed<180):return old
    value={'requested_at':now.isoformat(),'status':'queued'}
    with conn:conn.execute("INSERT INTO state(key,value,updated_at) VALUES('jd_live_refresh',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(json.dumps(value),now.isoformat()))
    return value


def sync_once(store,client):
    if client.session is None:
        if browser_busy(store.conn):raise BrowserBusy('Model is running')
        client.recover()
    refresh(store,client)
    # An expired session is recovered at the next scheduled/manual refresh, never by sending SMS.
    if store.conn.execute("SELECT COUNT(*) FROM jd_live_readonly WHERE public_status='login_required'").fetchone()[0]:
        client.close();return 'login_required'
    if store.conn.execute("SELECT COUNT(*) FROM jd_live_readonly WHERE public_status='unavailable'").fetchone()[0]:return 'unavailable'
    return 'completed'


def run(once=False):
    from .config import Settings
    settings=Settings();store=Store(settings.db_path);ensure_schema(store.conn);client=ReadonlyClient()
    interval=max(60,float(os.environ.get('GOLD_JD_LIVE_REFRESH_SECONDS','900')))
    next_at=0;last_request=None;stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            request=store.get_state('jd_live_refresh',{});wanted=request.get('requested_at')
            manual=wanted and wanted!=last_request
            if time.monotonic()>=next_at or manual:
                status='completed'
                store.set_state('jd_live_sync_status',{'status':'running','at':datetime.now(timezone.utc).isoformat()})
                try:status=sync_once(store,client)
                except BrowserBusy:status='waiting_for_resources'
                except PermissionError:
                    status='login_required'
                    for bank in PRODUCTS:record_failure(store.conn,bank,'login_required')
                except Exception:
                    status='unavailable'
                    for bank in PRODUCTS:record_failure(store.conn,bank,'unavailable')
                last_request=wanted
                store.set_state('jd_live_sync_status',{'status':status,'at':datetime.now(timezone.utc).isoformat(),'interval_seconds':interval})
                if manual and store.get_state('jd_live_refresh',{}).get('requested_at')==wanted:
                    store.set_state('jd_live_refresh',{**request,'status':status,'finished_at':datetime.now(timezone.utc).isoformat()})
                next_at=time.monotonic()+(60 if status=='waiting_for_resources' else interval)
                if once:break
            if once:break
            time.sleep(5)
    finally:client.close();store.conn.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true');run(parser.parse_args().once)
