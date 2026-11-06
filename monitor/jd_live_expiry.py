"""One expiry review for each consecutive economic plan, persisted across processes."""
import hashlib
import json

from .ai_events import read,write
from .controls import control
from .jd_live_feedback import plan_signature


def identity(value,revision):
    conditions=sorted(json.dumps({k:c.get(k) for k in
        ('condition','price_cny','quote_side','order_key')},sort_keys=True)
        for c in value.get('review_conditions',[]))
    return hashlib.sha256(json.dumps([revision,plan_signature(value),conditions],sort_keys=True).encode()).hexdigest()


def request_expiry_review(store,account,advice,now):
    """Observe every plan transition; enqueue atomically only on its first actionable expiry."""
    from .jd_live_readonly import PRODUCTS,enabled_banks
    model=advice['analyst'];bank=account['bank'];conn=store.conn
    if advice.get('snapshot_revision')!=account['revision'] or advice.get('schema_version',1)<2:return
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        switch=control(conn,model)
        if not switch['enabled'] or bank not in enabled_banks(conn):return
        if switch.get('changed_at') and advice['created_at']<=switch['changed_at']:return
        latest=conn.execute('SELECT MAX(id) FROM jd_live_advice WHERE bank=? AND analyst=?',(bank,model)).fetchone()[0]
        if latest!=advice['id']:return
        key=f'live-expiry-review:{model}:{bank}:{switch["generation"]}'
        row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        watch=json.loads(row[0]) if row else {}
        if watch.get('last_advice_id',0)>advice['id']:return
        # Catch intermediate transitions even if another round completed between collector polls.
        rows=conn.execute('SELECT id,snapshot_revision,advice_json FROM jd_live_advice WHERE bank=? AND analyst=? AND id>? AND id<=? ORDER BY id',
                          (bank,model,watch.get('last_advice_id',0),advice['id'])).fetchall()
        for item in rows:
            signature=identity(json.loads(item[2]),item[1])
            if watch.get('signature')!=signature:
                watch={'signature':signature,'origin_advice_id':item[0],'reviewed':False}
            watch['last_advice_id']=item[0]
        actionable=advice['action'] in ('buy','sell') or advice.get('review_conditions') or any(
            o['action'] in ('cancel','replace') for o in advice.get('order_management',[]))
        progress=(advice.get('execution_progress') or {}).get('status')
        needs_review=advice.get('review_conditions') or advice.get('order_management') or progress not in (
            'pending','partially_filled','filled','cancelled','expired')
        if advice['status']=='expired' and actionable and needs_review and not watch.get('reviewed'):
            event_id=f'live-expired:v2:{model}:{bank}:{switch["generation"]}:{watch["origin_advice_id"]}'
            events=read(conn,model)
            if event_id not in events['covered'] and not any(e['id']==event_id for e in events['pending']):
                events['pending'].append({'id':event_id,'reason':f'真实建议到期复评：{PRODUCTS[bank][1]}',
                    'min_gap_minutes':0,'observed_at':now.isoformat(),
                    'data':{'bank':bank,'snapshot_revision':account['revision'],'advice_id':advice['id']}})
                write(conn,model,events)
            watch['reviewed']=True
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (key,json.dumps(watch),now.isoformat()))
