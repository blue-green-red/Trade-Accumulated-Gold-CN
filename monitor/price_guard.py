"""Sticky market invalidation for an in-flight six-account decision, without restarting CLI jobs."""
import json
import math
from datetime import datetime
from .store import utc_now


def read(conn,key):
    row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn,key,value):
    conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (key,json.dumps(value,ensure_ascii=False),utc_now().isoformat()))


def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and n>0 else None
    except (TypeError,ValueError):return None


def check(state,market):
    if state.get('invalidated_at'):return state
    if market.get('at') and state.get('snapshot_at') and datetime.fromisoformat(market['at'])<datetime.fromisoformat(state['snapshot_at']):return state
    for source in ('jd','xauusd'):
        old,current=number(state.get(source)),number(market.get(source))
        if old and current:
            move=abs(current/old-1)*100
            if move+1e-9>=state['threshold_pct']:
                state.update(invalidated_at=utc_now().isoformat(),source=source,observed_price=current,move_pct=move,
                             reason=f'分析期间行情异动：{source} 较输入变化{move:.3f}%，达到{state["threshold_pct"]:g}%，旧决策不执行')
                break
    return state


def register(conn,model,attempt,context,settings):
    quote=context.get('jd_accumulation_quote') or (context.get('trading_accounts') or {}).get('reference_quote') or {}
    state={'attempt':attempt,'jd':number(quote.get('price')),'xauusd':number(context.get('xauusd')),
           'snapshot_at':context.get('snapshot_at'),'threshold_pct':settings.ai_inflight_max_move_pct}
    check(state,read(conn,'ai_live_market'))
    save(conn,'ai_price_guard:'+model,state)
    return state


def observe(conn,snap,settings):
    from .execution import execution_quote
    quote=execution_quote(snap)
    market={'jd':quote['price'] if quote else None,'xauusd':number(snap.get('xauusd')),'at':utc_now().isoformat()}
    save(conn,'ai_live_market',market)
    invalid=[]
    for model in settings.ai_analysts:
        key='ai_price_guard:'+model;state=read(conn,key)
        if not state:continue
        run=conn.execute('SELECT status FROM ai_runs WHERE id=?',(state['attempt'],)).fetchone()
        if not run or run[0]!='running':continue
        check(state,market);save(conn,key,state)
        if state.get('invalidated_at'):invalid.append((model,state))
    return invalid


def finish(conn,model,attempt):
    state=read(conn,'ai_price_guard:'+model)
    if state.get('attempt')!=attempt:return {}
    check(state,read(conn,'ai_live_market'))
    save(conn,'ai_price_guard:'+model,state)
    return state


def request_refresh(conn,model,state):
    from .ai_events import enqueue
    from .controls import active
    if state.get('invalidated_at') and active(conn,model):
        enqueue(conn,model,state['reason'],0,f'price-guard:{model}:{state["attempt"]}',{'source':'分析价格校验','move_pct':state['move_pct']})
