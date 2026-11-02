"""Local JD H5 read-only discovery. Standalone: needs Playwright, not project credentials."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urlsplit,parse_qs

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
 # Read-only detail requests observed after authentication on the official bank page.
 '/gw2/generic/hj/h5/m/cfQueryConditionWaittingNum',
 '/gw2/generic/hj/h5/m/queryConditionalRuleList',
 '/gw2/generic/jrm/h5/m/queryStdTradeListResourceNew',
 '/gw2/generic/CfGoldWeb/h5/m/cfQueryGoldProductInfo',
 '/gw2/generic/hj/h5/m/getKlineConfig',
 '/gw2/generic/jrm/h5/m/queryTradeDescStd',
 '/gw2/generic/CreatorSer/newh5/m/queryGoldPageDetailComponent',
 '/gw2/generic/CfGoldWeb/newh5/m/cfQueryLastTrade',
 '/gw2/generic/jrm/h5/m/getTradePeriodStd',
 # Product metadata requested by the authenticated bank selector.
 '/gw2/generic/jrm/h5/m/queryProductInfo',
 # Read-only balances used by each bank's official account-management page.
 '/gw2/generic/jrm/h5/m/queryStdAssetsBalV2',
 '/gw/generic/hj/h5/m/queryAccountAssetsV2',
 '/gw2/generic/hj/h5/m/cfGetPriceTrendChart',
 '/gw2/generic/hj/h5/m/bankTagListSort',
 '/gw/generic/aladdin/h5/m/getPageMultiDataForH5',
}
AUTH_HOSTS={'plogin.m.jd.com','passport.jd.com','passport.m.jd.com','iv.jd.com','sso.jd.com'}
# Gold-business SSO callback observed after successful interactive JD authentication.
GOLD_CALLBACK_HOSTS={'plogin.jdjygold.com'}
AUTH_SERVICE_HOSTS={'jcap.m.jd.com','jcapmonitor.m.jd.com','cactus.jd.com','sgm-taihang.jd.com','h5speed.m.jd.com'}
# Public official login scripts call these functions; this gateway also serves commerce.
AUTH_GATEWAY_FUNCTIONS={'h5Config','getCustomCtrl','wsgw_getinfo'}
AUTH_SUPPORT_PATHS={'sgm-m.jd.com':{'/h5/init'},'jra.jd.com':{'/jsTk.do'},
 'uranus.jd.com':{'/log/m','/log/m/v3'},'payrisk.jd.com':{'/m.html','/js/m.js'}}
ACTION=re.compile(r'/(?:buy|sell|pay|payment|submit|create|cancel|redeem|sign|confirm)(?:/|$)',re.I)


def safe_path(url):
 u=urlsplit(url)
 host=u.hostname or ''
 path=re.sub(r'(?<=/)(?:[0-9]{5,}|[a-fA-F0-9]{16,})(?=/|$)','[redacted]',u.path)
 return host+(path if host=='api.jdjygold.com' else '/[page]')  # no queries or authentication paths


def gateway_function_id(url,post_data=None):
 values=list(parse_qs(urlsplit(url).query,keep_blank_values=True).get('functionId',[]))
 if post_data:
  try:
   body=json.loads(post_data)
   if isinstance(body,dict) and 'functionId' in body:values.append(body['functionId'])
  except (ValueError,TypeError):values.extend(parse_qs(post_data,keep_blank_values=True).get('functionId',[]))
 if not values or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',v) for v in values):return None
 return values[0] if len(set(values))==1 else None


def allowed(method,url,resource_type='xhr',login=False,post_data=None):
 u=urlsplit(url);host=u.hostname or '';method=method.upper()
 if u.scheme not in ('https','http','data','blob'):return False
 if u.scheme in ('data','blob'):return method=='GET'
 if u.scheme!='https':return False
 if host=='api.jdjygold.com':return u.path in READ_PATHS and method in ('GET','POST','OPTIONS')
 if login and host=='api.m.jd.com':
  return u.path in ('','/','/client.action') and method in ('GET','POST','OPTIONS') and gateway_function_id(url,post_data) in AUTH_GATEWAY_FUNCTIONS
 if login and host in GOLD_CALLBACK_HOSTS:return method in ('GET','HEAD','OPTIONS')
 if login and host in AUTH_HOSTS:return method in ('GET','POST','HEAD','OPTIONS')
 if login and host in AUTH_SERVICE_HOSTS:return method in ('GET','POST','HEAD','OPTIONS') and not ACTION.search(u.path)
 if login and u.path in AUTH_SUPPORT_PATHS.get(host,set()):return method in ('GET','POST','OPTIONS')
 if method not in ('GET','HEAD','OPTIONS') or ACTION.search(u.path):return False
 if resource_type in ('document','xhr','fetch'):
  return host in ('m.jdjygold.com','m.jr.jd.com','qdsdk.jd.com','storage.360buyimg.com',*AUTH_HOSTS)
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
         'result_code':str(payload.get('resultCode','')) if isinstance(payload.get('resultCode'),(int,str)) and re.fullmatch(r'[0-9]{1,6}',str(payload.get('resultCode'))) else None,
         'auth_codes':{k:str(payload[k]) for k in ('err_code','errcode') if k in payload and re.fullmatch(r'-?[0-9]{1,6}',str(payload[k]))}}


def account_evidence(payload):
 """Evidence only; field presence does not mean that account synchronization works."""
 result=payload.get('resultData') if isinstance(payload,dict) else None
 result=result if isinstance(result,dict) else {}
 data=result.get('datas');data=data if isinstance(data,dict) else {}
 quote=result.get('data');quote=quote if isinstance(quote,dict) else {}
 stock=data.get('goldStockStat');stock=stock if isinstance(stock,dict) else {}
 return {'login_confirmed':data.get('login') if type(data.get('login')) is bool else None,
  'holdings_fields_observed':all(k in stock for k in ('totalGram','availableGram','frozenGram')),
  'product_fields_observed':isinstance(data.get('goldProductInfo'),dict),
  'quote_fields_observed':all(k in quote for k in ('buyPrice','sellPrice','productSku'))}


def summarize_evidence(events):
 evidence={'login_confirmed':None,'holdings_fields_observed':False,'product_fields_observed':False,'quote_fields_observed':False}
 for event in events:
  item=event.get('account_evidence') or {}
  if isinstance(item.get('login_confirmed'),bool):evidence['login_confirmed']=item['login_confirmed']
  for key in ('holdings_fields_observed','product_fields_observed','quote_fields_observed'):evidence[key]=evidence[key] or item.get(key) is True
 return evidence


def wait_for_login(page,read_input=None):
 """Pump Playwright while the terminal waits, so intercepted SMS/QR requests continue."""
 done=threading.Event();errors=[]
 def read():
  try:(read_input or input)()
  except (EOFError,KeyboardInterrupt) as exc:errors.append(exc)
  finally:done.set()
 threading.Thread(target=read,daemon=True).start()
 while not done.is_set():
  if page.is_closed():raise RuntimeError('浏览器已关闭，请重新运行助手。')
  page.wait_for_timeout(100)
 if errors:raise RuntimeError('终端输入已结束，登录检查取消。')


def browser_options(headed=False,channel='chromium'):
 launch={'headless':not headed,'args':['--no-sandbox'] if hasattr(os,'geteuid') and os.geteuid()==0 else []}
 if channel!='chromium':launch['channel']=channel
 if headed:return launch,{'viewport':{'width':1100,'height':850}}
 return launch,{'viewport':{'width':430,'height':932},'is_mobile':True,
  'user_agent':'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1'}


def probe(bank,headed=False,channel='chromium'):
 if headed and sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
  raise RuntimeError('当前Linux没有桌面显示。请在自己的电脑运行--headed登录；ECS只运行后台。')
 from playwright.sync_api import sync_playwright
 events=[]
 with sync_playwright() as p:
  # A temporary context deliberately avoids saving a browser profile or exporting auth state.
  launch,options=browser_options(headed,channel)
  browser=p.chromium.launch(**launch)
  context=browser.new_context(**options)
  def route(r):
   q=r.request
   if not allowed(q.method,q.url,q.resource_type,headed,q.post_data):
    if q.resource_type in ('xhr','fetch','document'):events.append({'blocked':True,'method':q.method,'path':safe_path(q.url),
      'gateway_function':gateway_function_id(q.url,q.post_data) if urlsplit(q.url).hostname=='api.m.jd.com' else None})
    r.abort()
   else:r.continue_()
  context.route('**/*',route)
  page=context.new_page()
  def response(r):
   host=urlsplit(r.url).hostname
   bank_query=host=='api.jdjygold.com' and urlsplit(r.url).path in READ_PATHS
   auth_query=headed and host in (AUTH_HOSTS|GOLD_CALLBACK_HOSTS|AUTH_SERVICE_HOSTS|set(AUTH_SUPPORT_PATHS)|{'api.m.jd.com'})
   if not (bank_query or auth_query):return
   item={'status':r.status,'method':r.request.method,'path':safe_path(r.url)}
   if host=='api.m.jd.com':item['gateway_function']=gateway_function_id(r.url,r.request.post_data)
   try:
    payload=r.json();item.update(response_info(payload))
    if bank_query:item['account_evidence']=account_evidence(payload)
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
    wait_for_login(page)
    page.goto(BANKS[bank]['url'],wait_until='domcontentloaded',timeout=45000);page.wait_for_timeout(3000)
   login_visible='立即登录' in page.locator('body').inner_text(timeout=5000)
  except Exception as exc:error=type(exc).__name__
  finally:context.close();browser.close()
 return {'format_version':5,'bank':bank,'checked_at':datetime.now(timezone.utc).isoformat(),
         'mode':'local_login_schema_review' if headed else 'anonymous_public_probe',
         'login_button_visible':login_visible,'connected':False,'order_submission_enabled':False,
         'error_type':error,'capabilities':summarize_evidence(events),'requests':events}


def write_report(path,report):
 p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)


def main():
 p=argparse.ArgumentParser(description='京东浙商/民生只读查询结构检查，不保存登录状态，不含下单功能')
 p.add_argument('--version',action='version',version='jd-readonly-probe 5')
 p.add_argument('--bank',choices=BANKS,required=True)
 p.add_argument('--headed',action='store_true',help='在自己的电脑打开官方页面，由本人手动登录')
 p.add_argument('--browser',choices=('chromium','chrome','msedge'),default='chromium',help='可用本机已安装的Chrome或Edge，仍使用独立临时会话')
 p.add_argument('--output',type=Path,required=True)
 args=p.parse_args();report=probe(args.bank,args.headed,args.browser);write_report(args.output,report)
 print('报告已保存：',args.output,'；尚未接通账户同步，请审核查询字段后再配置适配器。')


if __name__=='__main__':main()
