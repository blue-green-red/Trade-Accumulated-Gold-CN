"""Anonymous browser measurements, public paths only; no model calls or credentials."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor.config import Settings
from monitor.runtime_resources import slot


def benchmark(url):
    from playwright.sync_api import sync_playwright
    results=[]
    with slot(Settings().db_path) as acquired:
        if not acquired:raise SystemExit('Model/browser busy; defer public browser benchmark')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            try:
                for view in ('market','analysis','recommendations'):
                    context=browser.new_context(viewport={'width':390,'height':844})
                    page=context.new_page();requests=[];errors=[]
                    page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
                    started=time.perf_counter()
                    page.goto(url.rstrip('/')+'/#'+view,wait_until='domcontentloaded')
                    page.locator('[data-public-view="'+view+'"][data-ready="true"]').wait_for(timeout=20000)
                    ready_ms=round((time.perf_counter()-started)*1000,1)
                    page.wait_for_timeout(100)
                    assets=page.evaluate("performance.getEntriesByType('resource').map(r=>({path:new URL(r.name).pathname,bytes:r.transferSize,duration_ms:Math.round(r.duration)}))")
                    api=[r for r in requests if '/api/' in r]
                    assert all('/api/public/' in r for r in api),'Unexpected private API request'
                    assert not errors,errors
                    results.append({'view':view,'data_ready_ms':ready_ms,'api_requests':[r.split('/api/')[1] for r in api],
                                    'resource_transfer_bytes':sum(a['bytes'] for a in assets),'resources':assets})
                    if view=='analysis':
                        before=len(api);start=time.perf_counter()
                        page.get_by_role('button',name='仓位推荐',exact=True).click()
                        page.locator('.target-flow').wait_for(timeout=10000)
                        results[-1]['cached_recommendations_ms']=round((time.perf_counter()-start)*1000,1)
                        results[-1]['extra_api_requests_on_switch']=len([r for r in requests if '/api/' in r])-before
                    context.close()
            finally:browser.close()
    return {'url':url,'environment':'ECS local headless Chromium, anonymous fresh browser contexts; not an external user network measurement',
            'results':results}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='https://gold.goalgoal.fun/public/');parser.add_argument('--output');args=parser.parse_args()
    value=benchmark(args.url)
    if args.output:
        target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'views':[{k:v for k,v in row.items() if k!='resources'} for row in value['results']]},ensure_ascii=False))
