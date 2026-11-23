"""Sticky price invalidation for an in-flight analysis, without restarting CLI jobs."""
import json
import math
from datetime import datetime
from .store import utc_now


def read(conn,key):
    row=conn.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(conn,key,value):
    conn.execute('INSERT INTO state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (key,json.dumps(value,ensure_ascii=False),utc_now().isoformat()))


def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and n>0 else None
    except (TypeError,ValueError):return None


def check(state,market):
    if state.get('invalidated_at'):return state
    if market.get('at') and state.get('snapshot_at') and datetime.fromisoformat(market['at'])<datetime.fromisoformat(state['snapshot_at']):return state
    for source in ('jd','xauusd'):
        old,current=number(state.get(source)),number(market.get(source))
        if old and current:
            move=abs(current/old-1)*100
            if move+1e-9>=state['threshold_pct']:
                state.update(invalidated_at=utc_now().isoformat(),source=source,observed_price=current,move_pct=move,
                             reason=f'分析期间行情异动：{source} 较输入变化{move:.3f}%，达到{state["threshold_pct"]:g}%，旧决策不执行')
                break
    return state


def register(conn,model,attempt,context,settings):
    quote=context.get('jd_accumulation_quote') or (context.get('trading_accounts') or {}).get('reference_quote') or {}
    state={'attempt':attempt,'jd':number(quote.get('price')),'xauusd':number(context.get('xauusd')),
           'snapshot_at':context.get('snapshot_at'),'threshold_pct':settings.ai_inflight_max_move_pct}
    check(state,read(conn,'ai_live_market'))
    save(conn,'ai_price_guard:'+model,state)
    return state


def observe(conn,snap,settings):
    from .execution import execution_quote
    quote=execution_quote(snap)
    market={'jd':quote['price'] if quote else None,'xauusd':number(snap.get('xauusd')),'at':utc_now().isoformat()}
    save(conn,'ai_live_market',market)
    invalid=[]
    for model in settings.ai_analysts:
        key='ai_price_guard:'+model;state=read(conn,key)
        if not state:continue
        run=conn.execute('SELECT status FROM ai_runs WHERE id=?',(state['attempt'],)).fetchone()
        if not run or run[0]!='running':continue
        check(state,market);save(conn,key,state)
        if state.get('invalidated_at'):invalid.append((model,state))
    return invalid


def finish(conn,model,attempt):
    state=read(conn,'ai_price_guard:'+model)
    if state.get('attempt')!=attempt:return {}
    check(state,read(conn,'ai_live_market'))
    save(conn,'ai_price_guard:'+model,state)
    return state


def bank_moves_during_analysis(conn, inputs, now, threshold):
    """Compare each bank's own input with quotes received before publication."""
    from .jd_live_buy_watch import sample, utc_text
    from .jd_live_conditions import stamp
    from .jd_live_readonly import PRODUCTS, accounts
    from .trade_costs import number as price

    bank_inputs = (inputs or {}).get('accounts') or {}
    if not bank_inputs or not math.isfinite(threshold) or threshold < 0:
        return {}
    failures = {}
    cutoff = utc_text(now)
    for account in accounts(conn, now, include_advice=False):
        bank = account['bank']
        source = bank_inputs.get(bank) or {}
        origin = source.get('quote') or {}
        anchor = stamp(origin.get('quoted_at'))
        buy, sell = price(origin.get('buy_price_cny')), price(origin.get('sell_price_cny'))
        if source.get('status') != 'fresh' or origin.get('status') != 'fresh' or not anchor or not (buy or sell):
            continue
        # The last verified snapshot may commit before its separate quote-sample write.
        rows = list(conn.execute('''SELECT quoted_at,observed_at,buy_price_cny,sell_price_cny
            FROM jd_live_quote_samples WHERE bank=? AND quoted_at>? AND quoted_at<=?
            AND observed_at BETWEEN quoted_at AND ? ORDER BY quoted_at''', (bank, utc_text(anchor), cutoff, cutoff)))
        if (point := sample(account, now)) and point[0] > anchor:
            current_at = utc_text(point[0])
            if (not rows or rows[-1][0] < current_at
                    or rows[-1][0] == current_at and tuple(rows[-1][2:]) != point[2:]):
                rows.append((current_at, cutoff, point[2], point[3]))
        for quoted_at, observed_at, current_buy, current_sell in rows:
            for side, base, raw in (('buy', buy, current_buy), ('sell', sell, current_sell)):
                current = price(raw)
                if not base or not current:
                    continue
                change = (current / base - 1) * 100
                if abs(change) + 1e-9 < threshold:
                    continue
                direction = '上涨' if change >= 0 else '下跌'
                label = '买价' if side == 'buy' else '卖价'
                failures[bank] = {'reason': f'分析期间{PRODUCTS[bank][1]}{label}较输入{direction}{abs(change):.3f}%，需按最新报价复评',
                                  'quote_side': side, 'input_price_cny': base, 'input_quote_at': utc_text(anchor),
                                  'price_cny': current, 'quoted_at': quoted_at, 'observed_at': observed_at,
                                  'change_pct': round(change, 6)}
                break
            if bank in failures:
                break
    return failures


def bank_refreshes(conn, model, attempt, failures, now):
    """Queue one current retry per affected bank, within the publication transaction."""
    from .ai_events import read as read_events, write

    if not failures:
        return
    events = read_events(conn, model)
    prefix = f'live-bank-price-guard:{model}:'
    events['pending'] = [event for event in events['pending'] if not (event['id'].startswith(prefix)
                         and (event.get('data') or {}).get('bank') in failures)]
    for bank, evidence in failures.items():
        events['pending'].append({'id': f'{prefix}{bank}:{attempt}', 'reason': evidence['reason'],
            'min_gap_minutes': 0, 'observed_at': now.isoformat(), 'data': {'bank': bank, **evidence}})
    write(conn, model, events)


def request_refresh(conn,model,state):
    from .ai_events import enqueue
    from .controls import active
    if state.get('invalidated_at') and active(conn,model):
        enqueue(conn,model,state['reason'],0,f'price-guard:{model}:{state["attempt"]}',{'source':'分析价格校验','move_pct':state['move_pct']})
