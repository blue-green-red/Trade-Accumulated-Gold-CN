"""Compressed, append-only application-visible analysis traces and streaming JSONL export."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from datetime import datetime,timezone

SCHEMA='''
CREATE TABLE IF NOT EXISTS ai_analysis_archives (
 id INTEGER PRIMARY KEY, analyst TEXT NOT NULL, run_id TEXT, backend TEXT NOT NULL,
 model TEXT, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
 view_id INTEGER, metadata BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_analysis_artifacts (
 id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
 sha256 TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_artifacts ON ai_analysis_artifacts(archive_id,id);
'''


def plain(value):
    if hasattr(value,'model_dump'):return plain(value.model_dump(mode='json'))
    if isinstance(value,dict):return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [plain(v) for v in value]
    if isinstance(value,SimpleNamespace):return plain(vars(value))
    if isinstance(value,(datetime,Path)):return str(value)
    if isinstance(value,bytes):return value.decode('utf-8',errors='replace')
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise TypeError('Unsupported archive value: '+type(value).__name__)


def encode(value):
    raw=json.dumps(plain(value),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    return gzip.compress(raw,mtime=0),hashlib.sha256(raw).hexdigest()


def decode(value):return json.loads(gzip.decompress(value))


def now():return datetime.now(timezone.utc).isoformat()


class AnalysisArchive:
    def __init__(self,store,analyst,attempt,run_id,trigger,context):
        self.db=store.path if hasattr(store,'path') else analyst.settings.db_path
        self.secrets=sorted({v for k,v in os.environ.items() if len(v)>=8 and any(x in k.upper() for x in ('TOKEN','PASSWORD','API_KEY','SECRET'))},key=len,reverse=True)
        settings=analyst.settings
        root=Path(__file__).parent
        hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
                ('ai.py','autonomous.py','ai_inputs.py','codex_runtime.py','history_tool.py','risk_profiles.py')}
        meta={'format_version':1,'attempt_id':attempt,'trigger':trigger,'snapshot_at':context.get('snapshot_at'),
              'trading_mode':settings.ai_trading_mode,'ai_optimize':settings.ai_optimize,
              'search_budget':settings.ai_max_searches,'history_budget':settings.ai_history_max_queries,
              'source_hashes':hashes,'visibility':'application-visible only; provider-internal prompts/state and unreturned search documents are unavailable'}
        with sqlite3.connect(self.db,timeout=10) as c:
            c.executescript(SCHEMA)
            self.id=c.execute('INSERT INTO ai_analysis_archives(analyst,run_id,backend,model,started_at,status,metadata) VALUES(?,?,?,?,?,?,?)',
                (analyst.analyst,run_id,analyst.backend,analyst.model,now(),'running',encode(self.clean(meta))[0])).lastrowid
        self.add('input_context',context)

    def clean(self,value):
        value=plain(value)
        if isinstance(value,dict):
            return {k:('[REDACTED]' if re.search(r'(?i)(authorization|password|api[_-]?key|access[_-]?token|secret)$',k) else self.clean(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.clean(v) for v in value]
        if isinstance(value,str):
            for secret in self.secrets:value=value.replace(secret,'[REDACTED]')
        return value

    def add(self,kind,value):
        payload,digest=encode(self.clean(value))
        with sqlite3.connect(self.db,timeout=10) as c:
            c.execute('INSERT INTO ai_analysis_artifacts(archive_id,ts,kind,sha256,payload) VALUES(?,?,?,?,?)',
                      (self.id,now(),kind,digest,payload))

    def finish(self,status,**result):
        self.add('outcome',{'status':status,**result})
        with sqlite3.connect(self.db,timeout=10) as c:
            c.execute('UPDATE ai_analysis_archives SET status=?,completed_at=?,view_id=? WHERE id=?',
                      (status,now(),result.get('view_id'),self.id))


def record(usage,kind,value):
    archive=usage.get('_archive')
    if archive:archive.add(kind,value)


def export(db,output,analyst=None):
    """All outcomes exported, including failed/invalidated runs; never label these as wins."""
    target=Path(output);target.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    count=0
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out, sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            # One read transaction keeps archives, outcomes and execution labels consistent.
            c.execute('BEGIN')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'ai_analysis_archives' not in tables:return 0
            for row in c.execute('SELECT * FROM ai_analysis_archives'+(' WHERE analyst=?' if analyst else '')+' ORDER BY id',(analyst,) if analyst else ()):
                value=dict(row);value['metadata']=decode(value['metadata'])
                artifacts=[]
                for r in c.execute('SELECT id,ts,kind,sha256,payload FROM ai_analysis_artifacts WHERE archive_id=? ORDER BY id',(row['id'],)):
                    item=dict(r);raw=gzip.decompress(item.pop('payload'))
                    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Archive checksum mismatch')
                    item['data']=json.loads(raw);artifacts.append(item)
                value['artifacts']=artifacts
                view_id=row['view_id']
                # Labels are an export-time snapshot, not part of historical model inputs.
                value['execution_as_of']=now()
                for name,table in (('plans','ai_plans'),('fills','ai_portfolio_fills')):
                    value[name]=[dict(r) for r in c.execute('SELECT * FROM '+table+' WHERE view_id=? ORDER BY id',(view_id,))] if view_id and table in tables else []
                # Deleted experiments may reuse ai_views rowids; match model + run identifier/time.
                valid=False
                if view_id and 'ai_views' in tables:
                    v=c.execute('SELECT analyst,run_id,started_at FROM ai_views WHERE rowid=?',(view_id,)).fetchone()
                    outcome=next((a['data'] for a in reversed(artifacts) if a['kind']=='outcome'),{})
                    valid=bool(v and v['analyst']==row['analyst'] and v['run_id']==row['run_id'] and v['started_at']==outcome.get('started_at'))
                value['execution_link_valid']=valid
                if not valid:value['plans']=[];value['fills']=[]
                out.write(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n');count+=1
        return count
    except BaseException:
        target.unlink(missing_ok=True);raise


def main():
    p=argparse.ArgumentParser(description='Export archived AI inputs/results and current execution labels, without model calls')
    p.add_argument('--db',type=Path,default=Path('monitor/data/monitor.db'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--analyst',choices=('codex','claude'))
    args=p.parse_args();print('Exported',export(args.db,args.output,args.analyst),'analyses to',args.output)


if __name__=='__main__':main()
