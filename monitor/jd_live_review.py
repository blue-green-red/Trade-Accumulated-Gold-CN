"""Coalesced account/order changes, data recovery and one expiry review per actionable advice."""
import json
from datetime import datetime,timezone
from .jd_live_readonly import accounts,PRODUCTS,age
from .controls import MODELS,active
from .ai_events import enqueue


def request_reviews(store,now=None,after_sync=False):
    now=now or datetime.now(timezone.utc)
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
            elif ready and not old_ready and (advice['action']=='wait' or advice.get('input_quote_status') not in (None,'fresh')):
                enqueue(store.conn,model,f'真实账户报价恢复：{PRODUCTS[bank][1]}',0,
                        event_id=f'live-recovered:{model}:{bank}:{advice["id"]}',payload=data)
            elif ready and advice['status']=='expired' and (advice['action'] in ('buy','sell') or advice.get('review_conditions') or any(o['action'] in ('cancel','replace') for o in advice.get('order_management',[]))):
                progress=advice.get('execution_progress') or {}
                if advice.get('review_conditions') or advice.get('order_management') or progress.get('status') not in ('pending','partially_filled','filled','cancelled','expired'):
                    enqueue(store.conn,model,f'真实建议到期复评：{PRODUCTS[bank][1]}',0,
                            event_id=f'live-expired:{model}:{bank}:{advice["id"]}',payload={**data,'advice_id':advice['id']})
            if advice:
                from .jd_live_conditions import monitor_prices
                monitor_prices(store,account,advice,now)
