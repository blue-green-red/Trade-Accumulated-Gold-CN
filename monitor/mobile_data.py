"""Small, read-only payloads for task-specific mobile screens. Legacy APIs stay intact."""
from collections import Counter
from datetime import datetime,timezone
import json

from .autonomous import summaries,enabled,exists
from .controls import active
from .jd_live_readonly import accounts as live_accounts
from .jd_live_conditions import stamp
from .model_visibility import hidden_models

LIVE_FIELDS = ('bank','name','status','connected','fetched_at','checked_at','message','official_url',
               'total_grams','sellable_grams','frozen_grams','cash_cny','cash_source','cash_declared_at',
               'average_cost_cny','holding_pnl_cny','holding_pnl_basis','quote','sell_fee_rate','pending_order_count',
               'risk_profile','component_times','coverage','order_submission_enabled')
PORTFOLIO_FIELDS = ('key','model','analyst','name','start_mode','started','started_at','budget','value','return',
                    'cash','grams','position_pct','paused','realized_pnl','unrealized_pnl','pnl_breakdown')


def live_summary(account):
    now=datetime.now(timezone.utc)
    result={k:account.get(k) for k in LIVE_FIELDS}
    result['advice_summary']=[]
    for advice in account.get('advice',[]):
        pending=sum(1 for p in advice.get('suggested_orders',[]) if not p.get('checks')
                    and stamp(p.get('expires_at')) and stamp(p['expires_at'])>now
                    and (p.get('execution_progress') or {}).get('status','not_observed')=='not_observed')
        result['advice_summary'].append({**{k:advice.get(k) for k in ('id','analyst','created_at','expires_at','status','action_status','action')},
                                        'suggested_order_count':pending if advice.get('action_status',advice.get('status'))=='current' else 0})
    return result


def message_history(conn,settings):
    """One logical sent message per row, with attempts and associated events underneath."""
    from .notification_outbox import visible_tasks
    hidden=hidden_models(conn);groups={}
    for raw in conn.execute('SELECT * FROM notification_deliveries ORDER BY id DESC LIMIT 100'):
        row=dict(raw)
        keys=json.loads(row.get('event_keys_json') or '[]')
        events=[dict(r) for r in conn.execute('SELECT key,title,ts FROM alerts WHERE key IN ('+','.join('?' for _ in keys)+')',keys)] if keys else []
        if any(m in (row['title']+' '+ ' '.join(keys)+' '+' '.join(e['title'] for e in events)).lower() for m in hidden):continue
        group=(row['category'],tuple(sorted(keys)) or (str(row['id']),))
        attempt={k:row.get(k) for k in ('id','ts','status','channel','provider_id','provider_code')}
        if group not in groups:
            groups[group]={**{k:row.get(k) for k in ('id','ts','title','status','category','channel','provider_id','provider_code')},
                           'events':events,'attempts':[]}
        groups[group]['attempts'].append(attempt)
    messages=list(groups.values())[:15]
    represented={e['key'] for m in messages for e in m['events']}
    for task in visible_tasks(conn,settings.ai_trading_mode):
        if any(m in (task['title']+' '+task['key']).lower() for m in hidden):continue
        matching=next((m for m in messages if any(e['key']==task['key'] for e in m['events'])),None)
        if matching:
            matching.setdefault('tasks',[]).append(task)
            priority={'uncertain':0,'sending':1,'retry':2,'pending':3,'failed':4,'cancelled':5}
            matching['status']=min(matching['tasks'],key=lambda t:priority.get(t['status'],6))['status']
            matching['next_attempt_at']=min((t['next_attempt_at'] for t in matching['tasks'] if t['status']=='retry'),default=None)
        elif task['key'] not in represented:
            row=conn.execute('SELECT updated_at FROM notification_outbox WHERE event_key=?',(task['key'],)).fetchone()
            messages.insert(0,{'id':'task:'+task['key'],'ts':row[0] if row else None,'title':task['title'],
                              'status':task['status'],'category':'pending','events':[],'attempts':[],
                              'attempt_count':task['attempts'],'next_attempt_at':task['next_attempt_at']})
    return messages[:25]


def scoped_data(conn,settings,result,page,view,snap,quote,valuation,price):
    from .web import get_state,ai_section
    if page=='strategies' and (view.endswith('-list') or view.startswith(('live/','simulation/'))):
        if view.startswith('live/'):
            _,bank,section=view.split('/')
            rows=live_accounts(conn,include_advice=section!='holdings',selected_bank=bank)
            if not rows:
                result.update(account_view='live',bank_view=section,live_account=None)
                return True
            account=rows[0];value={k:account.get(k) for k in LIVE_FIELDS}
            if section=='holdings':
                value.update({k:account.get(k) for k in ('cash_balance_cny','cash_scope','holding_details')})
            elif section=='advice':
                value.update({k:account.get(k) for k in ('advice','pending_orders','review_status')})
            elif section=='orders':
                value.update(pending_orders=account.get('pending_orders'),order_history=account.get('order_history'),
                             advice=[{k:a.get(k) for k in ('id','analyst','created_at','expires_at','status','action_status','schema_version','action','order_management','review_conditions','execution_progress')} for a in account.get('advice',[])])
            else:
                value.update({k:account.get(k) for k in ('order_history','holding_details','closed_positions','recent_trade')})
                value['advice_history']=sorted(account.get('advice',[])+account.get('advice_history',[]),key=lambda a:a['id'],reverse=True)
            result.update(account_view='live',bank_view=section,live_account=value)
            result['live_readonly_sync']=get_state(conn,'jd_live_sync_status') or {}
            result['live_readonly_refresh']=get_state(conn,'jd_live_refresh') or {}
            return True
        if view.startswith('simulation/'):
            key=view.removeprefix('simulation/')
            owned=[a for a in summaries(conn,settings,price) if a['key']==key] if enabled(settings) and exists(conn) else []
            result.update(account_view='simulation',portfolio=owned[0] if owned and active(conn,key.split(':')[0]) else None)
            a=result['portfolio']
            if a and a['grams']<=1e-9 and (settings.ai_trading_mode=='risk_six_account' or a['start_mode'].startswith('short_')):
                from .short_review import wait_review
                feedback=wait_review(conn,a.get('plan'),quote,datetime.now(timezone.utc).isoformat())
                if feedback:a['short_wait_review']=feedback
            return True
        rows=live_accounts(conn) if view!='simulation-list' else []
        mode=('live' if any(a.get('fetched_at') for a in rows) else 'simulation') if view=='auto-list' else view.removesuffix('-list')
        result['account_view']=mode
        if mode=='live':
            result.update(live_readonly=[live_summary(a) for a in rows],live_readonly_sync=get_state(conn,'jd_live_sync_status') or {},
                          live_readonly_refresh=get_state(conn,'jd_live_refresh') or {})
        else:
            data=summaries(conn,settings,price) if enabled(settings) and exists(conn) else []
            result['leaderboard']=[{k:a.get(k) for k in PORTFOLIO_FIELDS} for a in data]
        return True
    if page=='ai' and view in ('research','control','evaluation'):
        result['ai_view']=view
        result['cadence']={'review_minutes':settings.ai_short_review_minutes,'min_move_pct':settings.ai_periodic_min_move_pct,'max_idle_minutes':settings.ai_review_max_idle_minutes}
        if view=='control':return True
        data=ai_section(conn,settings)
        fields=(('analyst','name','ts','model','bias_short','bias_long','confidence','p_up_3d','p_down_3d','p_up_3m','p_down_3m',
                 'summary','drivers','sources','upcoming_events','hard_risk_event','hard_risk_note','_price_guard','_snapshot_at','_started_at','_completed_at')
                if view=='research' else ('analyst','name','ts','model','_performance','wave_input','_snapshot_at','_started_at','_completed_at'))
        analysts=[]
        for original in data['analysts']:
            value={k:original.get(k) for k in fields}
            if view=='research':
                value['action_counts']=dict(Counter(d.get('action') for d in (original.get('account_decisions') or {}).values()))
                value['live_advice_count']=sum(v is not None for v in (original.get('live_account_advice') or {}).values())
            analysts.append(value)
        result['ai']={'enabled':data['enabled'],'analysts':analysts}
        if view=='evaluation':result['ai_inputs']=get_state(conn,'ai_inputs')
        return True
    if page=='notifications' and view in ('settings','history'):
        result['notification_view']=view
        result['notifications']={'configured':bool(settings.pushplus_token or settings.serverchan_sendkey),
                                 'channel':'PushPlus' if settings.pushplus_token else 'Server酱' if settings.serverchan_sendkey else None}
        if view=='settings':
            from .notification_control import visible_subscriptions
            result['notifications']['subscriptions']=visible_subscriptions(conn,settings.ai_trading_mode)
        else:result['notifications']['messages']=message_history(conn,settings)
        return True
    if page=='me':
        result['preferences']={'notification_configured':bool(settings.pushplus_token or settings.serverchan_sendkey),
                               'channel':'PushPlus' if settings.pushplus_token else 'Server酱' if settings.serverchan_sendkey else None}
        return True
    if page=='overview' and view=='summary':
        from .page_data import _totals,_plans
        result['execution']={'quote':quote,'valuation_quote':valuation,'ready':quote is not None,'status':snap.get('jd_quote_status')}
        result['snapshot']={k:snap.get(k) for k in ('ts','market_open','jd_session')}
        result['live_readonly']=[live_summary(a) for a in live_accounts(conn)]
        data=summaries(conn,settings,price) if enabled(settings) and exists(conn) else []
        result['comparison']={'models':_totals(conn,settings,data)}
        plans=_plans(conn,settings)
        result['pending_simulation_count']=len(plans['plans'])
        result['hard_events_24h']=((get_state(conn,'web_macro') or {}).get('hard_events_24h') or [])[:1]
        return True
    return False
