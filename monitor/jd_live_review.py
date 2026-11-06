"""Coalesced account/order changes, data recovery and expiry review per consecutive plan."""
import math
from datetime import datetime,timezone
from .jd_live_readonly import accounts,PRODUCTS,age
from .controls import MODELS,active
from .ai_events import enqueue


def request_reviews(store,now=None,after_sync=False,settings=None):
    now=now or datetime.now(timezone.utc)
    from .config import Settings
    config=settings or Settings()
    threshold=config.ai_periodic_min_move_pct
    from .trading_hours import trading_session
    if not trading_session(now)['is_open']:return
    sync=store.get_state('jd_live_sync_status',{})
    if not after_sync and sync.get('status')=='running' and age(sync.get('at'),now)<180:return
    for account in accounts(store.conn,now):
        bank=account['bank']
        for model in MODELS:
            if not active(store.conn,model) or not store.get_state('ai_status:'+model,{}).get('available'):continue
            watch_key=f'live-data-ready:{model}:{bank}'
            ready=account['connected'] and (account.get('quote') or {}).get('status')=='fresh'
            old_ready=store.get_state(watch_key,False)
            if old_ready!=ready:store.set_state(watch_key,ready)
            if not account['connected']:continue
            advice=next((v for v in account['advice'] if v['analyst']==model),None)
            data={'bank':bank,'snapshot_revision':account['revision']}
            if not advice or advice.get('snapshot_revision')!=account['revision']:
                old=(advice or {}).get('input_state') or {}
                changes=[k for k in ('total_grams','cash_cny','pending_order_count','recent_trade') if k in old and old[k]!=account.get(k)]
                labels={'total_grams':'持仓','cash_cny':'余额','pending_order_count':'条件单数量','recent_trade':'最近交易'}
                reason='、'.join(labels[k] for k in changes) or '账户或条件单'
                enqueue(store.conn,model,f'真实{reason}变化：{PRODUCTS[bank][1]}' if advice else f'真实账户首次分析：{PRODUCTS[bank][1]}',0,
                        event_id=f'live-account:{bank}:{account["revision"]}:after:{advice["id"] if advice else "initial"}',payload={**data,'changes':changes})
            elif advice.get('schema_version',1)<2:
                enqueue(store.conn,model,f'真实账户结构化挂单与复评建议升级：{PRODUCTS[bank][1]}',0,
                        event_id=f'live-schema-v2:{model}:{bank}:{advice["id"]}',payload=data)
            elif ready and (advice.get('input_data_status') or {}).get('account') not in (None,'fresh'):
                from .jd_live_preflight import missing_inputs
                if bank not in (missing_inputs(store.conn,now) or {}):
                    enqueue(store.conn,model,f'真实账户分析数据恢复：{PRODUCTS[bank][1]}',0,
                            event_id=f'live-inputs-recovered:{model}:{bank}:{advice["id"]}',payload=data)
            elif ready and (account.get('pending_orders') or {}).get('status')=='fresh' and (account.get('pending_orders') or {}).get('complete') and (
                    (advice.get('input_data_status') is not None and
                     (advice['input_data_status'].get('orders')!='fresh' or advice['input_data_status'].get('orders_complete') is False)) or
                    any('挂单明细不是新鲜数据' in warning for warning in advice.get('checks',[]))):
                enqueue(store.conn,model,f'真实账户挂单明细恢复：{PRODUCTS[bank][1]}',0,
                        event_id=f'live-orders-recovered:{model}:{bank}:{advice["id"]}',
                        payload={**data,'advice_id':advice['id']})
            elif ready and not old_ready and (advice['action']=='wait' or advice.get('input_quote_status') not in (None,'fresh')):
                enqueue(store.conn,model,f'真实账户报价恢复：{PRODUCTS[bank][1]}',0,
                        event_id=f'live-recovered:{model}:{bank}:{advice["id"]}',payload=data)
            if advice:
                if ready:
                    from .jd_live_expiry import request_expiry_review
                    request_expiry_review(store,account,advice,now)
                if ready and not (advice.get('input_strategy_review') or {}).get('revalidate_unchanged_plan'):
                    from .jd_live_feedback import strategy_review
                    feedback=strategy_review(store.conn,account,model,now)
                    if feedback and feedback['revalidate_unchanged_plan']:
                        enqueue(store.conn,model,f'真实账户连续弱化但方案未变，复核旧价位：{PRODUCTS[bank][1]}',0,
                                event_id=f'live-plan-review:v1:{model}:{bank}:{advice["id"]}',
                                payload={**data,'advice_id':advice['id'],
                                         'same_plan_rounds':feedback['same_plan_rounds'],'weakened_rounds':feedback['weakened_rounds']})
                request_bank_move(store,account,advice,threshold,now,config.ai_short_review_minutes)
                from .jd_live_conditions import monitor_prices
                monitor_prices(store,account,advice,now)


def request_bank_move(store,account,advice,threshold,now,interval=0):
    """One event per bank/model's last observed input; the existing worker coalesces all triggers."""
    from .jd_live_feedback import input_quote,price
    from .jd_live_conditions import stamp
    quote=account.get('quote') or {}
    current=price(quote.get('sell_price_cny'))
    if not account.get('connected') or quote.get('status')!='fresh' or current is None:return
    if not math.isfinite(threshold) or threshold<0 or interval<0:return
    if age(advice.get('created_at'),now)<interval*60:return
    anchor=input_quote(store.conn,advice)
    if not anchor or not stamp(quote.get('quoted_at')) or stamp(quote['quoted_at'])<stamp(anchor['quoted_at']):return
    change=(current/anchor['price_cny']-1)*100
    if abs(change)+1e-9<threshold:return
    model=advice['analyst'];bank=account['bank']
    enqueue(store.conn,model,f'真实账户人民币金价变化{change:+.2f}%：{PRODUCTS[bank][1]}',interval,
            event_id=f'live-move:{model}:{bank}:{advice["id"]}',
            payload={'source':'银行报价复查','bank':bank,'advice_id':advice['id'],
                     'previous_quote_cny':anchor['price_cny'],'previous_quote_at':anchor['quoted_at'],
                     'current_quote_cny':current,'current_quote_at':quote['quoted_at'],
                     'change_pct':round(change,3)})
