"""Remove long-account ledgers and embedded structured account data in four-account mode."""
import json


def is_long(value):
    return isinstance(value,str) and (value in ('long_full','long_empty') or value.endswith((':long_full',':long_empty')))


def prune(value):
    if isinstance(value,dict):
        if any(is_long(value.get(k)) for k in ('key','account','strategy','start_mode')):return None
        return {k:prune(v) for k,v in value.items() if not is_long(k)}
    if isinstance(value,list):
        return [clean for item in value if (clean:=prune(item)) is not None]
    return value


def remove_long_accounts(conn):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        removed=0
        for table,column in (('ai_portfolios','key'),('ai_portfolio_fills','account'),
                             ('ai_portfolio_cycles','account'),('ai_portfolio_equity','account'),('ai_plans','account')):
            if table in tables:removed+=conn.execute(f"DELETE FROM {table} WHERE {column} LIKE '%:long_full' OR {column} LIKE '%:long_empty'").rowcount
        if removed and 'ai_portfolio_model_stats' in tables:
            # Previous peaks used six accounts; rebuild for the retained four.
            conn.execute('DELETE FROM ai_portfolio_model_stats')
        if 'ai_views' in tables:
            for row in conn.execute("SELECT rowid,view_json FROM ai_views WHERE view_json LIKE '%long_full%' OR view_json LIKE '%long_empty%'").fetchall():
                value=json.loads(row[1]);clean=prune(value)
                if clean!=value:conn.execute('UPDATE ai_views SET view_json=? WHERE rowid=?',(json.dumps(clean,ensure_ascii=False),row[0]))
        if 'state' in tables:
            conn.execute("DELETE FROM state WHERE key IN ('notify:claude:long','notify:codex:long')")
            for key,raw in conn.execute("SELECT key,value FROM state WHERE value LIKE '%long_full%' OR value LIKE '%long_empty%'").fetchall():
                try:value=json.loads(raw)
                except (ValueError,TypeError):continue
                clean=prune(value)
                if clean!=value:conn.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(clean,ensure_ascii=False),key))
    return removed
