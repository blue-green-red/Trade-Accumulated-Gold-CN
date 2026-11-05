"""A retired real bank cannot poll, prompt, publish or keep an enabled bank logged out."""
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from monitor.config import Settings
from monitor.store import Store
from monitor.ai_events import enqueue,read
from monitor.jd_live_readonly import (ensure_schema,enabled_banks,set_enabled_banks,
    parse_account,record_success,record_failure,accounts,model_input)
from monitor.jd_live_sync import sync_once
from monitor.jd_live_advice import save,notification_events
from monitor.jd_live_preflight import missing_inputs
from monitor.notification_control import subscriptions,set_subscription,selected_events
from monitor.mobile_data import scoped_data
from test_jd_live import payload,advice


class LiveBankControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db')
        self.c=self.store.conn;ensure_schema(self.c);self.now=datetime.now(timezone.utc)
        for bank in ('zheshang','minsheng'):
            record_success(self.c,bank,parse_account(bank,payload(bank)),self.now.isoformat())

    def tearDown(self):self.c.close();self.tmp.cleanup()

    def test_scope_hides_retired_bank_from_inputs_and_pages_but_keeps_its_ledger(self):
        before=self.c.execute("SELECT data_json FROM jd_live_snapshots WHERE bank='minsheng'").fetchone()[0]
        set_enabled_banks(self.c,['zheshang'])
        self.assertEqual(enabled_banks(self.c),('zheshang',))
        self.assertEqual([a['bank'] for a in accounts(self.c)],['zheshang'])
        self.assertEqual(accounts(self.c,selected_bank='minsheng'),[])
        self.assertEqual(set(model_input(self.c)['accounts']),{'zheshang'})
        self.assertNotIn('minsheng',missing_inputs(self.c) or {})
        data={}
        self.assertTrue(scoped_data(self.c,Settings(),data,'strategies','live/minsheng/holdings',{},None,None,None))
        self.assertIsNone(data['live_account'])
        self.assertEqual(self.c.execute("SELECT data_json FROM jd_live_snapshots WHERE bank='minsheng'").fetchone()[0],before)

    def test_retired_login_failure_cannot_invalidate_active_session_or_poll_inactive_bank(self):
        set_enabled_banks(self.c,['zheshang'])
        record_failure(self.c,'minsheng','login_required')
        client=Mock(session=object())
        def query(operation,bank):
            self.assertEqual(bank,'zheshang')
            if operation=='indexDataStd':return payload(bank)
            raise TimeoutError('fixture')
        client.query.side_effect=query
        self.assertEqual(sync_once(self.store,client,{'core'}),'completed')
        client.close.assert_not_called();client.recover.assert_not_called()
        self.assertTrue(accounts(self.c)[0]['connected'])
        set_enabled_banks(self.c,[])
        client.reset_mock()
        self.assertEqual(sync_once(self.store,client),'completed')
        client.query.assert_not_called();client.recover.assert_not_called()

    def test_inflight_advice_does_not_restore_retired_bank(self):
        source=model_input(self.c,self.now)
        save(self.c,'codex',1,{'minsheng':advice()},source,self.now);self.c.commit()
        set_enabled_banks(self.c,['zheshang'])
        save(self.c,'codex',2,{'minsheng':advice(),'zheshang':advice()},source,self.now);self.c.commit()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM jd_live_advice WHERE bank='minsheng'").fetchone()[0],1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM jd_live_advice WHERE bank='zheshang'").fetchone()[0],1)
        self.assertEqual(notification_events(self.c,'codex',1),[])

    def test_retirement_cancels_only_retired_bank_events_subscriptions_and_unsent_notifications(self):
        for topic in ('codex:analysis','codex:live_zheshang','codex:live_minsheng'):set_subscription(self.c,topic,True)
        enqueue(self.c,'codex','old bank',0,'retire',{'bank':'minsheng'})
        enqueue(self.c,'codex','active bank',0,'keep',{'bank':'zheshang'})
        enqueue(self.c,'codex','market',0,'market')
        stamp=self.now.isoformat();expiry=(self.now+timedelta(minutes=10)).isoformat()
        with self.c:
            for bank in ('zheshang','minsheng'):
                event={'key':bank,'notification_topic':'codex:live_'+bank,'title':'fixture','body':'fixture'}
                self.c.execute('INSERT INTO notification_outbox(event_key,created_at,expires_at,event_json,status,next_attempt_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                               (bank,stamp,expiry,json.dumps(event),'retry',stamp,stamp))
        set_enabled_banks(self.c,['zheshang'])
        self.assertEqual({e['id'] for e in read(self.c,'codex')['pending']},{'keep','market'})
        self.assertNotIn('codex:live_minsheng',{s['key'] for s in subscriptions(self.c)})
        self.assertIn('codex:analysis',{s['key'] for s in subscriptions(self.c) if s['enabled']})
        states=dict(self.c.execute('SELECT event_key,status FROM notification_outbox'))
        self.assertEqual(states,{'zheshang':'retry','minsheng':'cancelled'})
        with self.assertRaises(ValueError):set_subscription(self.c,'codex:live_minsheng',True)
        self.assertEqual(selected_events(self.c,[{'notification_topic':'codex:live_minsheng'}]),[])
        set_enabled_banks(self.c,['zheshang'])
        self.assertEqual(states,dict(self.c.execute('SELECT event_key,status FROM notification_outbox')))

    def test_invalid_scope_is_rejected_and_corrupt_saved_scope_fails_closed(self):
        for bad in (['unknown'],[{}],None,'zheshang'):
            with self.assertRaises(ValueError):set_enabled_banks(self.c,bad)
        self.store.set_state('jd_live_banks',{'banks':[{}]})
        self.assertEqual(enabled_banks(self.c),())


class LiveBankControlBrowserTests(unittest.TestCase):
    def test_single_bank_pages_and_retired_bookmark_are_consistent(self):
        from playwright.sync_api import sync_playwright,expect
        from test_dashboard_browser import local_dashboard
        with local_dashboard() as (f,url),sync_playwright() as p:
            c=f.m.store.conn;ensure_schema(c)
            for bank in ('zheshang','minsheng'):record_success(c,bank,parse_account(bank,payload(bank)))
            set_enabled_banks(c,['zheshang'])
            browser=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':390,'height':844});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(url+'/#strategies/live')
                page.get_by_role('heading',name='浙商积存金',exact=True).wait_for()
                self.assertEqual(page.get_by_role('heading',name='民生积存金',exact=True).count(),0)
                page.goto(url+'/#notifications/settings')
                page.get_by_role('heading',name='微信操作通知',exact=True).wait_for()
                self.assertNotIn('民生',page.locator('#panel-notifications').inner_text())
                page.goto(url+'/#strategies/live/minsheng/orders')
                expect(page.locator('#panel-strategies')).to_contain_text('此真实账户已停用或暂不可用')
                for width in (390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(errors,[])
            finally:browser.close()
