from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.config import Settings
from monitor.jd_live_readonly import ensure_schema,record_failure
from monitor.jd_login_watch import KEY,poll,notification_current
from monitor.notification_control import set_subscription,LOGIN_TOPIC
from monitor.notification_outbox import dispatch
from monitor.runtime_resources import slot,interactive_slot
from monitor.store import Store

NOW=datetime.now(timezone.utc)


class LoginWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=replace(Settings(),db_path=Path(self.tmp.name)/'test.db',pushplus_token='unit-login-token',serverchan_sendkey='')
        self.s=Store(self.settings.db_path);self.addCleanup(self.s.conn.close)
        ensure_schema(self.s.conn);self.s.set_state('jd_live_banks',{'banks':['zheshang']})
        set_subscription(self.s.conn,LOGIN_TOPIC,True)
        self.launch=Mock();self.close=Mock();self.probe=Mock(return_value=None)

    def mark(self,status='login_required',at=NOW):record_failure(self.s.conn,'zheshang',status,at.isoformat())
    def check(self,at=NOW):return poll(self.s,self.settings,at,probe=self.probe,launcher=self.launch,closer=self.close)
    def portal(self,at=NOW,identity='entry-one'):
        self.probe.return_value={'id':identity,'url':'https://8.218.117.20:1455/','expires_at':(at+timedelta(minutes=15)).isoformat()}

    def test_fresh_login_checked_hourly_without_opening_or_model_dependency(self):
        self.mark('fresh');self.assertEqual(self.check(),[])
        self.assertEqual(self.s.get_state(KEY)['phase'],'healthy')
        self.check(NOW+timedelta(minutes=50));self.launch.assert_not_called()
        self.assertEqual(self.s.get_state(KEY)['checked_at'],NOW.isoformat())

    def test_expired_login_opens_then_notifies_ready_url_once(self):
        self.mark();self.assertEqual(self.check(),[]);self.launch.assert_called_once()
        self.assertEqual(self.s.get_state(KEY)['phase'],'opening')
        self.portal();events=self.check(NOW+timedelta(seconds=30));self.assertEqual(len(events),1)
        self.assertIn('入口有效至',events[0]['body']);self.assertIn('现有看板登录密码',events[0]['body'])
        self.assertIn('https://8.218.117.20:1455/',events[0]['body']);self.assertNotIn(self.settings.pushplus_token or 'not-a-secret',events[0]['body'])
        notifier=Mock();notifier.send.return_value=True
        with patch('monitor.jd_login_watch.utc_now',return_value=NOW+timedelta(seconds=30)):
            dispatch(self.s,notifier,events,now=NOW+timedelta(seconds=30))
        self.assertEqual(notifier.send.call_count,1)
        self.assertEqual(self.s.conn.execute('SELECT category FROM notification_deliveries').fetchone()[0],'account_login')
        self.assertEqual(self.check(NOW+timedelta(minutes=1)),[])
        self.launch.assert_called_once()
        self.check(NOW+timedelta(minutes=16))
        self.assertEqual(self.s.get_state(KEY)['phase'],'entry_expired')
        self.launch.assert_called_once()

    def test_waits_for_model_then_reuses_existing_manual_viewer(self):
        self.mark()
        with slot(self.settings.db_path,model=True):self.check()
        self.launch.assert_not_called();self.assertEqual(self.s.get_state(KEY)['phase'],'waiting_for_resources')
        self.portal(NOW+timedelta(minutes=1))
        self.assertEqual(len(self.check(NOW+timedelta(minutes=1))),1)
        self.launch.assert_not_called()
        self.mark('fresh',NOW+timedelta(minutes=2));self.check(NOW+timedelta(minutes=2))
        self.close.assert_not_called()

    def test_probe_requests_current_readonly_state_and_network_errors_do_not_mean_logout(self):
        self.mark(at=NOW-timedelta(minutes=20));self.check()
        self.assertEqual(self.s.get_state(KEY)['phase'],'checking')
        self.assertEqual(self.s.get_state('jd_live_refresh')['components'],['core'])
        self.check(NOW+timedelta(seconds=30));self.launch.assert_not_called()
        self.mark('unavailable',NOW+timedelta(seconds=40));self.check(NOW+timedelta(seconds=40))
        self.assertEqual(self.s.get_state(KEY)['phase'],'unknown');self.launch.assert_not_called()

    def test_recovery_closes_only_owned_auto_viewer_and_cancels_stale_notice(self):
        self.mark();self.check();self.portal();events=self.check(NOW+timedelta(seconds=10))
        self.mark('fresh',NOW+timedelta(seconds=20));self.check(NOW+timedelta(seconds=20))
        self.close.assert_called_once();self.assertEqual(self.s.get_state(KEY)['phase'],'healthy')
        self.assertFalse(notification_current(self.s.conn,events[0],NOW+timedelta(seconds=20)))
        self.assertIsNone(self.s.get_state(KEY)['incident_id'])

    def test_not_logged_in_next_hour_rechecks_reopens_and_new_ready_entry_can_remind(self):
        self.mark();self.check();self.portal();first=self.check(NOW+timedelta(seconds=10))[0]
        self.probe.return_value=None;later=NOW+timedelta(hours=1)
        self.mark(at=later);self.check(later)
        self.assertEqual(self.launch.call_count,2)
        self.portal(later,'entry-two');second=self.check(later+timedelta(seconds=10))[0]
        self.assertNotEqual(first['key'],second['key'])
        self.assertEqual(first['login_incident_id'],second['login_incident_id'])

    def test_disabled_or_no_bank_stops_checks_and_failures_do_not_publish_bad_links(self):
        self.mark();set_subscription(self.s.conn,LOGIN_TOPIC,False);self.check();self.launch.assert_not_called()
        set_subscription(self.s.conn,LOGIN_TOPIC,True);self.launch.side_effect=OSError('service unavailable')
        self.assertEqual(self.check(),[]);self.assertEqual(self.s.get_state(KEY)['last_error'],'entry_start_failed')
        self.s.set_state('jd_live_banks',{'banks':[]});self.check(NOW+timedelta(hours=1))
        self.assertEqual(self.launch.call_count,1)

    def test_start_stop_only_touch_the_bounded_auto_service(self):
        from monitor.jd_login_watch import start_entry,stop_entry
        with patch('monitor.jd_login_watch.subprocess.run') as command:
            start_entry();stop_entry()
        self.assertEqual([c.args[0] for c in command.call_args_list],[
            ['systemctl','start','--no-block','gold-jd-login-auto'],['systemctl','stop','--no-block','gold-jd-login-auto']])
        text=(Path(__file__).resolve().parents[1]/'deploy/gold-jd-login-auto.service').read_text()
        self.assertIn('RuntimeMaxSec=900',text);self.assertIn('MemoryMax=650M',text)
        self.assertIn('--dashboard-auth',text);self.assertNotIn('WantedBy=',text)

    def test_market_failure_does_not_skip_login_maintenance(self):
        from monitor.__main__ import Monitor
        m=object.__new__(Monitor);m.settings=self.settings;m.store=self.s;m.analysts={}
        m.ai_running=Mock(return_value=False);m.notifier=Mock();m.dispatch=Mock();m.tick=Mock(side_effect=RuntimeError('market'))
        with patch('monitor.jd_login_watch.poll',return_value=['login event']),patch('monitor.__main__.time.sleep',side_effect=StopIteration),patch('monitor.__main__.log.exception'):
            with self.assertRaises(StopIteration):m.run_forever()
        m.dispatch.assert_called_once_with(['login event']);m.tick.assert_called_once()

    def test_settings_page_includes_live_maintenance_status_without_internal_fields(self):
        from monitor.mobile_data import scoped_data
        self.mark('fresh');self.check();state=self.s.get_state(KEY)
        self.s.set_state(KEY,{**state,'incident_id':'internal-only','cookie':'never-export'})
        result={}
        self.assertTrue(scoped_data(self.s.conn,self.settings,result,'notifications','settings',{},None,None,None))
        value=result['notifications'];self.assertEqual(value['login_watch']['phase'],'healthy')
        self.assertTrue(next(s for s in value['subscriptions'] if s['key']==LOGIN_TOPIC)['enabled'])
        self.assertNotIn('cookie',value['login_watch']);self.assertNotIn('incident_id',value['login_watch'])


class InteractiveResourceTests(unittest.TestCase):
    def test_cookie_recovery_cannot_launch_a_second_browser_during_viewer_startup(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'test.db'
            with interactive_slot(db,hold_startup=True) as ready:
                self.assertTrue(callable(ready))
                with slot(db) as cookies:self.assertFalse(cookies)
                ready();ready()
                with slot(db) as cookies:self.assertTrue(cookies)
                with slot(db,model=True) as model:self.assertFalse(model)
            with slot(db,model=True) as model:self.assertTrue(model)

    def test_viewer_blocks_models_but_allows_lightweight_cookie_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'test.db'
            with interactive_slot(db) as viewer:
                self.assertTrue(viewer)
                with slot(db,model=True) as model:self.assertFalse(model)
                with slot(db) as cookies:self.assertTrue(cookies)
                with interactive_slot(db) as second:self.assertFalse(second)
            with slot(db,model=True) as model:self.assertTrue(model)
            with slot(db,model=True):
                with interactive_slot(db) as viewer:self.assertFalse(viewer)


class InterruptedRuntimeTests(unittest.TestCase):
    def test_dead_worker_flags_do_not_block_sync_or_consume_queued_manual_request(self):
        from monitor.ai import AIAnalyst
        from monitor.ai_runtime import recover
        with tempfile.TemporaryDirectory() as d:
            s=replace(Settings(),db_path=Path(d)/'test.db');store=Store(s.db_path)
            try:
                AIAnalyst(s,store,'codex','codex')
                store.set_state('ai_runtime:codex',{'running':True,'queued':False})
                store.set_state('ai_manual:codex',{'id':'pending','status':'queued','generation':0})
                with store.conn:
                    for status,run_id in (('running','dead'),('diagnostic_running','external')):
                        store.conn.execute('INSERT INTO ai_runs(analyst,started_at,trigger,run_id,status) VALUES(?,?,?,?,?)',('codex',NOW.isoformat(),'test',run_id,status))
                recover(store,['codex'],NOW)
                self.assertFalse(store.get_state('ai_runtime:codex')['running'])
                self.assertEqual(store.get_state('ai_manual:codex')['status'],'queued')
                self.assertEqual([r[0] for r in store.conn.execute('SELECT status FROM ai_runs ORDER BY id')],['interrupted','diagnostic_running'])
                self.assertEqual(store.conn.execute('SELECT count(*) FROM ai_views').fetchone()[0],0)
            finally:store.conn.close()

    def test_daemon_initialization_reconciles_before_resetting_runtime_flags(self):
        from monitor.ai import AIAnalyst
        from monitor.__main__ import Monitor
        with tempfile.TemporaryDirectory() as d:
            s=replace(Settings(),db_path=Path(d)/'test.db',codex_cli='missing',claude_cli='missing',
                clawx_api_key='',anthropic_api_key='',ai_analysts=('codex',))
            store=Store(s.db_path);AIAnalyst(s,store,'codex','codex')
            from monitor.risk_profiles import reset_experiment
            reset_experiment(store.conn)
            store.set_state('ai_runtime:codex',{'running':True})
            with store.conn:store.conn.execute('INSERT INTO ai_runs(analyst,started_at,trigger,run_id,status) VALUES(?,?,?,?,?)',('codex',NOW.isoformat(),'test','dead','running'))
            try:
                monitor=Monitor(s,True)
                try:self.assertEqual(monitor.store.conn.execute('SELECT status FROM ai_runs').fetchone()[0],'interrupted')
                finally:monitor.store.conn.close()
            finally:store.conn.close()


if __name__=='__main__':unittest.main()
