"""Verified real-account snapshots. Independent of every simulation and execution path."""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from .jd_readonly_probe import BANKS

PRODUCTS={'zheshang':('1961543816','浙商积存金','indexDataStd'),'minsheng':('21001001000001','民生积存金','indexData')}
STALE_SECONDS=1800
CONTROL_KEY='jd_live_banks'


def enabled_banks(conn):
    """Only explicitly enabled real accounts enter collection, advice and presentation."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone():return tuple(PRODUCTS)
    row=conn.execute('SELECT value FROM state WHERE key=?',(CONTROL_KEY,)).fetchone()
    if not row:return tuple(PRODUCTS)
    try:
        banks=json.loads(row[0])['banks']
        if not isinstance(banks,list) or any(not isinstance(bank,str) or bank not in PRODUCTS for bank in banks):return ()
        return tuple(bank for bank in PRODUCTS if bank in banks)
    except (ValueError,TypeError,KeyError):return ()


def set_enabled_banks(conn,banks,at=None):
    """Retire polling and unsent events while keeping financial and delivery history."""
    if not isinstance(banks,(list,tuple)) or any(not isinstance(bank,str) or bank not in PRODUCTS for bank in banks):raise ValueError('Invalid real banks')
    selected=[bank for bank in PRODUCTS if bank in banks]
    at=at or datetime.now(timezone.utc).isoformat()
    disabled=set(PRODUCTS)-set(selected)
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (CONTROL_KEY,json.dumps({'banks':selected,'changed_at':at}),at))
        for bank in disabled:
            for model in ('codex','claude'):
                conn.execute('DELETE FROM state WHERE key=?',('notify:'+model+':live_'+bank,))
        for key,raw in conn.execute("SELECT key,value FROM state WHERE key LIKE 'ai_events:%'").fetchall():
            value=json.loads(raw)
            value['pending']=[e for e in value.get('pending',[]) if (e.get('data') or {}).get('bank') not in disabled]
            conn.execute('UPDATE state SET value=?,updated_at=? WHERE key=?',(json.dumps(value,ensure_ascii=False),at,key))
        if 'notification_outbox' in tables:
            for key,raw in conn.execute("SELECT event_key,event_json FROM notification_outbox WHERE status IN ('pending','retry')").fetchall():
                topic=json.loads(raw).get('notification_topic','').partition(':')[2]
                if topic in {'live_'+bank for bank in disabled}:
                    conn.execute("UPDATE notification_outbox SET status='cancelled',updated_at=? WHERE event_key=?",(at,key))
    return selected

def risk_profile():
    from .risk_profiles import PROFILES
    key=os.environ.get('GOLD_JD_LIVE_RISK_PROFILE','high')
    if key not in PROFILES:key='high'
    return {'key':key,**PROFILES[key],'enforcement':'advice_reference_only',
            'risk_budget_cny':None,'historical_drawdown_pct':None,
            'note':'金额预算见各户account_risk，仅为参考上限而非必须用满；历史峰值未知，成本浮亏不等于峰值回撤。'}
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
    from .jd_live_orders import SCHEMA as order_schema
    with conn:
        conn.executescript(SCHEMA+order_schema)
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
    identity=account_identity(data)
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


def account_identity(data):
    identity={k:data.get(k) for k in ('product_sku','total_grams','sellable_grams','frozen_grams','average_cost_cny','pending_order_count','recent_trade','cash_cny')}
    part=data.get('pending_orders')
    identity['orders']=[{k:r.get(k) for k in ('order_key','action','kind','status','grams','filled_grams','price_condition','price_cny','loss_price_cny','expires_at')} for r in sorted((part or {}).get('records',[]),key=lambda r:r['order_key'])] if part else None
    outcomes=data.get('order_outcomes')
    identity['order_outcomes']=sorted(outcomes,key=lambda r:r['order_key']) if outcomes else outcomes
    return identity


def accounts(conn,now=None,include_advice=True,selected_bank=None):
    now=now or datetime.now(timezone.utc)
    banks=tuple(bank for bank in enabled_banks(conn) if selected_bank is None or bank==selected_bank)
    if not banks:return []
    placeholders=','.join('?' for _ in banks)
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    records={r[0]:{'checked_at':r[1],'public_status':r[2]} for r in conn.execute(f'SELECT bank,checked_at,public_status FROM jd_live_readonly WHERE bank IN ({placeholders})',banks)} if 'jd_live_readonly' in tables else {}
    snapshots={r[0]:{'fetched_at':r[1],'revision':r[2],**json.loads(r[3])} for r in conn.execute(f'SELECT * FROM jd_live_snapshots WHERE bank IN ({placeholders})',banks)} if 'jd_live_snapshots' in tables else {}
    declarations={r[0]:r for r in conn.execute(f'SELECT bank,cash_cny,declared_at,anchor,invalidated_at FROM jd_live_cash_declarations WHERE bank IN ({placeholders})',banks)} if 'jd_live_cash_declarations' in tables else {}
    out=[]
    for bank in banks:
        info=BANKS[bank]
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
        row['advice']=[];row['advice_history']=[]
        row['risk_profile']=risk_profile()
        pending=row.get('pending_orders')
        if pending:
            pending=dict(pending)
            observed=(row.get('component_times') or {}).get('pending_orders')
            available=(row.get('coverage') or {}).get('pending_orders')=='available' and 0<=age(observed,now)<=900
            pending['status']='fresh' if fresh and available else 'stale'
            pending['freshness_reason']=('account_unavailable' if not fresh else
                'refresh_failed' if (row.get('coverage') or {}).get('pending_orders','').startswith('unavailable:') else
                'cache_old' if not available and observed else 'not_fetched' if not available else
                'incomplete' if not pending.get('complete') else None)
            if not available:pending['complete']=False
            row['pending_orders']=pending
        from .jd_live_orders import history,progress
        row['order_history']=history(conn,bank) if 'jd_live_orders' in tables else []
        combined={o['order_key']:o for o in (row.get('pending_orders') or {}).get('records',[])}
        combined.update({o['order_key']:o for o in row['order_history']})
        row['order_history']=list(combined.values())
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
        from .jd_live_risk import account_risk,protection_state
        row['protection_state']=protection_state(row)
        row['position_pct'],row['account_risk']=account_risk(row,row['risk_profile'])
        row['risk_profile']['risk_budget_cny']=row['account_risk']['risk_budget_cny']
        if include_advice and 'jd_live_advice' in tables:
            from .controls import active,control
            seen={}
            pinned={i for o in row['order_history'] for i in o.get('advice_links',{}).values()}
            placeholders=','.join('?' for _ in pinned) or 'NULL'
            query=f'SELECT id,analyst,created_at,expires_at,snapshot_revision,advice_json FROM jd_live_advice WHERE bank=? AND (id IN (SELECT id FROM jd_live_advice WHERE bank=? ORDER BY id DESC LIMIT 20) OR id IN ({placeholders})) ORDER BY id DESC'
            for r in conn.execute(query,(bank,bank,*pinned)):
                if not active(conn,r[1]):continue
                changed=control(conn,r[1]).get('changed_at')
                if changed and r[2]<=changed:continue
                if seen.get(r[1],0)>=3 and r[0] not in pinned:continue
                value=json.loads(r[5])
                lifecycle='expired' if age(r[3],now)>=0 else 'account_changed' if r[4]!=row.get('revision') else 'data_unavailable' if not fresh else 'current'
                state='review_required' if lifecycle=='current' and value.get('checks') else lifecycle
                from .jd_live_semantics import blocking_checks
                action_state='review_required' if lifecycle=='current' and blocking_checks(value) else lifecycle
                execution=progress(value,r[2],row['order_history'])
                target=row['advice'] if not seen.get(r[1]) else row['advice_history']
                entry={'id':r[0],'analyst':r[1],'created_at':r[2],'expires_at':r[3],'snapshot_revision':r[4],'status':state,
                       'lifecycle_status':lifecycle,'action_status':action_state,'execution_progress':execution,**value}
                from .jd_live_orders import proposal_progress
                for proposal in entry.get('suggested_orders',[]):
                    proposal['execution_progress']=proposal_progress(proposal,r[2],row['order_history'],value.get('valid_for_minutes',240))
                from .jd_live_path import current_state as path_current_state
                path_current_state(entry,row,conn)
                from .jd_live_conditions import condition_state,management_state
                for item in entry.get('order_management',[]):item['state']=management_state(row,entry,item,now)
                for condition in entry.get('review_conditions',[]):condition['monitoring']=condition_state(conn,row,entry,condition,now)
                target.append(entry)
                seen[r[1]]=seen.get(r[1],0)+1
            from .participation_review import live as participation_review
            reviewed=set()
            for entry in sorted(row['advice']+row['advice_history'],key=lambda v:v['id'],reverse=True):
                if entry['analyst'] not in reviewed:
                    entry['participation_review']=participation_review(conn,row,entry['analyst'],now)
                    reviewed.add(entry['analyst'])
        row['review_status']=_review_status(conn,bank)
        out.append(row)
    return out


def model_input(conn,now=None):
    rows=accounts(conn,now,include_advice=True)
    if not any(r.get('fetched_at') for r in rows):return None
    fields=('bank','name','product_sku','status','fetched_at','revision','total_grams','sellable_grams','frozen_grams',
            'average_cost_cny','holding_pnl_cny','holding_pnl_basis','cash_cny','cash_balance_cny','cash_scope','cash_source','cash_declared_at','position_pct','account_risk','quote','pending_order_count','pending_orders','recent_trade','sell_fee_rate','coverage','component_times')
    values={r['bank']:{k:r.get(k) for k in fields} for r in rows}
    for row in rows:
        from .trade_costs import position_comparison
        quote=row.get('quote') or {}
        if row['connected'] and quote.get('status')=='fresh':
            comparison=position_comparison(row.get('total_grams'),quote.get('sell_price_cny'),row.get('sell_fee_rate'))
            if comparison:values[row['bank']]['position_comparison']=comparison
        active_links={i for o in row['order_history'] if o['status'] in ('pending','partially_filled') for i in o.get('advice_links',{}).values()}
        previous=[];counts={}
        for value in row['advice']+row['advice_history']:
            name=value['analyst'];counts[name]=counts.get(name,0)+1
            if counts[name]==1 or value['id'] in active_links:
                brief={k:value.get(k) for k in ('id','analyst','action','grams','price_condition','price_cny','created_at','expires_at','status','execution_progress')}
                if counts[name]==1:
                    reason=value.get('reason') or ''
                    brief['reason']=reason if len(reason)<=420 else reason[:419]+'…'
                    if value.get('reassessment'):brief['reassessment']=value['reassessment']
                    if value.get('position_path'):
                        brief['position_path']={k:value['position_path'].get(k) for k in ('target_position_pct','boundary_scenario_index','exit_scope','continuation','boundary_price_cny','input_grams','state')}
                    if value.get('downside_scenarios'):
                        brief['downside_scenarios']=[{k:s.get(k) for k in ('price_cny','price_role','preferred_action','basis')} for s in value['downside_scenarios']]
                    if value.get('limit_buy_reviews'):
                        brief['limit_buy_reviews']=[{k:s.get(k) for k in (('decision','basis','reason') if s.get('decision')=='not_worthwhile' else
                            ('decision','entry_price_cny','target_price_cny','risk_exit_cny','grams','expires_at','basis','reason'))} for s in value['limit_buy_reviews']]
                    from .jd_live_feedback import strategy_review
                    brief['strategy_review']=strategy_review(conn,row,name,now)
                brief['order_management']=[{k:o.get(k) for k in ('order_key','action','replacement','state')} for o in value.get('order_management',[])]
                brief['review_conditions']=[{k:o.get(k) for k in ('condition','price_cny','quote_side','order_key','reason')} for o in value.get('review_conditions',[])] if counts[name]==1 else []
                if value.get('suggested_orders'):
                    brief['suggested_orders']=[{k:o.get(k) for k in ('kind','grams','price_cny','loss_price_cny','expires_at','reason','execution_progress')} for o in value['suggested_orders']]
                previous.append(brief)
        values[row['bank']]['previous_advice']=previous
        # First use / newly enabled models have no historical advice to carry the feedback.
        from .controls import MODELS,active
        if any(active(conn,name) and name not in counts for name in MODELS):
            from .participation_review import current,bank_cash
            q=row.get('quote') or {}
            values[row['bank']]['participation_review']=(
                {**current(bank_cash(row),row.get('total_grams'),q.get('sell_price_cny'),row.get('sell_fee_rate'),total_cash=row.get('cash_cny')),'as_of':q.get('quoted_at')}
                if row.get('connected') and q.get('status')=='fresh' else {'status':'data_unavailable'})
        orders=values[row['bank']].get('pending_orders')
        if orders and len(orders['records'])>5:
            values[row['bank']]['pending_orders']={**orders,'records':orders['records'][:5],'source_complete':orders['complete'],'complete':False,'omitted_count':len(orders['records'])-5}
        for kind,limit in (('holding_details',3),('closed_positions',5)):
            part=row.get(kind)
            if part:values[row['bank']][kind]={**part,'records':part['records'][:limit],'limit':limit}
    shared_profile=risk_profile()
    shared_profile.pop('risk_budget_cny',None)  # The amount belongs to each bank's account_risk, never a shared pot.
    return {'execution':'advice_only_user_submits','risk_profile':shared_profile,'accounts':values,
            'unknown':(['可用资金'] if any(r['cash_cny'] is None for r in rows) else [])+(['完整待成交委托列表'] if any(not (r.get('pending_orders') or {}).get('complete') for r in rows) else [])+['完整成交历史'],
            'note':'仅包含启用的真实账户；现金来源为用户声明时不可冒充接口返回；不可借用模拟盘现金或执行任何真实交易。'}


def _review_status(conn,bank):
    result=[]
    from .controls import MODELS,active
    from .ai_events import read
    for model in MODELS:
        if not active(conn,model):continue
        runtime=conn.execute('SELECT value FROM state WHERE key=?',('ai_runtime:'+model,)).fetchone()
        runtime=json.loads(runtime[0]) if runtime else {}
        pending=[e for e in read(conn,model)['pending'] if e.get('data',{}).get('bank')==bank]
        if pending:result.append({'analyst':model,'status':'running' if runtime.get('running') else 'queued','reasons':[e['reason'] for e in pending]})
    return result
