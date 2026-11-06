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
    return {'previous_input_price_cny':anchor['price_cny'] if anchor else None,
            'move_since_previous_pct':round((current/anchor['price_cny']-1)*100,3) if current and anchor else None,
            'lookback_rounds':len(history),'same_plan_rounds':len(unchanged),'weakened_rounds':weakened,
            'thesis_statuses':states,
            'revalidate_unchanged_plan':len(unchanged)>=2 and weakened>=2}
