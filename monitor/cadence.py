"""短线驱动的共同复查节奏，以及涵盖成功/失败/取消的调用预算。"""
import json
import math
from datetime import datetime
from .config import BJ

URGENT_PREFIXES = ("calendar:","jin10:","portfolio-fill:","portfolio-init:")


def usage(conn, model, now):
    day=now.astimezone(BJ).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(now.tzinfo).isoformat()
    has_runs=conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_runs'").fetchone()
    has_views=conn.execute("SELECT 1 FROM sqlite_master WHERE name='ai_views'").fetchone()
    if not has_views: return 0
    if not has_runs:
        return conn.execute("SELECT COUNT(*) FROM ai_views WHERE analyst=? AND ts>=?",(model,day)).fetchone()[0]
    new=conn.execute("SELECT COUNT(*) FROM ai_runs WHERE analyst=? AND started_at>=?",(model,day)).fetchone()[0]
    old=conn.execute("SELECT COUNT(*) FROM ai_views v WHERE analyst=? AND ts>=? AND NOT EXISTS(SELECT 1 FROM ai_runs r WHERE r.view_rowid=v.rowid)",(model,day)).fetchone()[0]
    return new+old


def allowed(settings, conn, model, now, event_ids=(), manual=False):
    if manual: return True
    used=usage(conn,model,now)
    maximum=max(1,settings.ai_max_runs_per_day)
    reserve=min(maximum-1,max(0,settings.ai_urgent_reserve))
    if used >= maximum: return False
    if used < maximum-reserve: return True
    event_ids=tuple(event_ids)
    if any(key.startswith(URGENT_PREFIXES) for key in event_ids): return True
    from .jd_live_path import queued_exit_protection
    return queued_exit_protection(conn,model,event_ids,now)


def request_periodic(settings, store, analysts, snap, now):
    interval=settings.ai_short_review_minutes
    if settings.ai_trading_mode not in ("six_account","four_account","risk_six_account","live_only") or interval<0: return
    for name,analyst in analysts.items():
        row=store.conn.execute("SELECT rowid,ts,xauusd FROM ai_views WHERE analyst=? AND error IS NULL ORDER BY rowid DESC LIMIT 1",(name,)).fetchone()
        if not row: continue
        age=(now-datetime.fromisoformat(row["ts"])).total_seconds()/60
        if age<interval: continue
        try:
            old,current=float(row["xauusd"]),float(snap["xauusd"])
            if old<=0 or current<=0 or not math.isfinite(current+old): continue
        except (TypeError,ValueError,KeyError): continue
        change=abs(current/old-1)*100
        if change<settings.ai_periodic_min_move_pct and age<settings.ai_review_max_idle_minutes: continue
        analyst.request_refresh(f"周期复查：距上次{age:.0f}分钟，金价变化{change:.2f}%",interval,
            event_id=f"periodic:{name}:{row['rowid']}",payload={"source":"周期复查","change_pct":round(change,3)})


def request_market_move(store, analysts, move, now):
    """Keep one persistent event per continuous directional movement."""
    key = 'ai_market_move_episode'
    episode = store.get_state(key, {})
    if not move:
        if episode:
            store.set_state(key, {})
        return
    event, current = move
    direction = event['key'].split(':', 1)[1]
    window = current['window_minutes']
    if episode.get('direction') != direction or (episode.get('data') or {}).get('window_minutes') != window:
        data = {**current, 'first_seen_at': now.isoformat()}
        episode = {'direction': direction, 'id': f'market-move:{direction}:{now.isoformat()}', 'data': data}
        store.set_state(key, episode)
    reason = f"{window}分钟{'急涨' if direction == 'up' else '急跌'}"
    for analyst in analysts.values():
        analyst.request_refresh(reason, min_gap_minutes=30, event_id=episode['id'], payload=episode['data'])
