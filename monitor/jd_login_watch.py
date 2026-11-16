"""Hourly read-only login checks and bounded viewer recovery; no account trades or AI calls."""
from datetime import datetime,timedelta,timezone
import hashlib
import http.client
import json
import ssl
import subprocess
import uuid

from .notification_control import LOGIN_TOPIC
from .store import utc_now

KEY='jd_login_watch'
INTERVAL=3600
LOGIN_URL='https://8.218.117.20:1455/'


def stamp(value):
    try:
        result=datetime.fromisoformat(value)
        return result if result.tzinfo else None
    except (ValueError,TypeError):return None


def enabled(conn):
    row=conn.execute('SELECT value FROM state WHERE key=?',('notify:'+LOGIN_TOPIC,)).fetchone()
    return bool(row and json.loads(row[0]).get('enabled') is True)


def bank_states(conn):
    from .jd_live_readonly import enabled_banks
    names=enabled_banks(conn)
    if not names:return []
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'jd_live_readonly' not in tables:return [(b,None,'unknown') for b in names]
    return [(b,*(conn.execute('SELECT checked_at,public_status FROM jd_live_readonly WHERE bank=?',(b,)).fetchone() or (None,'unknown'))) for b in names]


def entry_info(db_path,now):
    """Local TLS readiness, not a guess from systemd's activating/active state."""
    conn=None
    try:
        value=json.loads((db_path.parent/'jd-remote-browser/status.json').read_text())
        checked=stamp(value.get('checked_at'))
        if not checked or value.get('status') not in ('starting','ready') or not value.get('https'):return None
        if not 0<=(now-checked).total_seconds()<90:return None
        remaining=float(value.get('expires_in_seconds',0))-(now-checked).total_seconds()
        if remaining<120:return None
        conn=http.client.HTTPSConnection('127.0.0.1',1455,timeout=.7,context=ssl._create_unverified_context())
        conn.connect();identity=hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()[:24]
        conn.request('GET','/vnc.html');response=conn.getresponse();code=response.status;response.read(32)
        if code not in (200,401):return None
        return {'id':identity,'url':LOGIN_URL,'expires_at':(now+timedelta(seconds=remaining)).isoformat()}
    except (OSError,ValueError,TypeError,KeyError,http.client.HTTPException):return None
    finally:
        if conn:conn.close()


def start_entry():
    subprocess.run(['systemctl','start','--no-block','gold-jd-login-auto'],check=True,timeout=5,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def stop_entry():
    subprocess.run(['systemctl','stop','--no-block','gold-jd-login-auto'],check=True,timeout=5,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def notification_current(conn,event,now=None):
    now=now or utc_now();row=conn.execute('SELECT value FROM state WHERE key=?',(KEY,)).fetchone()
    state=json.loads(row[0]) if row else {}
    expiry=stamp(event.get('entry_expires_at'))
    rows=bank_states(conn)
    return bool(expiry and expiry>now and state.get('incident_id')==event.get('login_incident_id')
        and state.get('entry_id')==event.get('entry_id') and state.get('phase')=='entry_ready'
        and any(s=='login_required' for _,_,s in rows))


def public_state(conn):
    row=conn.execute('SELECT value FROM state WHERE key=?',(KEY,)).fetchone()
    state=json.loads(row[0]) if row else {}
    return {k:state.get(k) for k in ('phase','checked_at','next_check_at','entry_url','entry_expires_at','last_error')}


def poll(store,settings,now=None,*,probe=entry_info,launcher=start_entry,closer=stop_entry):
    """Nonblocking state machine, called even when the market tick fails or is closed."""
    from .config import BJ
    from .jd_live_sync import request_refresh
    from .runtime_resources import slot
    now=now or utc_now();state=store.get_state(KEY,{})
    def save():store.set_state(KEY,state)
    rows=bank_states(store.conn)
    if not enabled(store.conn) or not rows:
        if state.get('phase')!='disabled':state.update(phase='disabled');save()
        return []
    incident_at=stamp(state.get('incident_at'))
    if incident_at and all(s=='fresh' and stamp(t) and stamp(t)>=incident_at for _,t,s in rows):
        if state.get('opened_by_watch'):
            try:closer()
            except (OSError,subprocess.SubprocessError):pass
        state={**state,'phase':'healthy','checked_at':now.isoformat(),'incident_id':None,'incident_at':None,
            'entry_url':None,'entry_expires_at':None,'entry_id':None,'last_error':None,'opened_by_watch':False}
        save();return []
    if state.get('phase')=='entry_ready' and stamp(state.get('entry_expires_at')) and now>=stamp(state['entry_expires_at']):
        state.update(phase='entry_expired',entry_url=None);save()
    due=not stamp(state.get('next_check_at')) or now>=stamp(state['next_check_at']) or state.get('phase')=='disabled'
    if due:
        state.update(phase='checking',checked_at=now.isoformat(),next_check_at=(now+timedelta(seconds=INTERVAL)).isoformat(),
                     probe_at=now.isoformat(),check_after=(now-timedelta(seconds=120)).isoformat(),last_error=None)
        if not all(stamp(t) and 0<=(now-stamp(t)).total_seconds()<120 for _,t,_ in rows):
            request=request_refresh(store.conn,components={'core'},source='login_watch')
            state['probe_at']=request['requested_at'];state['check_after']=request['requested_at'];save();return []
        save()
    phase=state.get('phase')
    if phase=='checking':
        requested=stamp(state.get('probe_at')) or now
        cutoff=stamp(state.get('check_after')) or requested
        recent=all(stamp(t) and cutoff<=stamp(t)<=now for _,t,_ in rows)
        if not recent:
            if (now-requested).total_seconds()>120:
                state.update(phase='unknown',last_error='check_unavailable',next_check_at=(now+timedelta(minutes=5)).isoformat());save()
            return []
        if any(s=='login_required' for _,_,s in rows):
            if not state.get('incident_id'):state.update(incident_id=uuid.uuid4().hex,incident_at=now.isoformat())
            state.update(phase='waiting_for_resources',retry_at=now.isoformat());save()
        elif all(s=='fresh' for _,_,s in rows):state.update(phase='healthy');save();return []
        else:
            state.update(phase='unknown',last_error='check_unavailable',next_check_at=(now+timedelta(minutes=5)).isoformat());save();return []
    if state.get('phase') in ('waiting_for_resources','opening'):
        if stamp(state.get('retry_at')) and now<stamp(state['retry_at']):return []
        info=probe(settings.db_path,now)
        if info:
            state.update(phase='entry_ready',entry_id=info['id'],entry_url=info['url'],entry_expires_at=info['expires_at'],last_error=None)
            save()
        elif state['phase']=='opening':
            if (now-(stamp(state.get('opening_at')) or now)).total_seconds()>90:
                state.update(phase='waiting_for_resources',retry_at=(now+timedelta(minutes=5)).isoformat(),last_error='entry_not_ready');save()
            return []
        else:
            with slot(settings.db_path,model=True) as free:
                if not free:
                    state.update(retry_at=(now+timedelta(seconds=60)).isoformat());save();return []
            try:launcher()
            except (OSError,subprocess.SubprocessError):
                state.update(retry_at=(now+timedelta(minutes=5)).isoformat(),last_error='entry_start_failed');save();return []
            state.update(phase='opening',opening_at=now.isoformat(),retry_at=now.isoformat(),opened_by_watch=True);save();return []
    if state.get('phase')!='entry_ready':return []
    expiry=stamp(state.get('entry_expires_at'))
    if not expiry or expiry<=now:return []
    key=f"jd-login:{state['incident_id']}:{state['entry_id']}"
    if store.conn.execute('SELECT 1 FROM alerts WHERE key=?',(key,)).fetchone():return []
    at=lambda v:stamp(v).astimezone(BJ).strftime('%m-%d %H:%M')
    return [{'key':key,'notification_topic':LOGIN_TOPIC,'login_incident_id':state['incident_id'],
        'entry_id':state['entry_id'],'entry_expires_at':state['entry_expires_at'],
        'title':'京东登录失效，请重新登录',
        'body':'本轮结论：京东真实账户登录已失效，旧账户数据不能用于当前操作建议。\n\n'
            f"需要操作：[打开京东登录入口]({state['entry_url']})\n用户名：jd；使用现有看板登录密码，无额外临时密码。\n\n"
            f"时间与有效性：检查 {at(state['checked_at'])}，入口有效至 {at(state['entry_expires_at'])}（北京时间）。\n\n"
            '登录后自动恢复只读同步；未登录时下一小时继续检查。此入口不代为买卖。'}]
