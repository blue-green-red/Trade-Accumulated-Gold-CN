"""Small, model-scoped facts for rechecking unchanged real-account plans."""
import json
import math
from datetime import datetime, timezone

from .jd_live_conditions import stamp
from .jd_live_proposals import proposal_signature
from .jd_live_semantics import immediate_signature,management_signature


def price(value):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return value if math.isfinite(value) and value>0 else None


def input_quote(conn,advice):
    """Use the observed bank input, never an AI output price or another product's quote."""
    if advice.get('input_quote_status') not in (None,'fresh'):return None
    if 'input_quote_cny' in advice:
        value=price(advice['input_quote_cny']);at=stamp(advice.get('input_quote_at'))
        return {'price_cny':value,'quoted_at':advice['input_quote_at']} if value and at else None
    # Legacy advice did not persist the anchor. Recover only from its exact archived shared input.
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ai_views' not in tables or 'run_id' not in {r[1] for r in conn.execute('PRAGMA table_info(ai_views)')}:return None
    row=conn.execute('SELECT v.run_id,a.bank,a.analyst FROM jd_live_advice a JOIN ai_views v ON v.rowid=a.view_id WHERE a.id=?',(advice['id'],)).fetchone()
    if not row or not row[0] or row[2]!=advice['analyst']:return None
    saved=conn.execute('SELECT value FROM state WHERE key=?',('ai_input:'+row[0],)).fetchone()
    if not saved:return None
    source=(((json.loads(saved[0]).get('context') or {}).get('live_readonly_accounts') or {}).get('accounts') or {}).get(row[1]) or {}
    quote=source.get('quote') or {}
    value=price(quote.get('sell_price_cny'));at=stamp(quote.get('quoted_at'))
    if source.get('status')!='fresh' or quote.get('status')!='fresh' or not value or at is None:return None
    return {'price_cny':value,'quoted_at':quote['quoted_at']}


def plan_signature(value):
    # Reference quote and prose change each round; actual trade terms determine whether the plan changed.
    from .jd_live_entry import signature as entry_signature
    return json.dumps([immediate_signature(value),proposal_signature(value),management_signature(value),entry_signature(value)],sort_keys=True)


def entry_reassessment(conn,history):
    """Recent observed candidate drift, not a trading signal or an instruction to reprice."""
    if not history:return None
    rows=[]
    for value in history[:3]:
        if value.get('snapshot_revision')!=history[0].get('snapshot_revision') or value.get('action') not in ('hold','wait'):break
        if any(o.get('kind')=='limit_buy' and not o.get('checks') for o in value.get('suggested_orders',[])):break
        review=next((r for r in value.get('limit_buy_reviews',[]) if r.get('decision') in ('needs_review','not_worthwhile','unavailable') and not r.get('checks') and price(r.get('entry_price_cny'))),None)
        anchor=input_quote(conn,value)
        entry=price((review or {}).get('entry_price_cny'))
        if not entry or not anchor or entry>=anchor['price_cny']:break
        rows.append({'review':review,'entry':entry,'anchor':anchor,'discount':(1-entry/anchor['price_cny'])*100})
    if len(rows)<2:return None
    latest,previous=rows[:2]
    same=0
    for row in rows:
        if abs(row['discount']-latest['discount'])>.02:break
        same+=1
    at=stamp(latest['anchor']['quoted_at']);deadline=stamp(previous['review'].get('expires_at'))
    return {'lookback_rounds':len(rows),'deferred_candidate_rounds':len(rows),
            'latest_discount_pct':round(latest['discount'],3),'same_discount_rounds':same,
            'candidate_move_pct':round((latest['entry']/previous['entry']-1)*100,3),
            'input_price_move_pct':round((latest['anchor']['price_cny']/previous['anchor']['price_cny']-1)*100,3),
            'previous_candidate_reached':(at<deadline and latest['anchor']['price_cny']<=previous['entry']) if at and deadline else None}


def strategy_review(conn,account,model,now=None):
    now=now or datetime.now(timezone.utc)
    history=sorted([v for v in account.get('advice',[])+account.get('advice_history',[]) if v['analyst']==model
                    and stamp(v.get('created_at')) and stamp(v['created_at'])<=now],key=lambda v:v['id'],reverse=True)[:3]
    if not history:return None
    latest=history[0];anchor=input_quote(conn,latest)
    quote=account.get('quote') or {}
    current=price(quote.get('sell_price_cny')) if account.get('connected') and quote.get('status')=='fresh' else None
    current_at=stamp(quote.get('quoted_at'))
    if current_at is None or (anchor and current_at<stamp(anchor['quoted_at'])):current=None
    unchanged=[]
    for item in history:
        if item.get('snapshot_revision')!=latest.get('snapshot_revision') or plan_signature(item)!=plan_signature(latest):break
        unchanged.append(item)
    states=[(v.get('reassessment') or {}).get('thesis_status') for v in unchanged]
    weakened=sum(s in ('weakened','invalidated') for s in states)
    result={'previous_input_price_cny':anchor['price_cny'] if anchor else None,
            'move_since_previous_pct':round((current/anchor['price_cny']-1)*100,3) if current and anchor else None,
            'lookback_rounds':len(history),'same_plan_rounds':len(unchanged),'weakened_rounds':weakened,
            'thesis_statuses':states,
            'revalidate_unchanged_plan':len(unchanged)>=2 and weakened>=2}
    entry=entry_reassessment(conn,history)
    if entry:result['entry_reassessment']=entry
    if latest.get('participation_review') is not None:result['participation_review']=latest['participation_review']
    return result
