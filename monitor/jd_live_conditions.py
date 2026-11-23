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


def validate_structure(value,source,now,minimum_buy_grams=None):
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
        buy_review = condition.get('buy_review')
        if buy_review:
            if key or condition['quote_side'] != 'buy':
                checks.append('买入复评须使用本行买价，且不关联已有挂单')
            if buy_review['max_buy_price_cny'] < buy_review['recovery_price_cny']:
                checks.append('买价上限不能低于企稳观察价')
            if not buy_review.get('zone') and buy_review['hold_minutes'] >= value['valid_for_minutes']:
                checks.append('企稳观察时长须短于本轮有效期')
            if minimum_buy_grams is not None and buy_review['grams'] < minimum_buy_grams:
                checks.append(f'买入复评数量须至少{minimum_buy_grams:g}克')
            trigger=condition['price_cny'];recovery=buy_review['recovery_price_cny']
            cap=buy_review['max_buy_price_cny'];zone=buy_review.get('zone')
            if not zone and condition['condition']=='at_or_below' and trigger>=recovery:
                checks.append('回调触价须低于企稳价')
            if zone:
                floor=zone['floor_cny']
                if not floor<min(trigger,recovery) or recovery>trigger and condition['condition']=='at_or_above':
                    checks.append('回调或回踩区间与企稳价关系无效')
                if not zone['risk_exit_cny']<=floor or zone['weak_target_cny']<=cap:
                    checks.append('退出测算价应在区间下缘或更低，弱反弹目标须高于买价上限')
                from .trade_costs import bank_review_payoff
                if payoff:=bank_review_payoff(cap,buy_review['grams'],zone,source.get('sell_fee_rate')):
                    condition['buy_review_costs']=payoff
                else:
                    checks.append('缺本行卖费，无法核对复评候选的扣费空间')
            elif condition['condition']=='at_or_above' and not recovery<=trigger<=cap:
                checks.append('突破触价须介于回踩守住价与买价上限之间')
            if not zone and (source.get('recent_bank_quotes') or {}).get('status')=='available':
                checks.append('买入复评须按本行近期报价提供区间及扣费测算')
        if key and (key not in orders or orders[key].get('status') not in ('pending','partially_filled')):checks.append('复评条件关联的挂单不在有效输入中')
        fingerprint=condition_key(condition)
        if sum(condition_key(c)==fingerprint for c in value['review_conditions'])>1:checks.append('重复的价格复评条件')
        condition['checks']=checks
        warnings.extend('复评条件：'+c for c in checks)
    return warnings


def condition_key(condition):
    fields={k:condition.get(k) for k in ('condition','price_cny','quote_side','order_key')}
    if (condition.get('bank_breakout') or {}).get('companion'):
        fields['bank_breakout']=True
    return hashlib.sha256(json.dumps(fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]


def watch_key(conn,model,bank,condition):
    from .controls import control
    return f'live-price-watch:{model}:{control(conn,model)["generation"]}:{bank}:{condition_key(condition)}'


def read_watch(conn,key):
    row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def condition_state(conn,account,advice,condition,now,latest=True):
    """Derived display status, including historical triggers; never schedules work from a GET."""
    from .jd_live_readonly import age
    from .trading_hours import trading_session
    key=watch_key(conn,advice['analyst'],account['bank'],condition);watch=read_watch(conn,key)
    result={'condition_key':condition_key(condition),'triggered_at':watch.get('triggered_at') if watch.get('advice_id')==advice['id'] else None,
            'last_quote_at':watch.get('last_quote_at'),'last_price_cny':watch.get('last_price_cny'),
            'event_id':watch.get('event_id')}
    path_boundary=(advice.get('position_path') or {}).get('boundary_price_cny')
    path_role=(advice.get('position_path') or {}).get('boundary_role')
    if (path_role=='stress_test' and condition['condition']=='at_or_below' and condition['price_cny']==path_boundary
            and not condition.get('order_key') and not condition.get('buy_review')):
        result['status']='review_required';return result
    boundary_review=advice.get('_path_pause') and advice.get('lifecycle_status')=='current' and condition['condition']=='at_or_below' and condition['price_cny']==path_boundary and not condition.get('order_key')
    from .jd_live_buy_watch import (active_window, eligible, extended, pending_plan_review, watch_deadline, watch_id,
                                    watch_progress, watch_route_key, watch_routes)
    independent=latest and advice.get('buy_watch') and extended(condition)
    if independent:
        result['expires_at']=watch_deadline(advice,condition).isoformat()
        if now>=watch_deadline(advice,condition):result['status']='expired';return result
        if not active_window(advice,account):
            result['status']=advice.get('action_status') if advice.get('action_status') in ('account_changed','data_unavailable') else 'review_required'
            return result
    if not independent and advice.get('action_status',advice['status'])!='current' and not boundary_review:
        result['status']=advice.get('action_status',advice['status']);return result
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
    events=read(conn,advice['analyst'])
    pending=events['pending']
    if condition.get('buy_review') or condition.get('bank_breakout'):
        from .jd_live_buy_guard import confirmation_key
        current=watch_progress(read_watch(conn,f'live-buy-watch:{advice["analyst"]}:{account["bank"]}'))
        identity=watch_id(advice)
        same_plan=latest and current.get('plan_id',current.get('advice_id'))==identity
        review=pending_plan_review(events,account['bank'],identity)
        queued_keys={confirmation_key(r) for r in review['data']['routes']} if review else set()
        stages=current.get('routes',{}) if same_plan else {}
        route_keys=[watch_route_key(route) for route in watch_routes([condition])]
        def display_priority(key):
            step=stages.get(key,{})
            if (until:=stamp(step.get('observation_expires_at'))) and now>=until:
                return (-1,'','')
            confirmed=step.get('confirmed_at')
            return (step.get('stage')=='confirmed' and (key,confirmed) in queued_keys,
                    confirmed or '', step.get('last_at') or '')
        route_key=max(route_keys,key=display_priority)
        stage=stages.get(route_key,{})
        carried_until=stamp(stage.get('observation_expires_at'))
        if carried_until and now>=carried_until:
            stage={}
        elif carried_until and independent:
            result['expires_at']=min(watch_deadline(advice,condition),carried_until).isoformat()
        previous_review=current.get('reviewed_routes',{}).get(route_key) if same_plan else None
        event_id=stage.get('event_id',f'live-buy-confirmed:{advice["analyst"]}:{account["bank"]}:{identity}')
        status='monitoring' if eligible(condition,account.get('minimum_buy_grams')) else 'review_required'
        if current.get('plan_id')==identity and current.get('invalidated_at'):
            status='review_required'
        if stage.get('stage')=='above_cap':status='price_limit_exceeded'
        if stage.get('stage')=='pullback_lost':status='pullback_lost'
        if stage.get('stage')=='confirmed':
            queued=(route_key,stage['confirmed_at']) in queued_keys
            reviewed=stage.get('reviewed_confirmation_at')==stage['confirmed_at']
            if queued:
                status,event_id='queued',review['id']
            elif reviewed:
                status='reviewed'
            else:
                status='plan_reviewed' if previous_review else 'review_required'
                event_id=None
        elif stage.get('stage')=='episode_reviewed':
            status='reviewed'
            event_id=stage['event_id']
        elif status=='monitoring' and previous_review:
            status='plan_reviewed'
        if condition.get('bank_breakout'):
            from .jd_live_buy_watch import CONTINUATION_HOLD_MINUTES, CONTINUATION_MIN_QUOTES
            result.update(continuation_minutes=CONTINUATION_HOLD_MINUTES, continuation_min_quotes=CONTINUATION_MIN_QUOTES,
                          confirmation_mode=stage.get('confirmation_mode'))
        return {**result,'status':status,'confirmation_stage':stage.get('stage'),
                'triggered_at':stage.get('confirmed_at'),'last_quote_at':stage.get('last_at'),
                'last_price_cny':stage.get('last_price_cny'),'event_id':event_id if stage.get('confirmed_at') else None}
    if watch.get('event_id') and any(e['id']==watch['event_id'] for e in pending):result['status']='queued'
    elif watch.get('advice_id')==advice['id'] and watch.get('event_id') in events['covered']:result['status']='reviewed'
    elif watch.get('latched'):result['status']='latched'
    else:result['status']='monitoring'
    return result


def management_state(account,advice,item,now):
    if advice.get('action_status',advice['status'])!='current':return advice.get('action_status',advice['status'])
    expires=stamp((item.get('effective_replacement') or {}).get('expires_at'))
    if item['action']=='replace' and expires and expires<=now:return 'expired'
    if item.get('checks'):return 'review_required'
    part=account.get('pending_orders') or {}
    if part.get('status')!='fresh':return 'data_unavailable'
    target=next((o for o in part.get('records',[]) if o['order_key']==item['order_key'] and o['status'] in ('pending','partially_filled')),None)
    if not target or (stamp(target.get('expires_at')) and stamp(target['expires_at'])<=now):return 'order_inactive'
    return 'current'


def price_met(condition, price):
    if condition['condition'] == 'at_or_above':
        return price >= condition['price_cny']
    return price <= condition['price_cny']


def stayed_on_trigger_side(conn, bank, condition, after, point):
    from .jd_live_buy_watch import quote_tape

    return all(price_met(condition, buy) for _, buy, _ in quote_tape(conn, bank, after, point))


def unchanged_buy_observation(conn, bank, advice, condition, point):
    input_at = stamp(advice.get('input_quote_at'))
    input_price = advice.get('input_buy_quote_cny')
    return (input_at is not None and input_at <= point[0] and input_price is not None
            and price_met(condition, input_price)
            and stayed_on_trigger_side(conn, bank, condition, input_at, point))


def buy_observation_latched(conn, account, advice, condition, watch, now):
    """A bank price already seen by the model needs a new crossing to request a review."""
    quote = account['quote']
    point = (stamp(quote['quoted_at']), now, quote['buy_price_cny'], quote.get('sell_price_cny'))
    previous_at = stamp(watch.get('last_quote_at'))
    input_at = stamp(advice.get('input_quote_at'))
    if previous_at and (not input_at or previous_at >= input_at):
        return watch.get('latched') and stayed_on_trigger_side(conn, account['bank'], condition, previous_at, point)
    if not input_at or advice.get('input_buy_quote_cny') is None:
        return bool(watch.get('latched'))
    return unchanged_buy_observation(conn, account['bank'], advice, condition, point)


def monitor_prices(store,account,advice,now):
    from .ai_events import read,write
    from .controls import active,control
    from .jd_live_buy_guard import plain_buy_observation
    from .jd_live_readonly import PRODUCTS
    for condition in advice.get('review_conditions',[]):
        if condition.get('buy_review') or condition.get('bank_breakout'):continue
        state=condition_state(store.conn,account,advice,condition,now)
        if state['status'] not in ('monitoring','latched','queued','reviewed'):continue
        quote=account['quote'];price=quote.get(condition['quote_side']+'_price_cny')
        if not isinstance(price,(float,int)) or isinstance(price,bool) or price<=0:continue
        met=price_met(condition,price)
        # The collector and monitor can observe the same tick. Commit event + latch atomically.
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            switch=control(store.conn,advice['analyst'])
            if not active(store.conn,advice['analyst']) or (switch.get('changed_at') and advice['created_at']<=switch['changed_at']):continue
            key=watch_key(store.conn,advice['analyst'],account['bank'],condition);watch=read_watch(store.conn,key)
            if watch.get('last_quote_at') and stamp(quote['quoted_at'])<=stamp(watch['last_quote_at']):continue
            latched = watch.get('latched')
            if met and plain_buy_observation(condition):
                latched = buy_observation_latched(store.conn, account, advice, condition, watch, now)
            if met and not latched:
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
