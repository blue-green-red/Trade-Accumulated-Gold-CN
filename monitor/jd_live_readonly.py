"""Verified real-account snapshots. Independent of every simulation and execution path."""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from .jd_readonly_probe import BANKS

PRODUCTS={'zheshang':('1961543816','浙商积存金','indexDataStd'),'minsheng':('21001001000001','民生积存金','indexData')}
STALE_SECONDS=1800

def risk_profile():
    from .risk_profiles import PROFILES
    key=os.environ.get('GOLD_JD_LIVE_RISK_PROFILE','high')
    if key not in PROFILES:key='high'
    return {'key':key,**PROFILES[key],'enforcement':'advice_reference_only',
            'risk_budget_cny':None,'historical_drawdown_pct':None,
            'note':'金额预算取决于各户现金来源与有效报价；历史净值不完整，不能确认历史最大回撤，成本浮亏不等于峰值回撤。'}
SCHEMA='''CREATE TABLE IF NOT EXISTS jd_live_readonly (
 bank TEXT PRIMARY KEY, checked_at TEXT, public_status TEXT NOT NULL DEFAULT 'not_checked'
);
CREATE TABLE IF NOT EXISTS jd_live_snapshots (
 bank TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, revision TEXT NOT NULL, data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jd_live_advice (
 id INTEGER PRIMARY KEY, bank TEXT NOT NULL, analyst TEXT NOT NULL, view_id INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, snapshot_revision TEXT NOT NULL,
 advice_json TEXT NOT NULL, UNIQUE(bank,analyst,view_id)
);
CREATE TABLE IF NOT EXISTS jd_live_cash_declarations (
 bank TEXT PRIMARY KEY, cash_cny REAL NOT NULL, declared_at TEXT NOT NULL,
 anchor TEXT NOT NULL, invalidated_at TEXT
);'''


def ensure_schema(conn):
    with conn:
        conn.executescript(SCHEMA)
        for bank in BANKS:conn.execute('INSERT OR IGNORE INTO jd_live_readonly(bank) VALUES(?)',(bank,))


def number(value,nonnegative=False):
    if isinstance(value,bool):raise ValueError('Invalid numeric field')
    result=float(value)
    if not math.isfinite(result) or (nonnegative and result<0):raise ValueError('Invalid numeric field')
    return result


def cash_anchor(data):
    fields=('product_sku','total_grams','average_cost_cny','recent_trade')
    return hashlib.sha256(json.dumps({k:data.get(k) for k in fields},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]


def declare_full_position(conn,bank,at=None):
    if bank not in PRODUCTS:raise ValueError('Unknown bank')
    row=conn.execute('SELECT data_json FROM jd_live_snapshots WHERE bank=?',(bank,)).fetchone()
    if not row:raise ValueError('Verified account snapshot required')
    at=at or datetime.now(timezone.utc).isoformat()
    data=json.loads(row[0]);invalidated=at if data.get('cash_cny',0) and data.get('cash_source')=='jd_electronic_account_api' else None
    with conn:conn.execute('INSERT INTO jd_live_cash_declarations VALUES(?,0,?,?,?) ON CONFLICT(bank) DO UPDATE SET cash_cny=0,declared_at=excluded.declared_at,anchor=excluded.anchor,invalidated_at=excluded.invalidated_at',
                           (bank,at,cash_anchor(data),invalidated))


def parse_account(bank,payload):
    sku,name,_=PRODUCTS[bank]
    result=payload.get('resultData') or {};data=result.get('datas') or {}
    if payload.get('success') is not True or str(payload.get('resultCode'))!='0' or data.get('login') is not True:
        raise PermissionError('JD login required or account query rejected')
    product=data.get('goldProductInfo') or {};stock=data.get('goldStockStat') or {}
    if str(product.get('productSku'))!=sku or str(stock.get('productSku'))!=sku or product.get('productName')!=name:
        raise ValueError('Bank product identity mismatch')
    total,sellable,frozen=(number(stock[k],True) for k in ('totalGram','availableGram','frozenGram'))
    if sellable+frozen>total+0.0001:raise ValueError('Inconsistent holdings')
    optional=lambda field: number(stock[field]) if stock.get(field) is not None else None
    return {'product_sku':sku,'total_grams':total,'sellable_grams':sellable,'frozen_grams':frozen,
            # The official H5 cost and floating-P/L components use newAvgPrice.
            'average_cost_cny':optional('newAvgPrice') if stock.get('newAvgPrice') is not None else optional('avgCostPrice'),
            'holding_pnl_cny':None,
            'cash_cny':None,'orders':None,'quote':None,'recent_trade':None,'pending_order_count':None,
            'sell_fee_rate':number(product['sellRate'],True)/100 if product.get('sellRate') is not None else None}


def record_success(conn,bank,data,at=None):
    at=at or datetime.now(timezone.utc).isoformat()
    # Quotes and poll timestamps change without the account itself changing.
    identity={k:data.get(k) for k in ('product_sku','total_grams','sellable_grams','frozen_grams','average_cost_cny','pending_order_count','recent_trade','cash_cny')}
    revision=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    with conn:
        conn.execute('INSERT INTO jd_live_snapshots VALUES(?,?,?,?) ON CONFLICT(bank) DO UPDATE SET fetched_at=excluded.fetched_at,revision=excluded.revision,data_json=excluded.data_json',
                     (bank,at,revision,json.dumps(data,ensure_ascii=False)))
        conn.execute("UPDATE jd_live_readonly SET checked_at=?,public_status='fresh' WHERE bank=?",(at,bank))
        conn.execute('UPDATE jd_live_cash_declarations SET invalidated_at=? WHERE bank=? AND anchor!=? AND invalidated_at IS NULL',(at,bank,cash_anchor(data)))
        if data.get('cash_source')=='jd_electronic_account_api' and (data.get('cash_cny') or 0)>0:
            conn.execute('UPDATE jd_live_cash_declarations SET invalidated_at=? WHERE bank=? AND invalidated_at IS NULL',(at,bank))


def record_failure(conn,bank,status,at=None):
    with conn:conn.execute('UPDATE jd_live_readonly SET checked_at=?,public_status=? WHERE bank=?',
                          (at or datetime.now(timezone.utc).isoformat(),status,bank))


def age(stamp,now):
    try:return (now-datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError,ValueError):return float('inf')


def accounts(conn,now=None,include_advice=True):
    now=now or datetime.now(timezone.utc)
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    records={r[0]:{'checked_at':r[1],'public_status':r[2]} for r in conn.execute('SELECT bank,checked_at,public_status FROM jd_live_readonly')} if 'jd_live_readonly' in tables else {}
    snapshots={r[0]:{'fetched_at':r[1],'revision':r[2],**json.loads(r[3])} for r in conn.execute('SELECT * FROM jd_live_snapshots')} if 'jd_live_snapshots' in tables else {}
    declarations={r[0]:r for r in conn.execute('SELECT bank,cash_cny,declared_at,anchor,invalidated_at FROM jd_live_cash_declarations')} if 'jd_live_cash_declarations' in tables else {}
    out=[]
    for bank,info in BANKS.items():
        meta=records.get(bank,{'checked_at':None,'public_status':'not_checked'});snap=snapshots.get(bank,{})
        fresh=bool(snap) and meta['public_status']=='fresh' and 0<=age(snap.get('fetched_at'),now)<=STALE_SECONDS
        status='fresh' if fresh else 'stale' if meta['public_status']=='fresh' and snap else meta['public_status']
        if status in ('not_checked','reachable'):status='login_required'
        row={'bank':bank,'name':info['name'],'official_url':info['url'],**meta,
             'cash_cny':None,'total_grams':None,'sellable_grams':None,'frozen_grams':None,'average_cost_cny':None,
             'orders':None,'recent_trade':None,'pending_order_count':None,**snap,'status':status,'connected':fresh,
             'order_submission_enabled':False,'data_age_seconds':round(age(snap.get('fetched_at'),now)) if snap else None,
             'message':'账户数据已同步；资金余额与完整委托列表仍未知。' if fresh else '数据待刷新，已有数值仅为上次成功快照。' if snap else '尚无可用账户快照；需要有效的京东登录会话。'}
        if row.get('quote'):
            q=row['quote'];q_age=age(q.get('quoted_at'),now)
            q['status']='fresh' if fresh and -30<=q_age<=180 else 'historical'
        row['holding_pnl_cny']=None
        if row.get('quote') and row.get('average_cost_cny') is not None and row.get('total_grams') is not None:
            row['holding_pnl_cny']=round((row['quote']['sell_price_cny']-row['average_cost_cny'])*row['total_grams'],2)
        row['holding_pnl_basis']='bank_sell_quote_minus_average_cost_before_sell_fee'
        row['advice']=[]
        row['risk_profile']=risk_profile()
        declaration=declarations.get(bank)
        row.update(cash_source=snap.get('cash_source','unknown'),cash_declared_at=None,position_pct=None)
        api_cash=row['cash_source']=='jd_electronic_account_api' and row.get('cash_cny') is not None
        if declaration and not api_cash:
            row['cash_declared_at']=declaration[2]
            valid=bool(snap) and not declaration[4] and declaration[3]==cash_anchor(snap)
            row['cash_cny']=declaration[1] if valid else None
            row['cash_source']='user_declared_full_position' if valid else 'user_declaration_invalidated'
            if valid and row.get('total_grams',0)>0:row['position_pct']=100
            if valid:row['message']='现金0为用户确认当前满仓的声明；完整委托明细仍未知。'
            row['revision']=hashlib.sha256((snap.get('revision','')+str(declaration[2])+str(valid)).encode()).hexdigest()[:24]
        if api_cash:row['message']='已读取该银行黄金电子账户余额；不含绑定银行卡余额。'
        if include_advice and 'jd_live_advice' in tables:
            from .controls import active,control
            for r in conn.execute('SELECT id,analyst,created_at,expires_at,snapshot_revision,advice_json FROM jd_live_advice WHERE bank=? AND id IN (SELECT MAX(id) FROM jd_live_advice WHERE bank=? GROUP BY analyst) ORDER BY analyst',(bank,bank)):
                if not active(conn,r[1]):continue
                changed=control(conn,r[1]).get('changed_at')
                if changed and r[2]<=changed:continue
                value=json.loads(r[5])
                state='expired' if age(r[3],now)>=0 else 'account_changed' if r[4]!=row.get('revision') else 'data_unavailable' if not fresh else 'review_required' if value.get('checks') else 'current'
                row['advice'].append({'id':r[0],'analyst':r[1],'created_at':r[2],'expires_at':r[3],'status':state,**value})
        out.append(row)
    return out


def model_input(conn,now=None):
    rows=accounts(conn,now,include_advice=False)
    if not any(r.get('fetched_at') for r in rows):return None
    fields=('bank','name','product_sku','status','fetched_at','revision','total_grams','sellable_grams','frozen_grams',
            'average_cost_cny','holding_pnl_cny','holding_pnl_basis','cash_cny','cash_balance_cny','cash_scope','cash_source','cash_declared_at','position_pct','quote','pending_order_count','recent_trade','sell_fee_rate','coverage')
    values={r['bank']:{k:r.get(k) for k in fields} for r in rows}
    for row in rows:
        for kind,limit in (('holding_details',3),('closed_positions',5)):
            part=row.get(kind)
            if part:values[row['bank']][kind]={**part,'records':part['records'][:limit],'limit':limit}
    return {'execution':'advice_only_user_submits','risk_profile':risk_profile(),'accounts':values,
            'unknown':(['可用资金'] if any(r['cash_cny'] is None for r in rows) else [])+['完整待成交委托列表','完整成交历史'],
            'note':'两家为同一用户的真实资产；现金来源为用户声明时不可冒充接口返回；不可借用模拟盘现金或执行任何真实交易。'}
