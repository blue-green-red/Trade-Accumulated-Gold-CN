"""Persistent notification subscriptions, independent of trading switches."""
import json
from datetime import datetime, timezone

TOPICS = {f'{model}:{period}': f'{model.title()} · {label}'
          for model in ('claude', 'codex')
          for period, label in (('short', '短线'), ('medium', '中线'), ('long', '长线'), ('low','低风险短线'), ('standard','标准风险短线'), ('high','高风险短线'))}
TOPICS.update({'rules:hold': '对照 · 买入持有', 'rules:position': '对照 · 趋势策略',
               'rules:short': '对照 · 短线', 'system': '行情提醒与定时报告'})


def subscriptions(conn):
    result = []
    for key, label in TOPICS.items():
        row = conn.execute('SELECT value FROM state WHERE key=?', ('notify:'+key,)).fetchone()
        value = json.loads(row[0]) if row else {}
        result.append({'key': key, 'name': label, 'enabled': value.get('enabled') is True})
    return result


def visible_subscriptions(conn,mode):
    risk=mode=='risk_six_account'
    return [r for r in subscriptions(conn) if not r['key'].startswith(('claude:','codex:')) or
            (r['key'].split(':')[-1] in ('low','standard','high') if risk else
             r['key'].split(':')[-1] not in ('low','standard','high') and (mode!='four_account' or not r['key'].endswith(':long')))]


def set_subscription(conn, topic, enabled):
    if topic not in TOPICS or type(enabled) is not bool:
        raise ValueError('通知类型或开关值无效')
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     ('notify:'+topic, json.dumps({'enabled':enabled}), now))
    return {'key':topic, 'enabled':enabled}


def event_topic(event):
    if event.get('notification_topic') in TOPICS:
        return event['notification_topic']
    key = event.get('key', '')
    for prefix in ('acct:', 'paper:'):
        if key.startswith(prefix):
            strategy=key[len(prefix):]
            for model in ('claude', 'codex'):
                if strategy.startswith(model+':'):
                    return model+(':'+('short' if strategy.split(':')[1].startswith('short') else 'long'))
            strategy=strategy.split(':')[0]
            return 'rules:'+strategy if 'rules:'+strategy in TOPICS else 'system'
    return 'system'


def selected_events(conn, events):
    enabled={s['key'] for s in subscriptions(conn) if s['enabled']}
    return [e for e in events if event_topic(e) in enabled]
