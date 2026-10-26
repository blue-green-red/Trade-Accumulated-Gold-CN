"""Small, read-only feedback on the market since a short account last waited."""
import json


def wait_review(conn, plan, quote, as_of):
    if not plan or plan['decision']['action'] != 'wait':
        return None
    row=conn.execute('SELECT snapshot_at,ts,view_json FROM ai_views WHERE rowid=?',(plan['view_id'],)).fetchone()
    if not row:
        return None
    since=row['snapshot_at'] or row['ts']
    if conn.execute('SELECT julianday(?)>julianday(?)',(since,as_of)).fetchone()[0]:
        return None
    view=json.loads(row['view_json'] or '{}')
    previous=(view.get('_price_guard') or {}).get('jd')
    current=quote.get('price') if quote else None
    sampled=conn.execute('SELECT MIN(jd_price),MAX(jd_price),COUNT(*) FROM ticks '
        'WHERE julianday(ts)>=julianday(?) AND julianday(ts)<=julianday(?) AND jd_price>0',
        (since,as_of)).fetchone()
    return {'since_snapshot_at':since,'as_of':as_of,'previous_quote_cny':previous,
        'current_quote_cny':current,'change_pct':round((current/previous-1)*100,3) if previous and current else None,
        'sampled_low_cny':sampled[0],'sampled_high_cny':sampled[1],'samples':sampled[2],
        'note':'有效京东报价的采样区间，不是完整逐笔最高最低；上次入场条件见last_plan.decision.invalidation，须结合本轮信号判断是否出现，不自动触发交易。'}
