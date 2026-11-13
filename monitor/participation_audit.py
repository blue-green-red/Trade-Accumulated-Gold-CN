"""Private frozen participation-policy replay; never publishes advice or executes trades."""
import argparse
from bisect import bisect_left
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from .anchor_audit import digest,reserve_attempt,write_private
from .analysis_archive import decode
from .config import BJ

CALIBRATION=('反证须对应当前交易假设、期限和规模，说明为何该规模下等待更优；弱日线、鹰派背景或未确认利好可影响目标和仓位，'
    '但不单独等同反弹假设失效。多项证据联合亦可构成反证；无支持的获利假设、不利预期、执行缺口或风控限制仍可选0，不强制买入或改价。')
GUARD='历史冻结回放：判断时点严格为输入snapshot_at，只用本请求信息；禁止联网、工具、文件与其他会话。忽略运行环境日期，不补充快照之后的知识；账户过期状态按当时输入处理。'


def instant(value):
    result=datetime.fromisoformat(value)
    if result.tzinfo is None:raise ValueError('Timezone required')
    return result


def readonly(db):return sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)


def source(db,archive_id):
    """Use exactly the archived request, not reconstructed/current account inputs."""
    with readonly(db) as conn:
        row=conn.execute('SELECT analyst,status,view_id,started_at FROM ai_analysis_archives WHERE id=?',(archive_id,)).fetchone()
        if not row or row[0]!='codex' or row[1]!='completed':raise ValueError('Completed Codex archive required')
        artifacts={};tools=[]
        for kind,sha,payload in conn.execute('SELECT kind,sha256,payload FROM ai_analysis_artifacts WHERE archive_id=? ORDER BY id',(archive_id,)):
            value=decode(payload)
            # Verify the original byte checksum (not a newly sorted serialization).
            import gzip
            if hashlib.sha256(gzip.decompress(payload)).hexdigest()!=sha:raise ValueError('Archive checksum mismatch')
            if kind in ('request','model_output'):artifacts[kind]=value
            if kind=='runtime_event' and value.get('type')=='item.completed':
                item=value.get('item',{})
                if item.get('type') not in ('agent_message','reasoning','error'):tools.append(item.get('type'))
        request=artifacts.get('request');baseline=artifacts.get('model_output')
        if not request or not baseline or request.get('mode')!='fresh':raise ValueError('Self-contained request/output required')
        if len(request['context'].get('trading_accounts',{}).get('accounts',{}))!=6:raise ValueError('Six-account input required')
        instant(request['context']['snapshot_at'])
        return {'archive_id':archive_id,'view_id':row[2],'request':request,'baseline':baseline,
            'snapshot_at':request['context']['snapshot_at'],'original_tools':tools}


def request_for(value,candidate):
    request=deepcopy(value['request'])
    request['system']+='\n'+GUARD
    if candidate:request['system']+='\n'+CALIBRATION
    request['prompt']=request['system']+'\n\n'+request['user']
    # Both arms preserve the entire archived user/context/schema and use fresh sessions.
    return request


def positive(value):
    if value is None or isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return value if math.isfinite(value) and value>=0 else None


def actions(context,result):
    """Comparable proposed quantities; conditional orders are NOT counted as fills."""
    answer={};trading=context['trading_accounts'];constraints=trading['constraints']
    accounts=[('sim:'+k,a,(result.get('account_decisions') or {}).get(k) or {},False) for k,a in trading['accounts'].items()]
    accounts += [('live:'+k,a,(result.get('live_account_advice') or {}).get(k) or {},True)
        for k,a in (context.get('live_readonly_accounts') or {}).get('accounts',{}).items()]
    for key,account,decision,live in accounts:
        quote=account.get('quote') or {} if live else context.get('jd_accumulation_quote') or {}
        price=positive(quote.get('buy_price_cny') if live else quote.get('price'))
        fee=positive(account.get('sell_fee_rate') if live else constraints.get('sell_fee_rate'))
        spread=0 if live else positive(constraints.get('spread_cny',0))
        cash=positive(account.get('cash_cny') if live else account.get('cash'))
        held=positive(account.get('total_grams') if live else account.get('grams'))
        action=decision.get('action');grams=positive(decision.get('grams'))
        market=(decision.get('price_condition','none') in ('none','reference')) if live else decision.get('execution','market')=='market'
        if action in ('hold','wait','cancel'):market=True
        sizing_price=price if market else positive(decision.get('price_cny') if live else decision.get('limit_price_cny'))
        if not live and grams is None and sizing_price and positive(decision.get('amount_cny')) is not None:
            grams=positive(decision['amount_cny'])/sizing_price
        reason=None
        if action not in ('buy','sell','hold','wait','cancel'):reason='unknown_action'
        elif not price or fee is None or fee>=1 or spread is None or held is None:reason='missing_quote_holdings_or_costs'
        elif live and (account.get('status')!='fresh' or quote.get('status')!='fresh' or not (account.get('pending_orders') or {}).get('complete')):
            reason='stale_or_incomplete_account'
        elif not market:reason='conditional_not_simulated'
        elif action in ('buy','sell') and (grams is None or grams<=0):reason='quantity_unknown'
        elif action=='buy' and (cash is None or grams*price>cash+0.01):reason='cash_insufficient_or_unknown'
        elif action=='sell' and grams>float(account.get('sellable_grams',held) or 0)+1e-6:reason='sellable_insufficient'
        elif action=='buy' and not live and (account.get('risk_budget') or {}).get('buy_paused'):reason='buy_paused'
        if not live and action=='buy' and reason is None:
            risk=account.get('risk_budget') or {};exit_price=positive(decision.get('risk_exit_cny'))
            stop=positive(decision.get('stop_loss_cny'))
            if stop is None and not decision.get('clear_stop_loss'):stop=positive(account.get('stop_price'))
            if exit_price is None or exit_price>=price:reason='invalid_risk_exit'
            else:
                if stop is not None:exit_price=min(exit_price,stop)
                loss=held*(price*(1-fee)-spread)+grams*price-(held+grams)*(exit_price*(1-fee)-spread)
                if positive(risk.get('trade_risk_cny')) is None or loss>float(risk['trade_risk_cny'])+0.01:reason='risk_budget_exceeded'
        if not live and action in ('buy','sell') and reason is None:
            lo=positive(decision.get('min_execution_price_cny'));hi=positive(decision.get('max_execution_price_cny'))
            if lo is None or hi is None:reason='execution_range_unknown'
            elif lo>hi or price<lo or price>hi:reason='outside_execution_range'
        nav=cash+held*price if cash is not None and held is not None and price else None
        proposed=held+(grams or 0)*(1 if action=='buy' else -1 if action=='sell' else 0) if held is not None else None
        after_cash=(cash-(grams or 0)*price if action=='buy' else cash+(grams or 0)*(price*(1-fee)-spread) if action=='sell' else cash) if cash is not None and price and fee is not None and spread is not None else None
        nav_after=after_cash+proposed*price if after_cash is not None and proposed is not None and price else None
        answer[key]={'action':action,'execution':'market' if market else 'conditional','grams':grams if action in ('buy','sell') else 0,
            'amount_cny':round(grams*sizing_price,2) if grams is not None and sizing_price and action in ('buy','sell') else 0,
            'position_before_pct':round(held*price/nav*100,3) if nav else None,
            'position_after_pct':round(proposed*price/nav_after*100,3) if nav_after and market else None,
            'order_price_cny':sizing_price,
            'entry_cny':price,'sell_fee_rate':fee,'spread_cny':spread,'held_grams':held,
            'markout_excluded':reason,'suggested_order_count':len(decision.get('suggested_orders') or []),
            'suggested_buy_count':sum(o.get('kind')=='limit_buy' for o in decision.get('suggested_orders') or []),
            'reason':decision.get('reason'),'invalidation':decision.get('invalidation')}
    return answer


def horizon_at(start,days):
    value=instant(start).astimezone(BJ)
    for _ in range(days):
        value+=timedelta(days=1)
        while value.weekday()>=5:value+=timedelta(days=1)
    return value


def quote_series(db):
    """Evaluation-only read; never passed to the model. Bank quotes stay bank-specific."""
    series={'sim':{}}
    with readonly(db) as conn:
        for ts,price in conn.execute('SELECT ts,jd_price FROM ticks WHERE jd_price>0 AND market_open=1 ORDER BY ts'):
            series['sim'][instant(ts)]=price
        for (payload,) in conn.execute("SELECT payload FROM ai_analysis_artifacts WHERE kind='request' ORDER BY id"):
            context=decode(payload).get('context') or {};snapshot=context.get('snapshot_at')
            if not snapshot:continue
            for bank,account in (context.get('live_readonly_accounts') or {}).get('accounts',{}).items():
                quote=account.get('quote') or {};ts=quote.get('quoted_at');price=positive(quote.get('sell_price_cny'))
                if quote.get('status')!='fresh' or not ts or not price:continue
                if instant(ts)>instant(snapshot):continue
                points=series.setdefault('live:'+bank,{})
                when=instant(ts)
                if when in points and points[when]!=price:raise ValueError('Conflicting bank quotes at identical timestamp')
                points[when]=price
    return {k:sorted(v.items()) for k,v in series.items()}


def markout(proposal,price):
    """Incremental vs holding the starting position; not a full executor/backtest."""
    entry=proposal['entry_cny'];fee=proposal['sell_fee_rate'];spread=proposal['spread_cny']
    grams=proposal['grams'] or 0;action=proposal['action']
    price_only=grams*(price-entry)*(1 if action=='buy' else -1 if action=='sell' else 0)
    net=(grams*(price*(1-fee)-spread-entry) if action=='buy' else
         grams*((entry-price)*(1-fee)) if action=='sell' else 0.)
    holding_delta=(proposal.get('held_grams') or 0)*(price-entry)*(1-fee)
    return {'price_only_delta_cny':round(price_only,2),'incremental_net_cny':round(net,2),
        'starting_position_net_change_cny':round(holding_delta,2),'whole_account_net_change_cny':round(holding_delta+net,2)}


def evaluate(snapshot,proposals,series,horizons=(3,5)):
    result={}
    for key,proposal in proposals.items():
        points=series.get('sim' if key.startswith('sim:') else key,[]);times=[p[0] for p in points];rows=[]
        for days in horizons:
            target=horizon_at(snapshot,days);index=bisect_left(times,target)
            row={'weekdays':days,'target_at':target.isoformat()}
            if proposal['markout_excluded']:row['unavailable']=proposal['markout_excluded']
            elif index==len(points) or times[index]-target>timedelta(minutes=5 if key.startswith('sim:') else 60):
                row['unavailable']='no_same_product_quote_at_horizon'
            else:
                at,price=points[index];row.update(quote_at=at.isoformat(),quote_cny=price,**markout(proposal,price))
            rows.append(row)
        result[key]=rows
    return result


def metrics(usage):return {k:v for k,v in usage.items() if not k.startswith('_')}


def summary(report):
    lines=['# 有限参与规则冻结回放（私人报告）','',
        '候选规则：'+report['policy'],'',
        '行情、账户、用户输入及输出Schema冻结；原始组复用旧输出，matched为同工具条件重跑，candidate仅增加上述反证规则。',
        '以下均为假定快照价执行的纸面比较，不是实际交易。增量为相对维持原持仓的差额，整仓包含原持仓涨跌；条件单不假定成交。',
        '3/5日为工作日同一时刻，银行允许最多60分钟采样延迟，模拟最多5分钟。缺样本保留未知。','',
        '|归档|组别|账户|行动|金额元|动作后仓位%|3日增量/整仓元|5日增量/整仓元|',
        '|---|---|---|---|---:|---:|---:|---:|']
    def outcome(row):
        if row.get('unavailable'):return '未知：'+row['unavailable']
        return f"{row['incremental_net_cny']:+.2f}/{row['whole_account_net_change_cny']:+.2f}"
    for case in report['cases']:
        arms={'original':{'actions':case['original_actions'],'markouts':case['original_markouts']},**case['arms']}
        for name,arm in arms.items():
            for key,record in arm['actions'].items():
                horizons=arm['markouts'][key]
                lines.append(f"|{case['archive_id']}|{name}|{key}|{record['action']} / {record['execution']}|{record['amount_cny']}|"
                    f"{record['position_after_pct']}|{outcome(horizons[0])}|{outcome(horizons[1])}|")
    lines+=['','## 限制','']+['- '+v for v in report['limitations']]
    return '\n'.join(lines)+'\n'


def add_evaluation(report,db):
    from .store import utc_now
    series=quote_series(db)
    report['evaluation_as_of']=utc_now().isoformat()
    for case in report['cases']:
        case['original_markouts']=evaluate(case['snapshot_at'],case['original_actions'],series)
        for arm in case['arms'].values():arm['markouts']=evaluate(case['snapshot_at'],arm['actions'],series)


def save_report(report,json_path,markdown_path):
    write_private(json_path,report)
    fd=os.open(markdown_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(summary(report))


def refresh_evaluation(db,output):
    """Mature later horizons without another model call; generation evidence stays immutable."""
    from .store import utc_now
    if output.stat().st_mode&0o077:raise ValueError('Replay directory must be private (0700)')
    report=json.loads((output/'report.json').read_text())
    report['generation_report_sha256']=digest(report)
    # Recompute action metrics with the current evaluator, not model outputs.
    for case in report['cases']:
        value=json.loads((output/f"{case['archive_id']}.source.json").read_text())
        if digest(value['request']['context'])!=case['input_sha256']:raise ValueError('Frozen input changed')
        case['original_actions']=actions(value['request']['context'],value['baseline'])
        for arm in case['arms'].values():arm['actions']=actions(value['request']['context'],arm['output'])
    add_evaluation(report,db)
    name='evaluation-'+utc_now().strftime('%Y%m%dT%H%M%S%fZ')
    save_report(report,output/(name+'.json'),output/(name+'.md'))
    return report


def run(settings,archive_ids,output,matched_ids=()):
    """One candidate per archive, optionally a same-tool baseline; production budget rows only."""
    from .ai import AIAnalyst
    from .analysis_archive import AnalysisArchive,export
    from .codex_runtime import run_frozen
    from .runtime_resources import slot
    from .store import Store,utc_now
    if not archive_ids or len(set(archive_ids))!=len(archive_ids) or not set(matched_ids)<=set(archive_ids):raise ValueError('Invalid archive selection')
    sources=[source(settings.db_path,i) for i in archive_ids]
    total=len(sources)+len(set(matched_ids));run_id='participation-audit:'+uuid.uuid4().hex
    report={'run_id':run_id,'published':False,'execution_invoked':False,'policy':CALIBRATION,'cases':[],
        'method':'Archived inputs/schema/policy, one candidate per snapshot; optional matched no-tool baseline. No future prices in model input.',
        'limitations':['Tiny exploratory sample; one draw per arm cannot establish causal effects or missed-rally probability.',
            'Historical prompts may predate the current participation preference. Original tool access can differ; matched arm controls access, not model randomness.',
            'An unspecified provider model/default may change since the archived run; historical outputs are observational references.',
            '3/5 weekday same-clock markouts, sampled quotes, no holiday adjustment; not a full strategy backtest or actual fills.',
            'Market proposals assume snapshot-price execution; no slippage, inflight repricing, later stop/target/plan changes or existing-order fills.',
            'Limit/conditional proposals are listed but never assumed filled. Bank quotes never substituted with public/simulation prices.']}
    with slot(settings.db_path) as acquired:
        if not acquired:raise RuntimeError('Model/browser resource slot is busy; retry later')
        output.mkdir(parents=True,exist_ok=False,mode=0o700)
        output.chmod(0o700)
        with sqlite3.connect(settings.db_path,timeout=10) as conn:
            remaining=total
            for value in sources:
                aid=value['archive_id'];case={'archive_id':aid,'snapshot_at':value['snapshot_at'],
                    'input_sha256':digest(value['request']['context']),'original_tools':value['original_tools'],
                    'original_actions':actions(value['request']['context'],value['baseline']),'arms':{}}
                report['cases'].append(case)
                write_private(output/f'{aid}.source.json',value)
                for label,candidate in ([('matched',False)] if aid in matched_ids else [])+[('candidate',True)]:
                    attempt=reserve_attempt(conn,settings,run_id,f'{aid}:{label}',required=remaining);remaining-=1
                    usage={'input':0,'output':0,'searches':0};start=time.monotonic();status='diagnostic_failed';archive=None
                    request=request_for(value,candidate)
                    write_private(output/f'{aid}.{label}.request.json',request)
                    try:
                        with tempfile.TemporaryDirectory(prefix='gold-participation-audit-') as temp:
                            isolated=replace(settings,db_path=Path(temp)/'audit.db',ai_optimize=False,
                                codex_resume_enabled=False,ai_max_searches=0,ai_history_max_queries=0,ai_page_fetch_max_calls=0,
                                clawx_api_key='',anthropic_api_key='',codex_model=request.get('model') or settings.codex_model)
                            store=Store(isolated.db_path)
                            try:
                                analyst=AIAnalyst(isolated,store,'codex','codex')
                                archive=AnalysisArchive(store,analyst,attempt,run_id+f':{aid}:{label}','有限参与冻结回放',request['context'])
                                usage['_archive']=archive
                                _,_,result=run_frozen(analyst,request,usage)
                                result=result.model_dump(mode='json');archive.add('model_output',result)
                                status='diagnostic_completed'
                                arm={'output':result,'actions':actions(request['context'],result)}
                                case['arms'][label]=arm
                                write_private(output/f'{aid}.{label}.output.json',result)
                            finally:
                                if archive:
                                    archive.finish(status,usage=metrics(usage));export(isolated.db_path,output/f'{aid}.{label}.trace.jsonl')
                                store.conn.close()
                        arm.update(seconds=round(time.monotonic()-start,3),usage=metrics(usage))
                        print(json.dumps({'archive_id':aid,'arm':label,'status':status,'seconds':arm['seconds'],
                            'input_tokens':usage['input'],'output_tokens':usage['output']},ensure_ascii=False),flush=True)
                    finally:
                        with conn:
                            conn.execute('UPDATE ai_runs SET status=?,completed_at=?,metrics_json=? WHERE id=?',
                                (status,utc_now().isoformat(),json.dumps(metrics(usage)),attempt))
                        write_private(output/f'{aid}.{label}.progress.json',report)
        # Future labels are only read after all generations, never from the isolated model DB.
        add_evaluation(report,settings.db_path)
        save_report(report,output/'report.json',output/'report.md')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive-id',type=int,nargs='+',default=[])
    p.add_argument('--matched-id',type=int,nargs='*',default=[],help='Also regenerate baseline with identical disabled tools')
    p.add_argument('--output',type=Path,required=True,help='New PRIVATE directory; never publish')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--run',action='store_true')
    group.add_argument('--evaluate',action='store_true',help='Refresh only future labels in an existing report; zero model calls')
    args=p.parse_args()
    if args.run and not args.archive_id:p.error('--archive-id required with --run')
    from .config import Settings
    try:
        if args.evaluate:refresh_evaluation(Settings().db_path,args.output)
        else:run(Settings(),args.archive_id,args.output,args.matched_id)
    except Exception as exc:
        # Subprocess exceptions may include the entire private prompt in their command.
        raise SystemExit(f'Replay failed ({type(exc).__name__}); inspect private progress/trace files in {args.output}') from None
    print('Private replay saved to',args.output)


if __name__=='__main__':main()
