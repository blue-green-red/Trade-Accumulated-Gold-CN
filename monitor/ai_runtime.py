"""Reconcile interrupted daemon work only when its owning analysis thread is absent."""
import json
from .store import utc_now


def recover(store,models,now=None):
    """Caller guarantees no local AI thread; never clears diagnostic/other-process jobs."""
    from .analysis_archive import encode
    now=now or utc_now();at=now.isoformat();conn=store.conn
    interrupted=[]
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        for model in models:
            state=store.get_state('ai_runtime:'+model,{})
            if not state.get('running') and not state.get('queued'):continue
            interrupted.append(model)
            conn.execute('INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)',
                ('ai_runtime:'+model,json.dumps({**state,'running':False,'queued':False,'interrupted_at':at}),at))
            if 'ai_runs' not in tables:continue
            rows=conn.execute("SELECT id,run_id FROM ai_runs WHERE analyst=? AND status='running'",(model,)).fetchall()
            for attempt,run_id in rows:
                conn.execute("UPDATE ai_runs SET status='interrupted',completed_at=? WHERE id=?",(at,attempt))
                if {'ai_analysis_archives','ai_analysis_artifacts'}<=tables:
                    for (aid,) in conn.execute("SELECT id FROM ai_analysis_archives WHERE analyst=? AND run_id=? AND status='running'",(model,run_id)).fetchall():
                        payload,sha=encode({'status':'interrupted','reason':'analysis thread no longer exists; no result inferred'})
                        conn.execute('INSERT INTO ai_analysis_artifacts(archive_id,ts,kind,sha256,payload) VALUES(?,?,?,?,?)',(aid,at,'outcome',sha,payload))
                        conn.execute("UPDATE ai_analysis_archives SET status='interrupted',completed_at=? WHERE id=?",(at,aid))
    from .manual_ai import recover as recover_manual
    for model in interrupted:recover_manual(conn,model)
