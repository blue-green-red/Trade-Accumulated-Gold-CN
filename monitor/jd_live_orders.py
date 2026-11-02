"""Sanitized conditional-order observations and explicitly qualified advice matching."""
import hashlib
import json
import re
from datetime import datetime,timedelta

SCHEMA='''CREATE TABLE IF NOT EXISTS jd_live_orders (
 bank TEXT NOT NULL, order_key TEXT NOT NULL, data_json TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY(bank,order_key)
);'''
STATUSES={1:'pending',2:'filled',3:'expired',4:'cancelled',5:'failed'}


def parse_order(raw,bank):
    from .jd_live_sync import vendor_time
    from .jd_live_readonly import PRODUCTS,number
    if str(raw.get('productSku'))!=PRODUCTS[bank][0]:raise ValueError('Order bank mismatch')
    uuid=raw.get('uuid')
    if not isinstance(uuid,str) or not uuid:raise ValueError('Order identifier missing')
    rule_number=number(raw.get('ruleType'),True);status_number=number(raw.get('status'),True)
    if rule_number!=int(rule_number) or status_number!=int(status_number):raise ValueError('Invalid order state')
    rule=int(rule_number)
    status=STATUSES.get(int(status_number),'unknown')
    opt=lambda k:number(raw[k],True) if raw.get(k) is not None else None
    filled=opt('filledGram')
    grams=opt('orderGram')
    if status=='pending' and filled is not None and grams and 0<filled<grams:status='partially_filled'
    return {'order_key':hashlib.sha256((bank+':'+uuid).encode()).hexdigest()[:24],
            'action':'buy' if rule==1 else 'sell' if rule in (2,3) else 'unknown',
            'kind':{1:'limit_buy',2:'limit_sell',3:'take_profit_stop_loss'}.get(rule,'unknown'),
            'status':status,'grams':grams,'filled_grams':filled,
            'price_cny':opt('orderPrice'),'loss_price_cny':opt('lossPrice'),
            'price_condition':'at_or_below' if rule==1 else 'at_or_above' if rule==2 else 'none',
            'created_at':vendor_time(raw.get('createdTime')),
            'expires_at':vendor_time(raw.get('ruleDeadLineTime') or raw.get('deadLineTime')),
            'modified_at':vendor_time(raw.get('modifiedTime')),
            'auto_create':raw.get('autoCreateFlag')==1}


def parse_list(payload,bank):
    from .jd_live_sync import component
    data=component(payload)
    if not isinstance(data,dict):raise ValueError('Order list missing')
    total=data.get('total')
    if type(total) is not int or total<0:raise ValueError('Order total missing')
    raw=data.get('dataList',[])
    if not isinstance(raw,list) or (total and not raw):raise ValueError('Order list incomplete')
    return {'records':[parse_order(r,bank) for r in raw], 'total_count':total,
            'complete':total==len(raw),'scope':'bank_pending_conditional_orders'},raw


def record(conn,bank,orders,at):
    with conn:
        for order in orders:
            conn.execute('INSERT INTO jd_live_orders VALUES(?,?,?,?,?) ON CONFLICT(bank,order_key) DO UPDATE SET data_json=excluded.data_json,seen_at=excluded.seen_at',
                         (bank,order['order_key'],json.dumps(order,ensure_ascii=False),at,at))


def history(conn,bank,limit=10):
    return [{**json.loads(r[0]),'observed_at':r[1]} for r in conn.execute("SELECT data_json,seen_at FROM jd_live_orders WHERE bank=? ORDER BY json_extract(data_json,'$.created_at') DESC,seen_at DESC,order_key LIMIT ?",(bank,limit))]


def trade_history(payload,bank):
    from .jd_live_sync import vendor_time
    from .jd_live_readonly import PRODUCTS,number
    result=payload.get('resultData') or {}
    if result.get('status')!='SUCCESS' or str(result.get('errorCode'))!='00000' or not isinstance(result.get('datas'),list):raise ValueError('Trade history unavailable')
    out=[]
    for row in result['datas']:
        if str(row.get('productSku'))!=PRODUCTS[bank][0]:raise ValueError('Trade history bank mismatch')
        if str(row.get('isConditional')) not in ('1','2','3','4'):continue
        title=str(row.get('leftTitile1',''))
        status=next((state for word,state in (('过期','expired'),('撤销','cancelled'),('失败','failed'),('部分成交','partially_filled'),('等待中','pending'),('未成交','unknown'),('未成功','unknown'),('成功','filled'),('已成交','filled'),('全部成交','filled')) if word in title),'unknown')
        key=row.get('jdorderId')
        if not isinstance(key,str) or not key:continue
        grams=re.fullmatch(r'(\d+(?:\.\d+)?)克',str(row.get('rightTitile2','')))
        action='sell' if '卖' in title else 'buy' if '买' in title else 'unknown'
        out.append({'order_key':hashlib.sha256((bank+':'+key).encode()).hexdigest()[:24],
                    'status':status,'action':action,'grams':float(grams[1]) if grams else None,
                    'price_cny':number(row['orderPrice'],True) if row.get('orderPrice') is not None else None,
                    'price_condition':'at_or_above' if action=='sell' and '限价' in title else 'at_or_below' if action=='buy' and '限价' in title else 'none',
                    'created_at':vendor_time(row.get('leftTitile2')),'filled_grams':number(row['filledGram'],True) if row.get('filledGram') is not None else None})
    return out


def progress(advice,created_at,orders):
    """Match only a unique same-bank candidate; adjusted parameters remain visibly inferred."""
    matches=[]
    for order in orders:
        if advice['action'] not in ('buy','sell') or order.get('action')!=advice['action']:continue
        try:
            if datetime.fromisoformat(order['created_at'])<datetime.fromisoformat(created_at):continue
            if datetime.fromisoformat(order['created_at'])>datetime.fromisoformat(created_at)+timedelta(minutes=advice.get('valid_for_minutes',240)):continue
            grams,price=advice.get('grams'),advice.get('price_cny')
            if not grams or not price or order.get('grams') is None or order.get('price_cny') is None:continue
            if abs(order['grams']-grams)>max(.05,grams*.001) or abs(order['price_cny']-price)>price*.001:continue
            if order.get('price_condition')!=advice.get('price_condition'):continue
        except (ValueError,TypeError,KeyError):continue
        exact=abs(order['grams']-grams)<.0001 and abs(order['price_cny']-price)<.005
        matches.append({**order,'match':'exact_parameters' if exact else 'similar_parameters',
                        'association':'inferred_from_parameters_and_time_not_submission_proof'})
    active=[o for o in matches if o['status'] in ('pending','partially_filled')]
    if len(active)==1:matches=active
    return {'status':matches[0]['status'] if len(matches)==1 else 'ambiguous' if matches else 'not_observed',
            'match':matches[0]['match'] if len(matches)==1 else None,'orders':matches[:3]}
