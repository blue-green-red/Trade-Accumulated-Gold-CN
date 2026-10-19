"""Compare real Codex CLI modes on frozen inputs; no live-account writes or market requests."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from .ai import AIAnalyst
from .config import Settings
from .store import Store,utc_now
from .web import connect


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warm-runs',type=int,default=2)
    parser.add_argument('--output',type=Path,default=Path('monitor/data/ai-speed-benchmark.json'))
    args=parser.parse_args()
    original=Settings();conn=connect(original.db_path)
    try:
        rows=conn.execute("SELECT key,value FROM state WHERE key LIKE 'ai_input:%' ORDER BY updated_at DESC").fetchall()
        context=None
        for row in rows:
            run_id=row['key'][len('ai_input:'):]
            account=conn.execute('SELECT value FROM state WHERE key=?',('ai_account_input:'+run_id+':codex',)).fetchone()
            if account:
                context=json.loads(row['value'])['context'];context['trading_accounts']=json.loads(account[0]);break
        if context is None: raise RuntimeError('No archived six-account Codex input available')
    finally:conn.close()
    # The payload is identical for all cases. Only prompt/packing/session policy changes.
    results=[]
    with tempfile.TemporaryDirectory(prefix='gold-benchmark-') as temp:
        settings=replace(original,db_path=Path(temp)/'evaluation.db',ai_max_searches=0,clawx_api_key='',anthropic_api_key='')
        store=Store(settings.db_path)
        analyst=AIAnalyst(settings,store,'codex','codex')
        cases=[('baseline',False),('optimized_cold',True)]+[(f'optimized_warm_{i+1}',True) for i in range(max(1,min(3,args.warm_runs)))]
        try:
            for label,optimize in cases:
                analyst.settings=replace(settings,ai_optimize=optimize,codex_resume_enabled=optimize)
                usage={};started=time.monotonic()
                _,_,result=analyst._run_codex(context,usage)
                record={'case':label,**usage.pop('performance',{}),'elapsed_seconds':round(time.monotonic()-started,3),
                        'input_tokens':usage.get('input',0),'cached_input_tokens':usage.get('cached'),
                        'output_tokens':usage.get('output',0),'searches':usage.get('searches',0),
                        'accounts_validated':len(result.account_decisions.model_dump())}
                results.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
                report={'tested_at':utc_now().isoformat(),'model':original.codex_model or 'CLI default',
                    'method':'same frozen six-account snapshot; searches disabled for every case; isolated database; no execution',
                    'results':results,'live_accounts_modified':False,'jd_reference_requests':0}
                args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        finally:store.conn.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
