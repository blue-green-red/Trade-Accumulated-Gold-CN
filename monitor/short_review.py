"""Read-only feedback for waiting and continuously renewed unfilled limit buys."""
import json
from datetime import datetime


def entry_terms(decision):
    return {k:decision.get(k) for k in ('action','execution','amount_cny','limit_price_cny',
                                      'min_execution_price_cny','max_execution_price_cny')}


def wait_review(conn, plan, quote, as_of):
    if not plan:return None
    decision=plan['decision'];action=decision.get('action')
    limit_buy=action=='buy' and decision.get('execution')=='limit' and plan.get('status') in ('pending','expired','cancelled')
    if action!='wait' and not limit_buy:return None
    row=conn.execute('SELECT snapshot_at,ts,view_json FROM ai_views WHERE rowid=?',(plan['view_id'],)).fetchone()
    if not row:return None
    since=row['snapshot_at'] or row['ts']
    if conn.execute('SELECT julianday(?)>julianday(?)',(since,as_of)).fetchone()[0]:return None
    rounds=1;waiting_since=since
    if limit_buy:
        # A renewal changes the plan id/deadline, not how long this entry has been waiting.
        last_fill=conn.execute('SELECT MAX(julianday(ts)) FROM ai_portfolio_fills WHERE account=? AND julianday(ts)<=julianday(?)',
                               (plan['account'],as_of)).fetchone()[0]
        candidates=conn.execute('SELECT p.view_id,p.created_at,p.decision_json,p.status FROM ai_plans p WHERE p.account=? AND p.generation=? '
            'AND p.id<=? AND julianday(p.created_at)<=julianday(?) AND (? IS NULL OR julianday(p.created_at)>=?) ORDER BY p.id DESC',
            (plan['account'],plan['generation'],plan['id'],as_of,last_fill,last_fill)).fetchall()
        rounds=0;anchor=None
        for candidate in candidates:
            if candidate['status'] not in ('pending','expired','cancelled') or entry_terms(json.loads(candidate['decision_json']))!=entry_terms(decision):break
            anchor=candidate;rounds+=1
        if anchor is None:return None
        row=conn.execute('SELECT snapshot_at,ts,view_json FROM ai_views WHERE rowid=?',(anchor['view_id'],)).fetchone()
        if not row:return None
        since=row['snapshot_at'] or row['ts'];waiting_since=anchor['created_at']
    view=json.loads(row['view_json'] or '{}')
    previous=(view.get('_price_guard') or {}).get('jd')
    current=quote.get('price') if quote else None
    sampled=conn.execute('SELECT MIN(jd_price),MAX(jd_price),COUNT(*) FROM ticks '
        'WHERE julianday(ts)>=julianday(?) AND julianday(ts)<=julianday(?) AND jd_price>0 AND market_open=1',
        (waiting_since,as_of)).fetchone()
    value={'since_snapshot_at':since,'waiting_since':waiting_since,'as_of':as_of,
        'waiting_minutes':round(max(0,(datetime.fromisoformat(as_of)-datetime.fromisoformat(waiting_since)).total_seconds()/60),1),
        'previous_quote_cny':previous,'current_quote_cny':current,
        'change_pct':round((current/previous-1)*100,3) if previous and current else None,
        'sampled_low_cny':sampled[0],'sampled_high_cny':sampled[1],'samples':sampled[2],
        'note':'京东报价采样范围，不是完整最高最低；达到价格条件不证明当时委托有效或已成交，以成交账本为准，不自动追价。'}
    if limit_buy:
        low=decision.get('min_execution_price_cny');high=min(decision['limit_price_cny'],decision.get('max_execution_price_cny') or decision['limit_price_cny'])
        hits=conn.execute('SELECT COUNT(*) FROM ticks WHERE julianday(ts)>=julianday(?) AND julianday(ts)<=julianday(?) '
                         'AND market_open=1 AND jd_price>0 AND jd_price BETWEEN ? AND ?',
                         (waiting_since,as_of,low,high)).fetchone()[0]
        value.update(entry_limit_cny=decision['limit_price_cny'],same_entry_rounds=rounds,entry_condition_samples=hits)
    return value
