import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from monitor.store import Store
from monitor.notification_control import subscriptions, set_subscription, selected_events
from monitor.__main__ import Monitor


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db')

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_default_off_persistence_validation_and_independence(self):
        c=self.store.conn
        self.assertTrue(all(not s['enabled'] for s in subscriptions(c)))
        set_subscription(c,'codex:short',True)
        self.assertEqual([s['key'] for s in subscriptions(c) if s['enabled']],['codex:short'])
        with self.assertRaises(ValueError):set_subscription(c,'codex:short','true')
        with self.assertRaises(ValueError):set_subscription(c,'unknown',True)
        self.assertIsNone(c.execute("SELECT value FROM state WHERE key='model_control:codex'").fetchone())
        set_subscription(c,'codex:medium',True)
        set_subscription(c,'codex:short',False)
        self.assertEqual([s['key'] for s in subscriptions(c) if s['enabled']],['codex:medium'])

    def test_dispatch_only_selected_but_archives_all_without_leaking_other_events(self):
        set_subscription(self.store.conn,'codex:short',True)
        events=[{'key':str(i),'notification_topic':topic,'title':topic,'body':topic}
                for i,topic in enumerate(('codex:short','codex:long','claude:short','system'))]
        m=object.__new__(Monitor);m.store=self.store;m.notifier=Mock();m.notifier.send.return_value=True
        m.dispatch(events)
        m.notifier.send.assert_called_once()
        self.assertNotIn('codex:long',m.notifier.send.call_args.args[1])
        rows=self.store.conn.execute('SELECT title,delivered FROM alerts').fetchall()
        self.assertEqual(len(rows),4)
        self.assertEqual({r['title'] for r in rows if r['delivered']},{'codex:short'})
        m.notifier.reset_mock();m.dispatch(events[1:]);m.notifier.send.assert_not_called()

    def test_model_periods_and_rule_routing(self):
        set_subscription(self.store.conn,'rules:short',True)
        set_subscription(self.store.conn,'codex:short',True)
        events=[{'key':'paper:short:open:1'},{'key':'paper:codex:short_ai:open:1'},
                {'key':'acct:codex:position_ai:2026-10-22:buy'},{'key':'move:up'}]
        self.assertEqual(selected_events(self.store.conn,events),events[:2])

    def test_analysis_summary_independent_of_system_alerts_and_model_trade_topics(self):
        set_subscription(self.store.conn,'codex:analysis',True)
        events=[{'key':'ai:codex:run','notification_topic':'codex:analysis','title':'Codex摘要','body':'codex'},
                {'key':'ai:claude:run','notification_topic':'claude:analysis','title':'Claude摘要','body':'claude'},
                {'key':'touch:R1','title':'点位','body':'level'},{'key':'gate:allow','title':'门控','body':'gate'},
                {'key':'autonomous:1','notification_topic':'codex:high','title':'买入','body':'fill'}]
        self.assertEqual(selected_events(self.store.conn,events),events[:1])
        m=object.__new__(Monitor);m.store=self.store;m.notifier=Mock();m.notifier.send.return_value=True
        m.dispatch(events)
        self.assertEqual(m.notifier.send.call_args.args,('Codex摘要','### Codex摘要\n\ncodex'))
        self.assertEqual(self.store.conn.execute('select count(*) from alerts').fetchone()[0],5)
        self.assertEqual(self.store.conn.execute('select count(*) from alerts where delivered=1').fetchone()[0],1)

    def test_http_requires_login_origin_and_valid_topic(self):
        import base64, http.client, threading
        from dataclasses import replace
        from monitor.config import Settings
        from monitor.web import Handler, ThreadingHTTPServer
        class Local(Handler):
            password='test-notify'
            def log_message(self,*args):pass
        Local.settings=replace(Settings(),db_path=Path(self.tmp.name)/'test.db')
        server=ThreadingHTTPServer(('127.0.0.1',0),Local)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        good={'Authorization':'Basic '+base64.b64encode(b'user:test-notify').decode(),
              'Content-Type':'application/json','X-Gold-Control':'1'}
        def post(body='{"topic":"codex:short","enabled":true}',headers=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port)
            c.request('POST','/api/notifications',body,headers or {})
            r=c.getresponse();r.read();status=r.status;c.close();return status
        try:
            self.assertEqual(post(),401)
            self.assertEqual(post(headers={'Authorization':good['Authorization']}),403)
            self.assertEqual(post(headers={**good,'Origin':'https://evil.example'}),403)
            self.assertEqual(post('{"topic":"bad","enabled":true}',good),400)
            self.assertEqual(post('{"topic":"codex:short","enabled":"true"}',good),400)
            self.assertEqual(post(headers=good),200)
            self.assertEqual([r['key'] for r in subscriptions(self.store.conn) if r['enabled']],['codex:short'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_real_advice_is_a_separate_prioritized_message(self):
        for topic in ('codex:live_zheshang','codex:analysis','codex:high'):set_subscription(self.store.conn,topic,True)
        events=[{'key':'autonomous:fake','notification_topic':'codex:high','title':'模拟买入','body':'simulation'},
                {'key':'ai:fake','notification_topic':'codex:analysis','title':'Codex摘要','body':'research'},
                {'key':'live-advice:fake','notification_topic':'codex:live_zheshang','title':'真实账户·浙商卖出建议','body':'需手动确认'}]
        m=object.__new__(Monitor);m.store=self.store;m.notifier=Mock()
        m.notifier.send.side_effect=[False,True]
        m.dispatch(events)
        self.assertEqual(m.notifier.send.call_count,2)
        calls=m.notifier.send.call_args_list
        self.assertEqual(calls[0].args[0],'真实账户·浙商卖出建议')
        self.assertNotIn('simulation',calls[0].args[1]);self.assertNotIn('需手动确认',calls[1].args[1])
        receipts=self.store.conn.execute('SELECT category,status FROM notification_deliveries ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in receipts],[('real_advice','failed'),('other','accepted')])
        recorded={r[0]:r[1] for r in self.store.conn.execute('SELECT key,delivered FROM alerts')}
        self.assertEqual(recorded,{'autonomous:fake':1,'ai:fake':1,'live-advice:fake':0})

    def test_both_banks_merge_without_disabled_simulation(self):
        for bank in ('zheshang','minsheng'):set_subscription(self.store.conn,f'codex:live_{bank}',True)
        events=[{'key':f'live-advice:{bank}','notification_topic':f'codex:live_{bank}','title':bank,'body':bank}
                for bank in ('zheshang','minsheng')]
        events.append({'key':'sim:disabled','notification_topic':'codex:high','title':'模拟买入','body':'disabled'})
        m=object.__new__(Monitor);m.store=self.store;m.notifier=Mock();m.notifier.send.return_value=True
        m.dispatch(events)
        m.notifier.send.assert_called_once()
        title,body=m.notifier.send.call_args.args
        self.assertEqual(title,'💡真实账户建议 · 2条')
        self.assertIn('zheshang',body);self.assertIn('minsheng',body);self.assertNotIn('disabled',body)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0],1)

class ProviderReceiptTests(unittest.TestCase):
    def test_acceptance_keeps_message_id_without_claiming_phone_delivery(self):
        from dataclasses import replace
        from monitor.config import Settings
        from monitor.notify import Notifier
        from unittest.mock import patch
        notifier=Notifier(replace(Settings(),pushplus_token='unit-test-token',serverchan_sendkey=''))
        response=Mock(ok=True,status_code=200);response.json.return_value={'code':200,'data':'receipt-unit-1'}
        with patch('monitor.notify.requests.post',return_value=response) as request:
            self.assertTrue(notifier.send('真实建议','人工执行'))
            self.assertEqual(request.call_args.kwargs['json']['channel'],'wechat')
        self.assertEqual(notifier.last_receipt,{'channel':'pushplus','status':'accepted','provider_id':'receipt-unit-1','provider_code':'200'})

    def test_failure_logs_do_not_expose_secret_urls(self):
        from dataclasses import replace
        from monitor.config import Settings
        from monitor.notify import Notifier
        from unittest.mock import patch
        import requests
        notifier=Notifier(replace(Settings(),pushplus_token='',serverchan_sendkey='unit-private-sendkey'))
        with patch('monitor.notify.requests.post',side_effect=requests.ConnectionError('https://sctapi.ftqq.com/unit-private-sendkey.send')):
            with self.assertLogs('monitor.notify',level='WARNING') as logs:self.assertFalse(notifier.send('test','test'))
        self.assertNotIn('unit-private-sendkey',''.join(logs.output))
        self.assertEqual(notifier.last_receipt['status'],'failed')
