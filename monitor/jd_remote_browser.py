"""Temporary ECS JD read-only browser, via SSH or authenticated HTTPS."""
import argparse
from datetime import datetime,timezone
import json
import ipaddress
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time

from .jd_readonly_probe import BANKS,READ_PATHS,AUTH_HOSTS,GOLD_CALLBACK_HOSTS,AUTH_SERVICE_HOSTS,AUTH_SUPPORT_PATHS,allowed,safe_path,gateway_function_id,response_info,account_evidence,summarize_evidence

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'monitor/data/jd-remote-browser'
RUNTIME=Path('/run/gold-jd-readonly')


def private_write(path,value):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('Refusing symlink output')
    temp=path.with_name(path.name+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(value)
    os.replace(temp,path)


def ensure_private(path):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('Refusing symlink directory')
    path.mkdir(mode=0o700,parents=True,exist_ok=True);path.chmod(0o700)


def free_port(port):
    with socket.socket() as s:s.bind(('127.0.0.1',port))


def wait_port(port,process,seconds=20):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if process.poll() is not None:raise RuntimeError('Browser component exited during startup')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.3):return
        except OSError:time.sleep(.2)
    raise RuntimeError('Browser component startup timed out')


def public_config(port,backend,host,dashboard_auth=False):
    host=str(ipaddress.IPv4Address(host))
    return f'''user root;
worker_processes 1;
pid {RUNTIME}/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
 access_log off;
 server {{
  listen 0.0.0.0:{port} ssl;
  server_name {host};
  ssl_certificate {RUNTIME}/tls.crt;
  ssl_certificate_key {RUNTIME}/tls.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  error_page 497 =307 https://{host}:{port}$request_uri;
  auth_basic "{'Gold dashboard login' if dashboard_auth else 'Temporary JD browser'}";
  auth_basic_user_file {RUNTIME}/http.pass;
  location = / {{ return 302 /vnc.html?autoconnect=1&resize=scale; }}
  location / {{
   proxy_pass http://127.0.0.1:{backend};
   proxy_http_version 1.1;
   proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection "upgrade";
   proxy_read_timeout 3600s;
   proxy_buffering off;
  }}
 }}
}}
'''


def access_credentials(public_host,dashboard_auth=False):
    if dashboard_auth:
        if not public_host:raise ValueError('Dashboard authentication requires HTTPS')
        from .config import Settings
        Settings()  # Load the same local configuration used by the dashboard.
        password=os.environ.get('GOLD_WEB_PASSWORD','').strip()
        if not password:raise ValueError('Dashboard password is not configured')
        return password,'dashboard'
    return (secrets.token_urlsafe(18),'temporary') if public_host else (secrets.token_urlsafe(6),'ssh')


def child_environment(display=None):
    env=dict(os.environ)
    for key in list(env):
        if any(word in key.upper() for word in ('PROXY','TOKEN','PASSWORD','SECRET','API_KEY','SENDKEY')):env.pop(key,None)
    if display:env['DISPLAY']=display
    return env


def run(port=1455,ttl=3600,public_host=None,dashboard_auth=False):
    if not 1024<=port<=65535 or not 60<=ttl<=3600:raise ValueError('Invalid port or lifetime')
    backend=1456 if public_host else port
    if public_host:ipaddress.IPv4Address(public_host)
    for p in set((port,backend,5905,9225)):free_port(p)
    ensure_private(DATA);ensure_private(RUNTIME)
    profile=DATA/'chrome-profile';ensure_private(profile);ensure_private(profile/'Default')
    prefs={'credentials_enable_service':False,'profile':{'password_manager_enabled':False},
           'autofill':{'profile_enabled':False,'credit_card_enabled':False}}
    # Only this dedicated profile; never attach the user's regular browser.
    pref_path=profile/'Default/Preferences'
    if not pref_path.exists():private_write(pref_path,json.dumps(prefs))
    password,auth_mode=access_credentials(public_host,dashboard_auth)
    vnc_password=None if public_host else password[:8]
    if vnc_password:
        subprocess.run(['x11vnc','-storepasswd',str(RUNTIME/'vnc.pass')],input=vnc_password+'\n'+vnc_password+'\ny\n',text=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
        (RUNTIME/'vnc.pass').chmod(0o600)
    if public_host:
        import crypt
        private_write(RUNTIME/'http.pass','jd:'+crypt.crypt(password,crypt.mksalt(crypt.METHOD_SHA512))+'\n')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
            '-subj','/CN='+public_host,'-addext','subjectAltName=IP:'+public_host,
            '-keyout',str(RUNTIME/'tls.key'),'-out',str(RUNTIME/'tls.crt')],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=20)
        private_write(RUNTIME/'nginx.conf',public_config(port,backend,public_host,dashboard_auth))
    url=f'https://{public_host}:{port}/' if public_host else f'http://127.0.0.1:{port}/vnc.html?autoconnect=1&resize=scale'
    password_hint='use existing dashboard password' if dashboard_auth else password
    private_write(RUNTIME/'access.txt',f'URL: {url}\nUsername: jd\nAuthentication: {auth_mode}\nAccess password: {password_hint}\nVNC password: {"not required" if public_host else vnc_password}\nLifetime: {ttl} seconds\n')
    children=[];stopping=False;events=[];started=time.monotonic();stage='display';page_load_state='loading'
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def spawn(command,**kwargs):
        kwargs.setdefault('env',child_environment())
        child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kwargs)
        children.append(child);return child
    def save(status,error=None):
        report={'mode':'ecs_readonly_browser','status':status,'checked_at':datetime.now(timezone.utc).isoformat(),
                'port':port,'bind':'0.0.0.0' if public_host else '127.0.0.1','https':bool(public_host),
                'authentication':auth_mode,
                'expires_in_seconds':max(0,int(ttl-(time.monotonic()-started))),
                'connected':False,'order_submission_enabled':False,'capabilities':summarize_evidence(events),
                'error_type':error,'stage':stage,'page_load_state':page_load_state,
                'components':{Path(c.args[0]).name:c.poll() for c in children},'requests':events[-500:]}
        private_write(DATA/'status.json',json.dumps(report,ensure_ascii=False,indent=2))
    try:
        save('starting')
        display=':105'
        if Path('/tmp/.X11-unix/X105').exists():raise RuntimeError('Display already in use')
        xvfb=spawn(['Xvfb',display,'-screen','0','1200x850x24','-nolisten','tcp','-noreset'])
        deadline=time.monotonic()+10
        while not Path('/tmp/.X11-unix/X105').exists():
            if xvfb.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Display unavailable')
            time.sleep(.1)
        env=child_environment(display)
        stage='chrome'
        chrome=spawn(['/usr/bin/google-chrome','--no-sandbox','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check',
             '--disable-sync','--disable-background-networking','--disable-component-update',
             '--disable-gpu','--enable-low-end-device-mode','--renderer-process-limit=2',
             '--disable-features=BackForwardCache,MediaRouter,OptimizationHints',
             '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9225',
             '--user-data-dir='+str(profile),'--window-size=1200,850','about:blank'],env=env)
        wait_port(9225,chrome)
        stage='readonly_guard'
        from playwright.sync_api import sync_playwright,Error as BrowserError
        with sync_playwright() as p:
            browser=p.chromium.connect_over_cdp('http://127.0.0.1:9225')
            context=browser.contexts[0]
            # Existing service workers must not bypass the request route on profile reuse.
            def guard_page(page):
                cdp=context.new_cdp_session(page)
                cdp.send('Network.enable')
                cdp.send('Network.setBypassServiceWorker',{'bypass':True})
            context.on('page',guard_page)
            for existing in context.pages:guard_page(existing)
            def route(r):
                q=r.request
                if allowed(q.method,q.url,q.resource_type,True,q.post_data):r.continue_()
                else:
                    if q.resource_type in ('xhr','fetch','document'):
                        events.append({'blocked':True,'method':q.method,'path':safe_path(q.url),
                                       'gateway_function':gateway_function_id(q.url,q.post_data)})
                    r.abort()
            context.route('**/*',route)
            def response(r):
                from urllib.parse import urlsplit
                host=urlsplit(r.url).hostname
                bank_query=host=='api.jdjygold.com' and urlsplit(r.url).path in READ_PATHS
                if not (bank_query or host in AUTH_HOSTS|GOLD_CALLBACK_HOSTS|AUTH_SERVICE_HOSTS|set(AUTH_SUPPORT_PATHS)|{'api.m.jd.com'}):return
                item={'method':r.request.method,'path':safe_path(r.url),'status':r.status}
                try:
                    payload=r.json();item.update(response_info(payload))
                    if bank_query:
                        item['account_evidence']=account_evidence(payload)
                        if item['account_evidence'].get('login_confirmed') is True:
                            # Queue a fresh two-bank sync after user authentication; no SMS or trading requests.
                            now=time.monotonic()
                            if now-response.last_sync_request>60:
                                from .config import Settings
                                from .store import Store
                                from .jd_live_sync import request_refresh
                                store=Store(Settings().db_path)
                                try:request_refresh(store.conn)
                                finally:store.conn.close()
                                response.last_sync_request=now
                except Exception:item['response_format']='unreadable'
                events.append(item)
            response.last_sync_request=0
            context.on('response',response)
            # The read-only guard is active before any JD page is opened or VNC is reachable.
            stage='vnc'
            # Public HTTPS authenticates both HTTP and WebSocket. Avoid a second VNC password prompt.
            vnc_auth=['-nopw'] if public_host else ['-rfbauth',str(RUNTIME/'vnc.pass')]
            vnc=spawn(['x11vnc','-display',display,'-localhost','-listen','127.0.0.1','-rfbport','5905',
                *vnc_auth,'-forever','-shared','-noxdamage','-repeat','-quiet'])
            wait_port(5905,vnc)
            stage='novnc'
            web=spawn(['websockify','--web=/usr/share/novnc','127.0.0.1:'+str(backend),'127.0.0.1:5905'])
            wait_port(backend,web)
            if public_host:
                stage='https'
                proxy=spawn(['/usr/sbin/nginx','-p',str(RUNTIME)+'/', '-c',str(RUNTIME/'nginx.conf'),'-g','daemon off;'])
                wait_port(port,proxy)
            page=context.pages[0] if context.pages else context.new_page()
            stage='jd_page'
            try:
                # Slow advertising/analytics must not tear down a usable login channel.
                page.goto(BANKS['zheshang']['url'],wait_until='commit',timeout=20000)
                page_load_state='opened'
            except BrowserError:page_load_state='reload_available'
            stage='interactive';save('ready');last=0
            while not stopping and time.monotonic()-started<ttl:
                if not browser.is_connected() or any(child.poll() is not None for child in children):raise RuntimeError('Browser session ended')
                # Keep dispatching intercepted login/SMS requests while the user operates VNC.
                page.wait_for_timeout(100)
                if time.monotonic()-last>3:
                    save('ready');last=time.monotonic()
            save('expired' if not stopping else 'stopped')
    except Exception as exc:
        if stopping:save('stopped')
        else:
            save('error',type(exc).__name__)
            raise RuntimeError('Remote browser failed; see private status.json') from None
    finally:
        # Tear down every child before the controller exits, including Chrome and its VNC display.
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in reversed(children):
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        for name in ('vnc.pass','access.txt','http.pass','tls.key','tls.crt','nginx.conf'):(RUNTIME/name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description='Temporary ECS JD browser; SSH or authenticated HTTPS')
    parser.add_argument('--port',type=int,default=1455);parser.add_argument('--ttl',type=int,default=3600)
    parser.add_argument('--public-host',help='Optional public IPv4; enables authenticated HTTPS')
    parser.add_argument('--dashboard-auth',action='store_true',help='Use the configured dashboard password instead of a separate temporary password')
    args=parser.parse_args();run(args.port,args.ttl,args.public_host,args.dashboard_auth)


if __name__=='__main__':main()
