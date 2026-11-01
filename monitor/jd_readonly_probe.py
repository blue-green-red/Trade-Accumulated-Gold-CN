"""Local JD H5 read-only discovery. Standalone: needs Playwright, not project credentials."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

BANKS={
 'zheshang':{'name':'浙商积存金','url':'https://m.jdjygold.com/finance-gold/gold-standard/home/?productSku=1961543816'},
 'minsheng':{'name':'民生积存金','url':'https://m.jdjygold.com/finance-gold/newgold/home/'},
}
# Exact paths observed on the official H5 pages. No order, payment or account-opening APIs.
READ_PATHS={
 '/gw/generic/hj/h5/m/latestPrice',
 '/gw2/generic/6440/h5/m/queryStallForGoldNoLogin',
 '/gw2/generic/6440/h5/m/queryStallForGold',
 '/gw2/generic/jrm/h5/m/getTotalHoldingStat',
 '/gw/generic/jrm/h5/m/queryStallNew',
 '/gw2/generic/jrm/newh5/m/indexDataStd',
 '/gw2/generic/jrm/newh5/m/indexData',
 '/gw2/generic/hj/h5/m/cfGetLatestPriceInfo',
}
AUTH_HOSTS={'plogin.m.jd.com','passport.jd.com','passport.m.jd.com','iv.jd.com'}
ACTION=re.compile(r'/(?:buy|sell|pay|payment|submit|create|cancel|redeem|sign|confirm)(?:/|$)',re.I)


def safe_path(url):
 u=urlsplit(url)
 host=u.hostname or ''
 path=re.sub(r'(?<=/)(?:[0-9]{5,}|[a-fA-F0-9]{16,})(?=/|$)','[redacted]',u.path)
 return host+(path if host=='api.jdjygold.com' else '/[page]')  # no queries or authentication paths


def allowed(method,url,resource_type='xhr',login=False):
 u=urlsplit(url);host=u.hostname or '';method=method.upper()
 if u.scheme not in ('https','http','data','blob'):return False
 if u.scheme in ('data','blob'):return method=='GET'
 if u.scheme!='https':return False
 if host=='api.jdjygold.com':return u.path in READ_PATHS and method in ('GET','POST','OPTIONS')
 if login and host in AUTH_HOSTS:return True  # user-operated official authentication only
 if login and host=='sgm-m.jd.com' and u.path=='/h5/init':return method in ('POST','OPTIONS')
 if method not in ('GET','HEAD','OPTIONS') or ACTION.search(u.path):return False
 if resource_type in ('document','xhr','fetch'):
  return host in ('m.jdjygold.com','m.jr.jd.com',*AUTH_HOSTS)
 return host.endswith(('.jd.com','.360buyimg.com','.jdjygold.com'))


def shape(value,depth=0):
 """Field names/types only: no balances, identifiers, auth tokens or personal values."""
 if depth>=5:return type(value).__name__
 if isinstance(value,dict):return {(str(k) if re.fullmatch(r'[A-Za-z_]{1,48}',str(k)) else '[dynamic_key]'):shape(v,depth+1) for k,v in list(value.items())[:80]}
 if isinstance(value,list):return [shape(value[0],depth+1)] if value else []
 return type(value).__name__


def response_info(payload):
 if not isinstance(payload,dict):return {'shape':shape(payload)}
 return {'shape':shape(payload),'success':payload.get('success') if type(payload.get('success')) is bool else None,
         'result_code':str(payload.get('resultCode','')) if isinstance(payload.get('resultCode'),(int,str)) and re.fullmatch(r'[0-9]{1,6}',str(payload.get('resultCode'))) else None}


def probe(bank,headed=False):
 if headed and sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
  raise RuntimeError('当前Linux没有桌面显示。请在自己的电脑运行--headed登录；ECS只运行后台。')
 from playwright.sync_api import sync_playwright
 events=[]
 with sync_playwright() as p:
  # A temporary context deliberately avoids saving a browser profile or exporting auth state.
  browser=p.chromium.launch(headless=not headed,args=['--no-sandbox'] if hasattr(os,'geteuid') and os.geteuid()==0 else [])
  context=browser.new_context(viewport={'width':430,'height':932},is_mobile=True,
    user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1')
  def route(r):
   q=r.request
   if not allowed(q.method,q.url,q.resource_type,headed):
    if q.resource_type in ('xhr','fetch','document'):events.append({'blocked':True,'method':q.method,'path':safe_path(q.url)})
    r.abort()
   else:r.continue_()
  context.route('**/*',route)
  page=context.new_page()
  def response(r):
   if urlsplit(r.url).hostname!='api.jdjygold.com' or urlsplit(r.url).path not in READ_PATHS:return
   item={'status':r.status,'method':r.request.method,'path':safe_path(r.url)}
   try:item.update(response_info(r.json()))
   except Exception:item['response_format']='unreadable'
   events.append(item)
  page.on('response',response)
  error=None;login_visible=None
  try:
   page.goto(BANKS[bank]['url'],wait_until='domcontentloaded',timeout=45000)
   page.wait_for_timeout(4000)
   if headed:
    print('请在京东官方页面自行登录。不要买卖、开户、签约或输入支付密码。')
    print('查看账户首页后回到此终端按回车，生成仅含字段结构的报告。')
    input()
    page.reload(wait_until='domcontentloaded',timeout=45000);page.wait_for_timeout(3000)
   login_visible='立即登录' in page.locator('body').inner_text(timeout=5000)
  except Exception as exc:error=type(exc).__name__
  finally:context.close();browser.close()
 return {'format_version':1,'bank':bank,'checked_at':datetime.now(timezone.utc).isoformat(),
         'mode':'local_login_schema_review' if headed else 'anonymous_public_probe',
         'login_button_visible':login_visible,'connected':False,'order_submission_enabled':False,
         'error_type':error,'requests':events}


def write_report(path,report):
 p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)


def main():
 p=argparse.ArgumentParser(description='京东浙商/民生只读查询结构检查，不保存登录状态，不含下单功能')
 p.add_argument('--bank',choices=BANKS,required=True)
 p.add_argument('--headed',action='store_true',help='在自己的电脑打开官方页面，由本人手动登录')
 p.add_argument('--output',type=Path,required=True)
 args=p.parse_args();report=probe(args.bank,args.headed);write_report(args.output,report)
 print('报告已保存：',args.output,'；尚未接通账户同步，请审核查询字段后再配置适配器。')


if __name__=='__main__':main()
