"""Daily and position milestones from persisted analysis inputs, without sending messages."""
import json
from datetime import timedelta,timezone
from .config import BJ
from .jd_live_conditions import stamp
from .jd_live_semantics import notification_parts
from .trade_costs import number

LABELS={'daily_first':'当日首次真实账户分析','position_changed':'仓位或确认资金变化后的首次复评'}
THRESHOLDS={'total_grams':.01,'cash_cny':1.}


def eligible(value):
    """Unavailable or wholly blocked advice does not consume a milestone."""
    if value.get('input_quote_status')!='fresh':return False
    part=notification_parts(value)
    if part is None:return False
    return not (part.get('notification_partial') and part['action'] in ('hold','wait')
                and not part.get('suggested_orders')
                and not any(o['action'] in ('cancel','replace') for o in part.get('order_management',[])))


def reasons(conn,model,bank,advice_id,created_at,value):
    at=stamp(created_at)
    if at is None or not eligible(value):return []
    day=at.astimezone(BJ).date()
    start=at.astimezone(BJ).replace(hour=0,minute=0,second=0,microsecond=0)
    # SQLite rounds sub-millisecond timestamps; verify the local date in Python too.
    lower=(start-timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
    upper=(start+timedelta(days=1,seconds=1)).astimezone(timezone.utc).isoformat()
    seen_day=False
    for row in conn.execute(
        'SELECT created_at,advice_json FROM jd_live_advice WHERE bank=? AND analyst=? AND id<? '
        'AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) ORDER BY id DESC',
        (bank,model,advice_id,lower,upper)):
        previous_at=stamp(row[0])
        if previous_at and previous_at.astimezone(BJ).date()==day and eligible(json.loads(row[1])):
            seen_day=True;break
    result=[] if seen_day else ['daily_first']
    state=value.get('input_state') or {}
    current={k:number(state.get(k)) for k in THRESHOLDS}
    needed={k for k,v in current.items() if v is not None};baseline={}
    if not needed:return result
    for row in conn.execute(
        'SELECT advice_json FROM jd_live_advice WHERE bank=? AND analyst=? AND id<? ORDER BY id DESC',
        (bank,model,advice_id)):
        old=json.loads(row[0])
        if not eligible(old):continue
        old_state=old.get('input_state') or {}
        for key in needed-baseline.keys():
            n=number(old_state.get(key))
            if n is not None:baseline[key]=n
        if needed<=baseline.keys():break
    if any(abs(current[k]-v)+1e-9>=THRESHOLDS[k] for k,v in baseline.items()):
        result.append('position_changed')
    return result
