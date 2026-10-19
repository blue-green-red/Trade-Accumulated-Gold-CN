"""One-shot manual requests: authenticated queue, budget, generation and serial consumption."""
import http.client
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock
import test_autonomous as fixtures
from monitor.manual_ai import request,status,ready,claim,finish,recover,RequestError,TRIGGER
from monitor.controls import set_enabled
from monitor.web import Handler,ThreadingHTTPServer


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AutonomousTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()

    def test_deduplicates_across_connections_and_consumes_once(self):
        f=self.f
        a=request(f.store.conn,f.settings,'codex')
        import sqlite3
        with sqlite3.connect(f.settings.db_path) as other:b=request(other,f.settings,'codex')
        self.assertEqual(a['id'],b['id'])
        item=claim(f.store.conn,'codex','one-round');self.assertIsNone(claim(f.store.conn,'codex','two-round'))
        self.assertEqual(request(f.store.conn,f.settings,'codex')['id'],a['id'])
        finish(f.store.conn,'codex',item,{'_view_id':1})
        self.assertEqual(status(f.store.conn,'codex')['status'],'completed')
        self.assertNotEqual(request(f.store.conn,f.settings,'codex')['id'],a['id'])

    def test_disabled_unavailable_and_quota_are_rejected(self):
        f=self.f
        set_enabled(f.store.conn,'codex',False)
        with self.assertRaises(RequestError):request(f.store.conn,f.settings,'codex')
        set_enabled(f.store.conn,'codex',True);f.store.set_state('ai_status:codex',{'available':False})
        with self.assertRaises(RequestError):request(f.store.conn,f.settings,'codex')
        f.store.set_state('ai_status:codex',{'available':True})
        f.settings=replace(f.settings,ai_max_runs_per_day=1,ai_urgent_reserve=0)
        f.publish()
        with self.assertRaises(RequestError) as error:request(f.store.conn,f.settings,'codex')
        self.assertEqual(error.exception.status,429)

    def test_switch_generation_cancels_queued_and_running_requests(self):
        f=self.f
        request(f.store.conn,f.settings,'codex');set_enabled(f.store.conn,'codex',False);set_enabled(f.store.conn,'codex',True)
        self.assertEqual(status(f.store.conn,'codex')['status'],'cancelled')
        self.assertFalse(ready(f.store.conn,f.settings,'codex'))
        request(f.store.conn,f.settings,'codex');item=claim(f.store.conn,'codex','r')
        set_enabled(f.store.conn,'codex',False);finish(f.store.conn,'codex',item,{'_view_id':1})
        self.assertEqual(status(f.store.conn,'codex')['status'],'cancelled')

    def test_failed_attempt_is_not_replayed_and_restart_recovers_running(self):
        f=self.f
        request(f.store.conn,f.settings,'codex');item=claim(f.store.conn,'codex','r')
        finish(f.store.conn,'codex',item,None)
        self.assertFalse(ready(f.store.conn,f.settings,'codex'))
        self.assertEqual(status(f.store.conn,'codex')['status'],'failed')
        request(f.store.conn,f.settings,'codex');recover(f.store.conn,'codex')
        self.assertEqual(status(f.store.conn,'codex')['status'],'queued')
        claim(f.store.conn,'codex','r2');recover(f.store.conn,'codex')
        self.assertEqual(status(f.store.conn,'codex')['status'],'failed')

    def test_scheduler_queues_while_busy_then_runs_only_requested_model(self):
        f=self.f
        request(f.store.conn,f.settings,'codex')
        f.m.ai_running=Mock(return_value=True)
        f.m.run_ai_if_due(fixtures.snapshot())
        self.assertEqual(status(f.store.conn,'codex')['status'],'queued')
        f.m.ai_running=Mock(return_value=False);f.a.due=Mock(return_value=None)
        missing_quote=fixtures.snapshot();missing_quote.pop('execution_quote')
        f.m.run_ai_if_due(missing_quote)
        self.assertEqual(status(f.store.conn,'codex')['status'],'queued')
        self.assertIsNone(f.m._ai_thread)
        f.m.ai_context=Mock(return_value={'snapshot_at':fixtures.utc_now().isoformat(),'xauusd':4000})
        f.a._run_codex=Mock(return_value=('notes',[],fixtures.response()))
        f.m.run_ai_if_due(fixtures.snapshot());f.m.wait_for_ai()
        self.assertEqual(f.a._run_codex.call_count,1)
        self.assertEqual(status(f.store.conn,'codex')['status'],'completed')
        self.assertFalse(ready(f.store.conn,f.settings,'codex'))
        row=f.store.conn.execute("SELECT trigger FROM ai_views WHERE analyst='codex' ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(row[0],TRIGGER)

    def test_http_auth_origin_validation_and_deduplication(self):
        f=self.f
        class LocalHandler(Handler):
            password='test-secret'
            def log_message(self,*args):pass
        LocalHandler.settings=f.settings
        server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        import base64
        auth='Basic '+base64.b64encode(b'user:test-secret').decode()
        def post(body='{}',**headers):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST','/api/models/codex/analyze',body,headers)
            r=conn.getresponse();data=r.read();code=r.status;conn.close();return code,data
        try:
            self.assertEqual(post()[0],401)
            self.assertEqual(post(Authorization=auth)[0],403)
            good={'Authorization':auth,'Content-Type':'application/json','X-Gold-Control':'1'}
            self.assertEqual(post(Origin='https://evil.example',**good)[0],403)
            self.assertEqual(post('{"enabled":true}',**good)[0],400)
            a,b=post(**good),post(**good)
            self.assertEqual(a[0],202);self.assertEqual(json.loads(a[1])['id'],json.loads(b[1])['id'])
        finally:server.shutdown();server.server_close()
