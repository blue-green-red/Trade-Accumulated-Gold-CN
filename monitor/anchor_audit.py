"""Paired, frozen Codex diagnostic; outputs never enter advice or execution pipelines."""
import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from .ai_inputs import compact_context


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def paired_inputs(context):
    """Mask old opinions/protection only, retaining actual trades and open bank orders."""
    common=deepcopy(context)
    # Triggers can quote old private targets. Exclude equally, not only in the blind arm.
    excluded=[key for key in ('trigger_events','event_coverage') if key in common]
    for key in excluded:common.pop(key)
    reference=compact_context(common)
    shadow=deepcopy(reference);masked=[]

    def remove(value,key,path):
        if key in value:
            value.pop(key);masked.append(path+'.'+key)

    for name,account in (shadow.get('trading_accounts') or {}).get('accounts',{}).items():
        path='trading_accounts.accounts.'+name
        for key in ('stop_price','target_price'):remove(account,key,path)
        comparison=account.get('position_comparison')
        if isinstance(comparison,dict):remove(comparison,'scenarios',path+'.position_comparison')
        plan=account.get('last_plan')
        if isinstance(plan,dict):
            remove(plan,'reason',path+'.last_plan')
            # Preserve order status, quantities and executable entry terms, not old protection/thesis.
            decision=plan.get('decision')
            if isinstance(decision,dict):
                for key in ('risk_exit_cny','stop_loss_cny','take_profit_cny','clear_stop_loss',
                            'clear_take_profit','reason','invalidation'):
                    remove(decision,key,path+'.last_plan.decision')
    for name,account in (shadow.get('live_readonly_accounts') or {}).get('accounts',{}).items():
        remove(account,'previous_advice','live_readonly_accounts.accounts.'+name)
    remove(shadow,'previous_public_analysis','context')
    return reference,shadow,{'excluded_from_both':excluded,'masked_in_shadow':masked,
        'retained':'Raw market evidence, balances, holdings, fees, executed trades and actual pending bank orders. '
                   'Old prices may still be present as genuine market observations or actual open orders.',
        'limitation':'One pair is exploratory, not causal proof. The shadow lacks prior thesis/deadline context; '
                     'do not execute its protective changes or treat it as a replacement for the normal analysis.'}


def write_private(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')


def source_context(db,key=None):
    with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as conn:
        if key:
            row=conn.execute('SELECT key,value FROM state WHERE key=?',(key,)).fetchone()
        else:
            row=conn.execute("SELECT s.key,s.value FROM ai_views v JOIN state s "
                "ON s.key='ai_model_input:'||v.run_id||':codex' "
                "WHERE v.analyst='codex' AND v.error IS NULL ORDER BY v.rowid DESC LIMIT 1").fetchone()
    if not row:raise RuntimeError('No completed Codex model input available')
    value=json.loads(row[1])
    if len((value.get('trading_accounts') or {}).get('accounts',{}))!=6:
        raise ValueError('Diagnostic requires a frozen six-account input')
    return row[0],value


def reserve_attempt(conn,settings,run_id,label,required=1):
    from .cadence import usage
    from .controls import active
    from .store import utc_now
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if not active(conn,'codex'):raise RuntimeError('Codex is disabled')
        if usage(conn,'codex',utc_now())+required>settings.ai_max_runs_per_day:
            raise RuntimeError('Not enough daily Codex budget for diagnostic')
        return conn.execute('INSERT INTO ai_runs(analyst,started_at,trigger,run_id,status) VALUES(?,?,?,?,?)',
            ('codex',utc_now().isoformat(),'影子对照：'+label,run_id+':'+label,'diagnostic_running')).lastrowid


def run_pair(settings,context,key,output):
    """Only production writes are budget/audit rows in ai_runs; no views/plans/advice."""
    from .ai import AIAnalyst
    from .analysis_archive import AnalysisArchive,export
    from .runtime_resources import slot
    from .store import Store,utc_now
    reference,shadow,manifest=paired_inputs(context)
    run_id='anchor-audit:'+uuid.uuid4().hex
    report={'run_id':run_id,'source_key':key,'snapshot_at':context.get('snapshot_at'),
        'source_sha256':digest(context),'method':manifest,
        'controls':{'search_budget':0,'history_budget':0,'resume':False,'normal_analysis_requests':1},
        'published':False,'execution_invoked':False,'results':[]}
    with slot(settings.db_path) as acquired:
        if not acquired:raise RuntimeError('Model/browser resource slot is busy; retry after it finishes')
        output.mkdir(parents=True,exist_ok=False,mode=0o700)
        os.chmod(output,0o700)
        with sqlite3.connect(settings.db_path,timeout=10) as conn:
            for index,(label,value) in enumerate((('reference',reference),('shadow',shadow))):
                attempt=reserve_attempt(conn,settings,run_id,label,required=2-index)
                write_private(output/(label+'.input.json'),value)
                usage={'input':0,'output':0,'searches':0};started=time.monotonic()
                status='diagnostic_failed';archive=None
                try:
                    with tempfile.TemporaryDirectory(prefix='gold-anchor-'+label+'-') as temp:
                        isolated=replace(settings,db_path=Path(temp)/'diagnostic.db',ai_optimize=True,
                            codex_resume_enabled=False,ai_max_searches=0,ai_history_max_queries=0,
                            clawx_api_key='',anthropic_api_key='')
                        store=Store(isolated.db_path)
                        try:
                            analyst=AIAnalyst(isolated,store,'codex','codex')
                            archive=AnalysisArchive(store,analyst,attempt,run_id+':'+label,'旧价位影子对照',value)
                            usage['_archive']=archive
                            # Direct generation only: deliberately never call analyze()/on_tick()/notifier.
                            _,_,result=analyst._run_codex(value,usage)
                            result=result.model_dump(mode='json')
                            archive.add('model_output',result)
                            status='diagnostic_completed'
                            record={'case':label,'input_sha256':digest(value),'output':result}
                            write_private(output/(label+'.output.json'),record)
                        finally:
                            if archive:
                                archive.finish(status,usage={k:v for k,v in usage.items() if k!='_archive'})
                                export(isolated.db_path,output/(label+'.trace.jsonl'))
                            store.conn.close()
                    record.update({'seconds':round(time.monotonic()-started,3),
                        'usage':{k:v for k,v in usage.items() if k!='_archive'}})
                    report['results'].append(record)
                    print(json.dumps({'case':label,'status':status,'seconds':record['seconds'],
                        'input_tokens':usage.get('input'),'output_tokens':usage.get('output')},ensure_ascii=False),flush=True)
                finally:
                    metrics={k:v for k,v in usage.items() if k!='_archive'}
                    metrics['wall_seconds']=round(time.monotonic()-started,3)
                    with conn:
                        conn.execute('UPDATE ai_runs SET status=?,completed_at=?,metrics_json=? WHERE id=?',
                            (status,utc_now().isoformat(),json.dumps(metrics),attempt))
                    # Keep even a failed/partial pair reviewable; these files contain private data.
                    write_private(output/('progress-'+label+'.json'),report)
        write_private(output/'report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Consume two model calls; generation only, no execution')
    parser.add_argument('--source-key',help='Archived ai_model_input key; defaults to latest completed Codex input')
    parser.add_argument('--output',type=Path,required=True,help='New private output directory (never publish)')
    args=parser.parse_args()
    if not args.run:parser.error('--run is required; this diagnostic consumes two Codex calls')
    from .config import Settings
    settings=Settings();key,context=source_context(settings.db_path,args.source_key)
    run_pair(settings,context,key,args.output)
    print('Private diagnostic saved to',args.output)


if __name__=='__main__':main()
