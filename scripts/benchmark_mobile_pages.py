"""Compare legacy/scoped page reads. Only saves timings and sizes, never account payloads."""
import argparse
import gzip
import json
from pathlib import Path
from statistics import median
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor.config import Settings
from monitor.page_data import page_data

CASES=(
    ('overview','overview','auto','summary'),
    ('simulation_list','strategies','simulation','simulation-list'),
    ('real_account_list','strategies','live','live-list'),
    ('ai_research','ai','auto','research'),
    ('ai_controls','ai','auto','control'),
    ('notification_settings','notifications','auto','settings'),
)

def measure(settings,page,view,rounds):
    samples=[]
    for _ in range(rounds):
        started=time.perf_counter()
        value=page_data(settings,page,view)
        body=json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode()
        samples.append((time.perf_counter()-started)*1000)
    return {'query_ms_median':round(median(samples),2),'json_bytes':len(body),'gzip_bytes':len(gzip.compress(body))}

def benchmark(settings,rounds=3):
    result={'scope':'same ECS, current SQLite read-only queries; no HTTP or browser rendering',
            'rounds':rounds,'cases':[]}
    for name,page,legacy,scoped in CASES:
        before=measure(settings,page,legacy,rounds)
        after=measure(settings,page,scoped,rounds)
        result['cases'].append({'name':name,'legacy_view':legacy,'scoped_view':scoped,
                               'before':before,'after':after,
                               'json_reduction_pct':round(100*(1-after['json_bytes']/before['json_bytes']),1)})
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--output')
    args=parser.parse_args();result=benchmark(Settings(),max(1,args.rounds));body=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(body+'\n')
    print(body)
