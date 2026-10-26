"""Short-wave experiment budgets; planned risk is not a guaranteed loss cap."""
import json
from .store import utc_now

MODE = 'risk_six_account'
PROFILES = {
    'low': {'name':'低风险', 'trade_risk_pct':1.0, 'drawdown_limit_pct':5.0},
    'standard': {'name':'标准风险', 'trade_risk_pct':2.0, 'drawdown_limit_pct':10.0},
    'high': {'name':'高风险', 'trade_risk_pct':4.0, 'drawdown_limit_pct':20.0},
}
MODES = tuple(f'{p}_{s}' for p in PROFILES for s in ('full','empty'))
LABELS = {f'{p}_{s}':f'{v["name"]} · 初始'+('满仓' if s=='full' else '空仓') for p,v in PROFILES.items() for s in ('full','empty')}


def profile(mode):
    return PROFILES.get(mode.split('_')[0])


def reset_experiment(conn):
    """One-time explicit experiment replacement. Preserve market caches and daily AI usage."""
    marker='risk_experiment:v1'
    if conn.execute('SELECT 1 FROM state WHERE key=?',(marker,)).fetchone():return False
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM state WHERE key=?',(marker,)).fetchone():return False
        for table in ('ai_portfolios','ai_plans','ai_portfolio_fills','ai_portfolio_cycles','ai_portfolio_equity',
                      'ai_portfolio_model_stats','ai_views','accounts','account_fills','account_equity',
                      'paper_accounts','paper_trades','paper_equity','paper_fills'):
            if table in tables:conn.execute('DELETE FROM '+table)
        if 'ai_runs' in tables:
            conn.execute("UPDATE ai_runs SET view_rowid=NULL,status=CASE WHEN status='running' THEN 'cancelled' ELSE status END")
        # Keep model switches, notifications, quotas, market/news and API caches.
        for prefix in ('ai_model_input:','ai_account_input:','ai_price_guard:','ai_input:','ai_history_queries:','codex_session:', 'ai_events:', 'ai_manual:', 'ai_runtime:'):
            conn.execute('DELETE FROM state WHERE key LIKE ?',(prefix+'%',))
        conn.execute("DELETE FROM state WHERE key IN ('autonomous_since','jd_execution_since','ai_inputs')")
        now=utc_now().isoformat()
        conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?)',(marker,json.dumps({'started_at':now}),now))
    return True


def risk_state(account,price,net):
    p=profile(account['start_mode'])
    if not p:return None
    value=account['cash']+account['grams']*net(price) if price else None
    drawdown=max(account['max_drawdown'],1-value/max(account['peak'],value)) if value is not None else account['max_drawdown']
    return {**p,'trade_risk_cny':max(0,value)*p['trade_risk_pct']/100 if value is not None else None,
            'drawdown_pct':drawdown*100,'buy_paused':drawdown*100>=p['drawdown_limit_pct']-1e-8,
            'policy':'单次预算约束买入后的整仓相对当前净值计划损失；历史最大回撤触线后停止新增买入，允许卖出；退出参考不是自动止损，实际损失可超预算。'}
