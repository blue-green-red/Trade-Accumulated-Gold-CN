"""Read-only cash-versus-participation diagnostics, never orders or hindsight execution."""
import json
import math
from datetime import datetime,timedelta,timezone

from .controls import control
from .trade_costs import number

POLICY={'reference_position_pcts':[1,5],'symmetric_move_pct':2,'history_reference_pct':1,
        'note':'仅纸上对照，非建议仓位或交易阈值；历史按输入报价及当前费率持有至今估值，不模拟期间退出或最佳买卖。'}


def stamp(value):
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return d if d.tzinfo else None
    except (AttributeError,TypeError,ValueError):return None


def fresh(at,now,seconds):
    at,now=stamp(at),stamp(now)
    return bool(at and now and -30<=(now-at).total_seconds()<=seconds)


def delta(amount,entry,exit_price,fee,spread=0):
    return amount/entry*(exit_price*(1-fee)-spread)-amount


def sizes(cash,grams,price,fee,spread,total_cash=None):
    net=(cash if total_cash is None else total_cash)+grams*(price*(1-fee)-spread)
    return [min(round(net*p/100,2),math.floor((cash+1e-8)*100)/100) for p in POLICY['reference_position_pcts']]


def current(cash,grams,price,fee,spread=0,total_cash=None):
    vals=[number(x) for x in (cash,grams,price,fee,spread)]
    if any(x is None for x in vals):return {'status':'data_unavailable'}
    cash,grams,price,fee,spread=vals
    if price<=0 or fee>=1 or price*(1-fee)<=spread:
        return {'status':'data_unavailable'}
    total=number(total_cash) if total_cash is not None else cash
    if total is None:return {'status':'data_unavailable'}
    amounts=sizes(*vals,total_cash=total)
    move=POLICY['symmetric_move_pct']/100
    return {'status':'ready' if cash>0 else 'no_cash','available_cash_cny':cash,
            'reference_amounts_cny':amounts,'move_pct':POLICY['symmetric_move_pct'],
            'up_delta_cny':[round(delta(a,price,price*(1+move),fee,spread),2) for a in amounts],
            'down_delta_cny':[round(delta(a,price,price*(1-move),fee,spread),2) for a in amounts]}


def history(anchor,rounds,current_price,fee,spread,now):
    at=stamp(anchor['quote_at']);end=stamp(now);decision=stamp(anchor['created_at'])
    if not at or not end or not decision or at>end or decision>end or at>decision+timedelta(seconds=30):return None
    amount=sizes(anchor['cash'],anchor['grams'],anchor['price'],fee,spread,anchor.get('total_cash'))[0]
    if amount<=0:return None
    return {'since':anchor['created_at'],'reference_quote_at':anchor['quote_at'],'rounds':rounds,
            'reference_quote_cny':anchor['price'],'reference_amount_cny':amount,
            'observed_minutes':round((end-at).total_seconds()/60,1),
            'quote_change_pct':round((current_price/anchor['price']-1)*100,3),
            'price_only_delta_cny':round(amount*(current_price/anchor['price']-1),2),
            'exit_cost_cny':round(amount/anchor['price']*(current_price*fee+spread),2),
            'buy_hold_delta_cny':round(delta(amount,anchor['price'],current_price,fee,spread),2)}


def simulation(conn,account,quote,as_of,fee,spread):
    if not quote or quote.get('source')!='jd_latest_price_api' or not all(fresh(quote.get(k),as_of,120) for k in ('quote_at','fetched_at')):
        return {'status':'data_unavailable'}
    if (account.get('risk_budget') or {}).get('buy_paused'):return {'status':'buy_paused'}
    cash=number(account.get('cash'));plan=account.get('plan') or {};d=plan.get('decision') or {}
    if plan.get('status')=='pending' and d.get('action')=='buy':
        reserved=number(d.get('amount_cny'))
        cash=max(0,cash-reserved) if cash is not None and reserved is not None else None
    value=current(cash,account.get('grams'),quote.get('price'),fee,spread,account.get('cash'))
    value['as_of']=quote['quote_at']
    if value['status']!='ready':return value
    ctl=control(conn,account['model']);now=stamp(as_of)
    if not ctl['enabled'] or now is None:return {'status':'disabled'}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ai_views' not in tables:return value
    path='$._account_inputs.'+account['start_mode']+'.revision'
    rows=conn.execute('SELECT p.created_at,p.decision_json,p.status,v.snapshot_at,v.ts,'
        "json_extract(v.view_json,'$._price_guard.jd'),json_extract(v.view_json,?) "
        'FROM ai_plans p JOIN ai_views v ON v.rowid=p.view_id WHERE p.account=? AND p.generation=? '
        'AND julianday(p.created_at)<=julianday(?) AND julianday(p.created_at)>=julianday(?) '
        'ORDER BY p.id DESC LIMIT 2048',
        (path,account['key'],ctl['generation'],min(now,stamp(quote['quote_at'])).isoformat(),(now-timedelta(days=14)).isoformat()))
    anchor=None;rounds=0
    for row in rows:
        d=json.loads(row[1])
        if row[2]!='executed' or d.get('action') not in ('hold','wait') or row[6]!=account['revision']:break
        p=number(row[5])
        quoted=stamp(row[3] or row[4]);created=stamp(row[0])
        if not p or not quoted or not created or quoted>created+timedelta(seconds=30) or (ctl.get('changed_at') and created<stamp(ctl['changed_at'])):break
        anchor={'created_at':row[0],'quote_at':row[3] or row[4],'price':p,'cash':cash,'total_cash':account['cash'],'grams':account['grams']};rounds+=1
    if anchor:
        result=history(anchor,rounds,number(quote['price']),fee,spread,min(now,stamp(quote['quote_at'])).isoformat())
        if result:value['waiting']=result
    return value


def bank_cash(account):
    cash=number(account.get('cash_cny'));orders=account.get('pending_orders') or {}
    if cash is None or orders.get('status')!='fresh' or orders.get('complete') is not True:return None
    from .jd_live_path import remaining
    for o in orders.get('records',[]):
        if o.get('status') not in ('pending','partially_filled') or o.get('action')!='buy':continue
        grams=remaining(o);price=number(o.get('price_cny'))
        if grams is None or not price:return None
        cash-=grams*price
    return max(0,cash)


def live(conn,account,model,now=None):
    now=now or datetime.now(timezone.utc);as_of=now.isoformat();q=account.get('quote') or {}
    if account.get('status')!='fresh' or q.get('status')!='fresh' or not fresh(q.get('quoted_at'),as_of,180):
        return {'status':'data_unavailable'}
    fee=number(account.get('sell_fee_rate'));price=number(q.get('sell_price_cny'))
    value=current(bank_cash(account),account.get('total_grams'),price,fee,total_cash=account.get('cash_cny'))
    value['as_of']=q['quoted_at']
    if value['status']!='ready':return value
    ctl=control(conn,model)
    if not ctl['enabled']:return {'status':'disabled'}
    rows=conn.execute('SELECT id,created_at,snapshot_revision,advice_json FROM jd_live_advice '
        'WHERE bank=? AND analyst=? AND julianday(created_at)<=julianday(?) AND julianday(created_at)>=julianday(?) '
        'ORDER BY id DESC LIMIT 2048',(account['bank'],model,min(now,stamp(q['quoted_at'])).isoformat(),(now-timedelta(days=14)).isoformat()))
    anchor=None;rounds=0
    for row in rows:
        d=json.loads(row[3]);state=d.get('input_state') or {};p=number(d.get('input_quote_cny'))
        if (row[2]!=account.get('revision') or d.get('action') not in ('hold','wait') or d.get('checks')
            or d.get('input_quote_status')!='fresh' or not p
            or any(o.get('kind')=='limit_buy' and not o.get('checks') for o in d.get('suggested_orders',[]))
            or (ctl.get('changed_at') and stamp(row[1])<stamp(ctl['changed_at']))):break
        cash=d.get('input_participation_cash_cny',state.get('cash_cny') if state.get('pending_order_count')==0 else None)
        cash=number(cash);grams=number(state.get('total_grams'))
        quoted=stamp(d.get('input_quote_at'));created=stamp(row[1])
        if cash is None or grams is None or not quoted or not created or quoted>created+timedelta(seconds=30):break
        anchor={'created_at':row[1],'quote_at':d.get('input_quote_at'),'price':p,'cash':cash,'total_cash':number(state.get('cash_cny')),'grams':grams};rounds+=1
    if anchor:
        result=history(anchor,rounds,price,fee,0,min(now,stamp(q['quoted_at'])).isoformat())
        if result:value['waiting']=result
    return value
