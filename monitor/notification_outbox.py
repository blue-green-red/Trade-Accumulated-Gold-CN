"""Durable, bounded retries for confirmed provider failures. Never replay uncertain sends."""
import json
from datetime import datetime,timedelta
from .store import utc_now
from .notification_control import selected_events, event_topic, topics_for_mode


def visible_tasks(conn, mode, limit=10):
    allowed = topics_for_mode(mode,conn)
    result = []
    for row in conn.execute("SELECT event_key,event_json,status,attempts,next_attempt_at FROM notification_outbox "
                            "WHERE status!='accepted' ORDER BY updated_at DESC"):
        event = json.loads(row[1])
        if event_topic(event) not in allowed:
            continue
        result.append({'key': row[0], 'title': event['title'], 'status': row[2],
                       'attempts': row[3], 'next_attempt_at': row[4]})
        if len(result) >= limit:
            break
    return result


def dispatch(store, notifier, events, now=None, mode="risk_six_account"):
    now = now or utc_now()
    stamp = now.isoformat()
    selected = selected_events(store.conn, events, mode)
    with store.conn:
        # A process that died during HTTP may already have delivered the message.
        store.conn.execute("UPDATE notification_outbox SET status='uncertain',updated_at=? WHERE status='sending'", (stamp,))
        for event in events:
            if not store.conn.execute('SELECT 1 FROM alerts WHERE key=? LIMIT 1', (event['key'],)).fetchone():
                store.conn.execute('INSERT INTO alerts(ts,key,title,body,delivered) VALUES(?,?,?,?,0)',
                                   (stamp,event['key'],event['title'],event['body']))
        for event in selected:
            expiry = now + timedelta(minutes=15)
            if event.get('live_advice_id'):
                row=store.conn.execute('SELECT expires_at FROM jd_live_advice WHERE id=?',(event['live_advice_id'],)).fetchone()
                if row: expiry=min(expiry, datetime.fromisoformat(row[0]))
            store.conn.execute('INSERT OR IGNORE INTO notification_outbox(event_key,created_at,expires_at,event_json,next_attempt_at,updated_at) VALUES(?,?,?,?,?,?)',
                               (event['key'],stamp,expiry.isoformat(),json.dumps(event,ensure_ascii=False),stamp,stamp))
    due=[]
    for row in store.conn.execute("SELECT event_key,event_json,attempts,expires_at FROM notification_outbox WHERE status IN ('pending','retry') AND julianday(next_attempt_at)<=julianday(?) ORDER BY created_at,event_key LIMIT 100",(stamp,)).fetchall():
        event=json.loads(row[1])
        from .controls import active
        model=event_topic(event).split(':')[0]
        valid=bool(selected_events(store.conn,[event], mode)) and (model not in ('codex','claude') or active(store.conn,model))
        if row[2]>=3 or datetime.fromisoformat(row[3])<=now or not valid:
            with store.conn:store.conn.execute("UPDATE notification_outbox SET status='cancelled',updated_at=? WHERE event_key=?",(stamp,row[0]))
        else:due.append((event,row[2]))
    for category in ('real_advice','other'):
        group=[(e,n) for e,n in due if (':live_' in e.get('notification_topic',''))==(category=='real_advice')]
        if not group:continue
        title=group[0][0]['title'] if len(group)==1 else (f'💡真实账户建议 · {len(group)}条' if category=='real_advice' else f"{group[0][0]['title']} 等{len(group)}条")
        title=title[:32]
        body='\n\n---\n\n'.join(f"### {e['title']}\n\n{e['body']}" for e,_ in group)
        render_options={}
        if category=='real_advice':
            from .notification_html import real_advice
            if len(group)==1:body=group[0][0]['body']
            render_options['body_html']=real_advice(title,[e for e,_ in group])
        with store.conn:
            for event,_ in group:store.conn.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE event_key=?",(stamp,event['key']))
        ok=notifier.send(title,body,**render_options)
        receipt=getattr(notifier,'last_receipt',{})
        if not isinstance(receipt,dict):receipt={}
        status='accepted' if ok else receipt.get('status','failed')
        channel=receipt.get('channel','unknown')
        with store.conn:
            store.conn.execute('INSERT INTO notification_deliveries(ts,category,title,channel,status,provider_id,provider_code,event_keys_json) VALUES(?,?,?,?,?,?,?,?)',
                (stamp,category,title,channel if isinstance(channel,str) else 'unknown',status,receipt.get('provider_id'),receipt.get('provider_code'),json.dumps([e['key'] for e,_ in group])))
            for event,previous_attempts in group:
                state='accepted' if ok else 'uncertain' if status=='uncertain' else 'retry' if status=='failed' and previous_attempts<2 else 'failed' if status=='failed' else 'cancelled'
                next_at=now+timedelta(seconds=60 if previous_attempts==0 else 180)
                store.conn.execute('UPDATE notification_outbox SET status=?,next_attempt_at=?,updated_at=? WHERE event_key=?',
                                   (state,next_at.isoformat(),stamp,event['key']))
                if ok:store.conn.execute('UPDATE alerts SET delivered=1 WHERE key=?',(event['key'],))
