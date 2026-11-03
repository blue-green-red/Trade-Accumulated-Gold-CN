"""Public research metadata/history. Anonymous reads never query private AI views."""
import json
import math
from datetime import datetime, timedelta, timezone
from .controls import active


def assessment_projection(value):
    value = value if isinstance(value, dict) else {}
    out = {k: value[k] for k in ('bias_short', 'bias_long') if value.get(k) in ('bullish', 'bearish', 'neutral')}
    if value.get('confidence') in ('low', 'medium', 'high'):
        out['confidence'] = value['confidence']
    for suffix in ('3d', '3m'):
        pair = [value.get('p_' + side + '_' + suffix) for side in ('up', 'down')]
        if all(type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1 for p in pair) and sum(pair) <= 1 + 1e-8:
            for side, p in zip(('up', 'down'), pair): out['p_' + side + '_' + suffix] = p
    return out or None


def ensure_columns(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(public_ai_reports)')}
    for name in ('assessment_json', 'evidence_json'):
        if name not in columns: conn.execute('ALTER TABLE public_ai_reports ADD COLUMN ' + name + ' TEXT')


def evidence_for(content, context):
    basis = (content or {}).get('decision_basis') or {}
    levels, sources = [], []
    quote = context.get('jd_accumulation_quote') or {}
    days = (context.get('jd_daily_samples') or {}).get('days') or []
    def add(label, price, fallback):
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0: return
        found = []
        if quote.get('price') == price: found.append('本轮公共京东参考价')
        for day in reversed(days):
            for field, title in (('sampled_low_cny', '采样低值'), ('sampled_high_cny', '采样高值'), ('first_price_cny', '首笔采样'), ('last_price_cny', '末笔采样')):
                if day.get(field) == price:
                    found.append(str(day.get('date_bj', ''))[:10] + ' 京东' + title)
        levels.append({'label': label, 'price_cny': price, 'basis': '；'.join(dict.fromkeys(found))[:220] if found else fallback})
    scenario = basis.get('entry_scenario')
    if scenario:
        for field, title in (('entry_price_cny', '假设入场'), ('target_price_cny', '情景目标'), ('exit_price_cny', '情景退出')):
            add(title, scenario[field], scenario['basis'])
    for trigger in basis.get('review_triggers', []):
        if trigger['kind'] == 'price':
            add('上行复评' if trigger['condition'] == 'at_or_above' else '下行复评', trigger['price_cny'],
                '本轮公开观察阈值；尚无精确采样匹配，不能当作已验证的技术支撑阻力。')
    if quote.get('price'):
        sources.append({'name': '京东积存金公共报价', 'as_of': quote.get('quote_at')})
    indicators = context.get('daily_indicators') or {}
    if indicators.get('fetched_at'):
        sources.append({'name': 'TradingView 国际黄金日线', 'as_of': indicators['fetched_at']})
    macro = context.get('macro') or {}
    for key, title in (('quotes_fetched_at', '美元、美债与人民币报价'), ('calendar_fetched_at', '经济日历')):
        if macro.get(key): sources.append({'name': title, 'as_of': macro[key]})
    return {'key_levels': levels[:7], 'data_sources': sources}


def metadata(assessment_raw, evidence_raw):
    try: assessment = assessment_projection(json.loads(assessment_raw or 'null'))
    except (ValueError, TypeError): assessment = None
    levels, sources = [], []
    try:
        value = json.loads(evidence_raw or '{}')
        from .public_ai import privacy_ok
        for row in value.get('key_levels', [])[:7]:
            if (isinstance(row, dict) and type(row.get('price_cny')) in (int, float)
                    and math.isfinite(row['price_cny']) and row['price_cny'] > 0
                    and isinstance(row.get('label'), str) and isinstance(row.get('basis'), str)):
                part = {k: row[k] for k in ('label', 'price_cny', 'basis')}
                if privacy_ok(part, {}): levels.append(part)
        for row in value.get('data_sources', [])[:5]:
            if isinstance(row, dict) and isinstance(row.get('name'), str):
                part = {'name': row['name'][:80], 'as_of': row.get('as_of') if isinstance(row.get('as_of'), str) else None}
                if privacy_ok(part, {}): sources.append(part)
    except (ValueError, TypeError, AttributeError): pass
    return {'market_assessment': assessment, 'key_levels': levels, 'data_sources': sources}


def backfill(conn):
    """Offline migration: project only numeric/enumerated forecasts with exact identity.

    Price provenance is rebuilt only from public ticks at the report's snapshot,
    never from account history, private notes or archived model input.
    """
    from .public_ai import validate_content, stamp
    ensure_columns(conn)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(ai_views)')}
    if not {'analyst', 'ts', 'view_json'} <= columns: return 0
    rows = conn.execute("SELECT p.id,p.snapshot_at,p.content_json,p.quote_json,v.view_json FROM public_ai_reports p "
        "JOIN ai_views v ON v.rowid=p.view_id AND v.analyst=p.analyst AND v.ts=p.created_at "
        "WHERE p.status='ready' AND (p.assessment_json IS NULL OR p.evidence_json IS NULL) ORDER BY p.id DESC LIMIT 1000").fetchall()
    count = 0
    for row in rows:
        try:
            content = validate_content(json.loads(row['content_json']), {})
            view = json.loads(row['view_json']); quote = json.loads(row['quote_json'] or '{}') or {}
            at = stamp(row['snapshot_at'])
            days = conn.execute("SELECT date(ts,'+8 hours') date_bj,MIN(jd_price) sampled_low_cny,MAX(jd_price) sampled_high_cny "
                "FROM ticks WHERE ts>=? AND ts<=? AND jd_price>0 AND market_open=1 GROUP BY date(ts,'+8 hours') ORDER BY date_bj DESC LIMIT 10",
                ((at - timedelta(days=30)).isoformat(), at.isoformat())).fetchall()
            context = {'jd_daily_samples': {'days': [dict(d) for d in days]},
                       'jd_accumulation_quote': {'price': quote.get('price_cny'), 'quote_at': quote.get('quoted_at')}}
            conn.execute('UPDATE public_ai_reports SET assessment_json=?,evidence_json=? WHERE id=?',
                (json.dumps(assessment_projection(view)), json.dumps(evidence_for(content, context), ensure_ascii=False), row['id']))
            count += 1
        except (ValueError, TypeError): continue
    return count


def history(conn, settings, query, now=None):
    from .public_ai import validate_content, omit_scenario_math
    if set(query) - {'analyst', 'before', 'limit', 'days', 'report_id'} or any(len(v) != 1 for v in query.values()):
        raise ValueError('Invalid public history query')
    analyst = query.get('analyst', ['codex'])[0]
    if analyst not in ('codex', 'claude') or analyst not in settings.ai_analysts: raise ValueError('Invalid analyst')
    def integer(key, default, low, high):
        value = query.get(key, [str(default)])[0]
        if not value.isascii() or not value.isdigit() or len(value) > 19: raise ValueError('Invalid history integer')
        result = int(value)
        if not low <= result <= high: raise ValueError('Invalid history integer')
        return result
    detail = 'report_id' in query
    if detail and set(query) - {'analyst', 'report_id'}: raise ValueError('Invalid history detail')
    rid = integer('report_id', 1, 1, 2**63-1) if detail else None
    limit, days, before = integer('limit', 10, 1, 20), integer('days', 14, 1, 30), integer('before', 2**63-1, 1, 2**63-1)
    out = {'analyst': analyst, 'available_analysts': [m for m in settings.ai_analysts if m in ('claude', 'codex') and active(conn, m)],
           'items': [], 'next_before': None, 'report': None}
    if not active(conn, analyst): return out
    now = now or datetime.now(timezone.utc)
    sql = 'SELECT id,created_at,snapshot_at,expires_at,content_json,quote_json,assessment_json,evidence_json FROM public_ai_reports WHERE analyst=? AND status=? AND content_json IS NOT NULL'
    params = [analyst, 'ready']
    if detail: sql += ' AND id=?'; params.append(rid)
    else:
        sql += ' AND id<? AND created_at>=? AND created_at<=?'
        params.extend([before, (now-timedelta(days=days)).isoformat(), now.isoformat()])
    rows = conn.execute(sql + ' ORDER BY id DESC LIMIT ?', [*params, 1 if detail else limit+1]).fetchall()
    if not detail and len(rows)>limit: out['next_before'] = rows[limit-1]['id']
    for row in rows[:1 if detail else limit]:
        try:
            content = validate_content(json.loads(row['content_json']), {})
            if ((content.get('decision_basis') or {}).get('no_entry_reason') or '').startswith('无法核验该情景全部价位的公共来源'):
                omit_scenario_math(content, {'public_reference': json.loads(row['quote_json'] or '{}') or {}})
            meta = metadata(row['assessment_json'], row['evidence_json'])
            item = {'id': row['id'], 'created_at': row['created_at'], 'snapshot_at': row['snapshot_at'],
                    'outlook': content['analysis']['outlook'], 'change_summary': content['analysis']['change_summary'],
                    'market_assessment': meta['market_assessment']}
            if detail:
                out['report'] = {**item, 'analysis': content['analysis'], 'key_levels': meta['key_levels'],
                                 'data_sources': meta['data_sources'], 'historical': True}
            else: out['items'].append(item)
        except (ValueError, TypeError): continue
    return out
