"""Manual order-management validation and cached bank-price review triggers. No trading APIs."""
import hashlib
import json
from datetime import datetime


def stamp(value):
    if not isinstance(value,str):return None
    try:
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
        return value if value.tzinfo else None
    except (ValueError,TypeError):return None


def validate_structure(value,source,now):
    warnings=[]
    part=source.get('pending_orders') or {}
    orders={o['order_key']:o for o in part.get('records',[])}
    seen=set()
    for item in value['order_management']:
        checks=[];key=item['order_key'];original=orders.get(key)
        if sum(o['order_key']==key for o in value['order_management'])>1:checks.append('同一挂单重复提出处理建议')
        seen.add(key)
        if not original or original.get('status') not in ('pending','partially_filled'):checks.append('目标不在输入的有效待成交明细中')
        if original and stamp(original.get('expires_at')) and stamp(original['expires_at'])<=now:checks.append('目标挂单有效期已过，需刷新确认')
        if part.get('status')!='fresh':checks.append('挂单明细不是新鲜数据')
        item['input_order']={k:original.get(k) for k in ('order_key','action','kind','grams','filled_grams','price_condition','price_cny','loss_price_cny','expires_at','status')} if original else None
        replacement=item.get('replacement')
        if item['action']!='replace' and replacement is not None:checks.append('保留或撤销建议不应附修改参数')
        if item['action']=='replace':
            changes={k:v for k,v in (replacement or {}).items() if v is not None}
            expiry_only=set(changes)=={'expires_at'}
            if not changes:checks.append('修改建议未指定新参数')
            if original:
                effective={k:original.get(k) for k in ('action','kind','grams','price_condition','price_cny','loss_price_cny','expires_at')}
                if original.get('status')=='partially_filled' and 'grams' not in changes and not expiry_only:
                    if original.get('filled_grams') is None:checks.append('部分成交单未确认剩余数量，修改时需明确克数')
                    elif original.get('grams') is not None:effective['grams']=max(0,original['grams']-original['filled_grams'])
                effective.update(changes);item['effective_replacement']=effective
                if original.get('kind') not in ('limit_buy','limit_sell') and not (original.get('kind')=='take_profit_stop_loss' and expiry_only):checks.append('组合条件单仅支持保留全部参数并修改截止时间，其他复杂修改需额外核对')
                if original.get('kind')=='take_profit_stop_loss' and expiry_only and (not original.get('loss_price_cny') or not original.get('price_cny') or original['loss_price_cny']>=original['price_cny']):checks.append('组合单原止盈止损参数不完整，修改期限前需核对')
                if changes and all(effective.get(k)==original.get(k) for k in changes):checks.append('修改后的参数与原单相同')
                expiry=stamp(effective.get('expires_at'))
                if expiry is None or expiry<=now:checks.append('修改后的截止时间必须含时区且晚于当前时间')
                grams,price=effective.get('grams'),effective.get('price_cny')
                if not grams or not price:checks.append('替换后的数量和价格不完整')
                if not expiry_only and original.get('action')=='sell' and grams and grams>(source.get('sellable_grams') or 0)+1e-8:checks.append('替换后数量超过当前可卖克数，需核对原单冻结量及撤单释放量')
                if not expiry_only and original.get('action')=='buy' and grams and price and (source.get('cash_cny') is None or grams*price>source['cash_cny']+1e-8):checks.append('替换买单需核对本户现金、原单冻结资金及撤单退款')
        item['checks']=checks
        warnings.extend('挂单建议：'+c for c in checks)
    if part.get('status')=='fresh':
        for key,order in orders.items():
            if order.get('status') in ('pending','partially_filled') and key not in seen:warnings.append('已有待成交挂单未提供结构化处理建议')
        management={o['order_key']:o for o in value['order_management']}
        reserved_sell=0
        for key,order in orders.items():
            if order.get('action')!='sell' or order.get('status') not in ('pending','partially_filled'):continue
            item=management.get(key,{})
            remaining=max(0,(order.get('grams') or 0)-(order.get('filled_grams') or 0))
            if item.get('action')=='replace' and not item.get('checks') and (item.get('replacement') or {}).get('grams') is not None:
                remaining=max(remaining,(item.get('effective_replacement') or {}).get('grams') or 0)
            reserved_sell+=remaining or 0
        reserved_sell+=(value.get('grams') or 0) if value['action']=='sell' else 0
        if source.get('total_grams') is not None and reserved_sell>source['total_grams']+1e-8:warnings.append('已有卖单、替换单与新增卖出合计超过本户总持仓，需避免重复占用')
        if not part.get('complete') and (value['action'] in ('buy','sell') or any(o['action']=='replace' for o in value['order_management'])):warnings.append('委托明细不完整，需核对其他挂单的数量和资金占用')
    for condition in value['review_conditions']:
        checks=[];key=condition.get('order_key')
        if key and (key not in orders or orders[key].get('status') not in ('pending','partially_filled')):checks.append('复评条件关联的挂单不在有效输入中')
        fingerprint=condition_key(condition)
        if sum(condition_key(c)==fingerprint for c in value['review_conditions'])>1:checks.append('重复的价格复评条件')
        condition['checks']=checks
        warnings.extend('复评条件：'+c for c in checks)
    return warnings


def condition_key(condition):
    fields={k:condition.get(k) for k in ('condition','price_cny','quote_side','order_key')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]


def watch_key(conn,model,bank,condition):
    from .controls import control
    return f'live-price-watch:{model}:{control(conn,model)["generation"]}:{bank}:{condition_key(condition)}'


def read_watch(conn,key):
    row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def condition_state(conn,account,advice,condition,now):
    """Derived display status, including historical triggers; never schedules work from a GET."""
    from .jd_live_readonly import age
    from .trading_hours import trading_session
    key=watch_key(conn,advice['analyst'],account['bank'],condition);watch=read_watch(conn,key)
    result={'condition_key':condition_key(condition),'triggered_at':watch.get('triggered_at') if watch.get('advice_id')==advice['id'] else None,
            'last_quote_at':watch.get('last_quote_at'),'last_price_cny':watch.get('last_price_cny'),
            'event_id':watch.get('event_id')}
    path_boundary=(advice.get('position_path') or {}).get('boundary_price_cny')
    boundary_review=advice.get('_path_pause') and advice.get('lifecycle_status')=='current' and condition['condition']=='at_or_below' and condition['price_cny']==path_boundary and not condition.get('order_key')
    if advice.get('action_status',advice['status'])!='current' and not boundary_review:result['status']=advice.get('action_status',advice['status']);return result
    if condition.get('checks'):result['status']='review_required';return result
    if not trading_session(now)['is_open']:result['status']='market_closed';return result
    quote=account.get('quote') or {}
    if not account['connected'] or quote.get('status')!='fresh' or not -30<=age(quote.get('quoted_at'),now)<=180:
        result['status']='data_unavailable';return result
    input_at=stamp(advice.get('input_quote_at'));quote_at=stamp(quote.get('quoted_at'))
    if input_at and (not quote_at or quote_at<input_at):result['status']='data_unavailable';return result
    order_key=condition.get('order_key')
    if order_key:
        part=account.get('pending_orders') or {}
        if part.get('status')!='fresh':result['status']='data_unavailable';return result
        target=next((o for o in part.get('records',[]) if o['order_key']==order_key and o['status'] in ('pending','partially_filled')),None)
        if not target or (stamp(target.get('expires_at')) and stamp(target['expires_at'])<=now):
            result['status']='order_inactive';return result
    from .ai_events import read
    pending=read(conn,advice['analyst'])['pending']
    if watch.get('event_id') and any(e['id']==watch['event_id'] for e in pending):result['status']='queued'
    elif watch.get('advice_id')==advice['id']:result['status']='reviewed'
    elif watch.get('latched'):result['status']='latched'
    else:result['status']='monitoring'
    return result


def management_state(account,advice,item,now):
    if advice.get('action_status',advice['status'])!='current':return advice.get('action_status',advice['status'])
    if item.get('checks'):return 'review_required'
    part=account.get('pending_orders') or {}
    if part.get('status')!='fresh':return 'data_unavailable'
    target=next((o for o in part.get('records',[]) if o['order_key']==item['order_key'] and o['status'] in ('pending','partially_filled')),None)
    if not target or (stamp(target.get('expires_at')) and stamp(target['expires_at'])<=now):return 'order_inactive'
    return 'current'


def monitor_prices(store,account,advice,now):
    from .ai_events import read,write
    from .controls import active,control
    from .jd_live_readonly import PRODUCTS
    for condition in advice.get('review_conditions',[]):
        state=condition_state(store.conn,account,advice,condition,now)
        if state['status'] not in ('monitoring','latched','queued','reviewed'):continue
        quote=account['quote'];price=quote.get(condition['quote_side']+'_price_cny')
        if not isinstance(price,(float,int)) or isinstance(price,bool) or price<=0:continue
        met=price>=condition['price_cny'] if condition['condition']=='at_or_above' else price<=condition['price_cny']
        # The collector and monitor can observe the same tick. Commit event + latch atomically.
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            switch=control(store.conn,advice['analyst'])
            if not active(store.conn,advice['analyst']) or (switch.get('changed_at') and advice['created_at']<=switch['changed_at']):continue
            key=watch_key(store.conn,advice['analyst'],account['bank'],condition);watch=read_watch(store.conn,key)
            if watch.get('last_quote_at') and stamp(quote['quoted_at'])<=stamp(watch['last_quote_at']):continue
            if met and not watch.get('latched'):
                event_id=f'live-price:{advice["analyst"]}:{account["bank"]}:{advice["id"]}:{condition_key(condition)}'
                reason=f'真实账户价格复评：{PRODUCTS[account["bank"]][1]}{ "卖出" if condition["quote_side"]=="sell" else "买入"}报价{price:g}元/克，达到{ "≥" if condition["condition"]=="at_or_above" else "≤"}{condition["price_cny"]:g}'
                events=read(store.conn,advice['analyst'])
                if event_id not in events['covered'] and not any(e['id']==event_id for e in events['pending']):
                    events['pending'].append({'id':event_id,'reason':reason,'min_gap_minutes':0,'observed_at':now.isoformat(),
                        'data':{'bank':account['bank'],'advice_id':advice['id'],'condition_key':condition_key(condition),
                                'quote_side':condition['quote_side'],'price_cny':price,'threshold_cny':condition['price_cny'],
                                'quoted_at':quote['quoted_at'],'order_key':condition.get('order_key'),'reason':condition['reason']}})
                    write(store.conn,advice['analyst'],events)
                    watch.update(triggered_at=now.isoformat(),event_id=event_id,advice_id=advice['id'])
            watch.update(latched=met,last_quote_at=quote['quoted_at'],last_price_cny=price)
            store.conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                               (key,json.dumps(watch,ensure_ascii=False),now.isoformat()))
