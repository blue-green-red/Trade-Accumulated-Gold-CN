"""Persistent notification subscriptions, independent of trading switches."""
import json
from datetime import datetime, timezone

TOPICS = {f'{model}:{period}': f'{model.title()} · {label}'
          for model in ('claude', 'codex')
          for period, label in (('short', '短线'), ('medium', '中线'), ('long', '长线'), ('low','低风险短线'), ('standard','标准风险短线'), ('high','高风险短线'))}
TOPICS.update({f'{model}:analysis':f'{model.title()} · 分析摘要' for model in ('claude','codex')})
TOPICS.update({f'{model}:live_{bank}':f'{model.title()} · {label}真实账户建议'
               for model in ('claude','codex') for bank,label in (('zheshang','浙商'),('minsheng','民生'))})
DEFAULT_MODE = 'risk_six_account'


def topics_for_mode(mode,conn=None):
    allowed = ('low','standard','high','analysis','live_zheshang','live_minsheng') if mode == DEFAULT_MODE else (
        'short','medium','analysis','live_zheshang','live_minsheng') if mode == 'four_account' else (
        'short','medium','long','analysis','live_zheshang','live_minsheng')
    from .jd_live_readonly import enabled_banks
    banks=enabled_banks(conn) if conn is not None else ('zheshang','minsheng')
    return {key: label for key, label in TOPICS.items() if key.split(':')[1] in allowed
            and (not key.split(':')[1].startswith('live_') or key.split(':')[1][5:] in banks)}


def subscriptions(conn, mode=DEFAULT_MODE):
    result = []
    for key, label in topics_for_mode(mode,conn).items():
        row = conn.execute('SELECT value FROM state WHERE key=?', ('notify:'+key,)).fetchone()
        value = json.loads(row[0]) if row else {}
        result.append({'key': key, 'name': label, 'enabled': value.get('enabled') is True})
    return result


def visible_subscriptions(conn,mode):
    return subscriptions(conn, mode)


def set_subscription(conn, topic, enabled, mode=DEFAULT_MODE):
    if topic not in topics_for_mode(mode,conn) or type(enabled) is not bool:
        raise ValueError('通知类型或开关值无效')
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     ('notify:'+topic, json.dumps({'enabled':enabled}), now))
    return {'key':topic, 'enabled':enabled}


def event_topic(event):
    if isinstance(event.get('notification_topic'),str):
        return event['notification_topic']
    key = event.get('key', '')
    for prefix in ('acct:', 'paper:'):
        if key.startswith(prefix):
            strategy=key[len(prefix):]
            for model in ('claude', 'codex'):
                if strategy.startswith(model+':'):
                    return model+(':'+('short' if strategy.split(':')[1].startswith('short') else 'long'))
            return ''
    return ''


def selected_events(conn, events, mode=DEFAULT_MODE):
    enabled={s['key'] for s in subscriptions(conn, mode) if s['enabled']}
    def current(e):
        if not e.get('live_advice_id'):return True
        from .jd_live_readonly import accounts
        from .jd_live_semantics import notification_parts
        return any(v['id']==e['live_advice_id'] and v.get('action_status',v['status'])=='current' and notification_parts(v) is not None
                   for a in accounts(conn) for v in a['advice'])
    return [e for e in events if event_topic(e) in enabled and current(e)]


def retire_subscriptions(conn, mode):
    """Delete obsolete switches and cancel unsent tasks; keep delivery history as audit."""
    allowed = topics_for_mode(mode,conn)
    stamp = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT key FROM state WHERE key LIKE 'notify:%'").fetchall():
            if row[0][len('notify:'):] not in allowed:
                conn.execute('DELETE FROM state WHERE key=?', (row[0],))
        for key, raw in conn.execute("SELECT event_key,event_json FROM notification_outbox WHERE status IN ('pending','retry')").fetchall():
            if event_topic(json.loads(raw)) not in allowed:
                conn.execute("UPDATE notification_outbox SET status='cancelled',updated_at=? WHERE event_key=?", (stamp, key))
        conn.execute("DELETE FROM state WHERE key='last_gate'")
