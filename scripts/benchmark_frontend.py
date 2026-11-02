"""Read-only browser benchmark. Does not submit controls or save account payloads."""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor.config import Settings


def benchmark_in_browser(browser,url,rounds=5,latency_ms=0,kbps=0,switches=False):
    # Playwright HTTP-auth interception disables Chromium's cache. A preemptive header
    # reproduces a browser whose user has already logged in, without interception.
    auth='Basic '+base64.b64encode(('viewer:'+os.environ.get('GOLD_WEB_PASSWORD','').strip()).encode()).decode()
    context=browser.new_context(extra_http_headers={'Authorization':auth},viewport={'width':390,'height':844})
    samples=[]
    transitions=[]
    try:
        page=context.new_page()
        origin=urlsplit(url)
        # Initialize the browser renderer without fetching any application code/data.
        page.goto(f'{origin.scheme}://{origin.netloc}/__benchmark_warmup',wait_until='load')
        if latency_ms or kbps:
            session=context.new_cdp_session(page)
            session.send('Network.enable')
            session.send('Network.emulateNetworkConditions',{'offline':False,'latency':latency_ms,'downloadThroughput':kbps*1000/8 if kbps else -1,'uploadThroughput':kbps*1000/8 if kbps else -1})
        for i in range(rounds):
            if i:page.reload(wait_until='load',timeout=30000)
            else:page.goto(url,wait_until='load',timeout=30000)
            page.wait_for_function("document.querySelector('[data-page-ready=\"true\"]') || (typeof DATA!=='undefined' && DATA && document.querySelector('#panel-overview .card'))",timeout=30000)
            sample=page.evaluate('''()=>{const n=performance.getEntriesByType('navigation')[0],r=performance.getEntriesByType('resource'),all=[n,...r];return {ready_ms:Math.round(performance.now()),ttfb_ms:Math.round(n.responseStart),dom_ms:Math.round(n.domContentLoadedEventEnd),wire_KiB:+(all.reduce((a,x)=>a+x.transferSize,0)/1024).toFixed(1),requests:all.length,network_requests:all.filter(x=>x.transferSize>0).length,assets_from_cache:r.filter(x=>x.name.includes('/assets/')&&!x.transferSize).length}}''')
            sample['visit']='cold' if not i else 'warm'
            samples.append(sample)
        if switches:
            for label,key in (('账户','strategies'),('操盘对比','compare'),('AI','ai'),('行情','market'),('操盘记录','records'),('通知','notifications'),('总览','overview')):
                before=page.evaluate("performance.getEntriesByType('resource').length")
                started=time.perf_counter();page.get_by_role('tab',name=label,exact=True).click()
                page.locator('#panel-'+key+'[data-page-ready="true"]').wait_for()
                if key=='strategies':page.locator('#panel-strategies .live-bank,#panel-strategies .asset-account').first.wait_for()
                if key=='records':page.locator('#panel-records .trade-record').first.wait_for()
                transitions.append({'page':key,'ready_ms':round((time.perf_counter()-started)*1000),'requests':page.evaluate("performance.getEntriesByType('resource').length")-before})
    finally:context.close()
    return {'latency_ms':latency_ms,'kbps':kbps,'samples':samples,'transitions':transitions}


def benchmark(url,rounds=5,latency_ms=0,kbps=0,switches=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:return benchmark_in_browser(browser,url,rounds,latency_ms,kbps,switches)
        finally:browser.close()


if __name__=='__main__':
    Settings()
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:'+os.environ.get('GOLD_WEB_PORT','8080')+'/#overview')
    parser.add_argument('--rounds',type=int,default=5)
    parser.add_argument('--latency-ms',type=int,default=0)
    parser.add_argument('--kbps',type=int,default=0)
    parser.add_argument('--output')
    parser.add_argument('--switches',action='store_true')
    args=parser.parse_args()
    result=benchmark(args.url,args.rounds,args.latency_ms,args.kbps,args.switches)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
