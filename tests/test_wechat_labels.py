"""Provider-facing labels change; private records, routing and links retain model identities."""
from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.config import Settings
from monitor.notification_control import set_subscription
from monitor.notification_outbox import dispatch
from monitor.notify import Notifier,wechat_text
from monitor.store import Store


class Text(HTMLParser):
    def __init__(self,value):
        super().__init__();self.text=[];self.links=[];self.feed(value)
    def handle_data(self,data):self.text.append(data)
    def handle_starttag(self,tag,attrs):
        if tag=='a':self.links.append(dict(attrs)['href'])


class WechatLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'notify.db');self.addCleanup(self.store.conn.close)
        for topic in ('codex:analysis','codex:live_zheshang'):set_subscription(self.store.conn,topic,True)
        self.settings=replace(Settings(),pushplus_token='unit-label-token',serverchan_sendkey='')
        self.response=Mock(ok=True,status_code=200);self.response.json.return_value={'code':200}
        self.url='https://gold.goalgoal.fun/#strategies/simulation/codex:high_empty'

    def event(self,real=False):
        return {'key':'unit-label-'+str(real),'notification_topic':'codex:live_zheshang' if real else 'codex:analysis',
                'title':'真实账户 · Codex · 浙商' if real else '📊Codex短期震荡',
                'body':'## CODEX\n\nCodex本轮判断。\n\n[查看codex账户]('+self.url+')'}

    def test_markdown_is_renamed_only_in_provider_payload_and_does_not_mutate_records(self):
        e=self.event()
        with patch('monitor.notify.requests.post',return_value=self.response) as request:
            dispatch(self.store,Notifier(self.settings),[e]);payload=request.call_args.kwargs['json']
            self.assertEqual(payload['title'],'📊AI短期震荡')
            self.assertIn('## AI',payload['content']);self.assertIn('AI本轮判断',payload['content'])
            self.assertIn('[查看AI账户]('+self.url+')',payload['content'])
            dispatch(self.store,Notifier(self.settings),[e]);self.assertEqual(request.call_count,1)
        self.assertEqual(e['title'],'📊Codex短期震荡')
        row=self.store.conn.execute('SELECT title,body FROM alerts').fetchone()
        self.assertEqual(tuple(row),(e['title'],e['body']))
        original=json.loads(self.store.conn.execute('SELECT event_json FROM notification_outbox').fetchone()[0])
        self.assertEqual(original,e)
        self.assertEqual(self.store.conn.execute('SELECT title FROM notification_deliveries').fetchone()[0],e['title'])

    def test_real_html_headings_and_link_labels_change_but_link_targets_are_unchanged(self):
        e=self.event(real=True)
        with patch('monitor.notify.requests.post',return_value=self.response) as request:
            dispatch(self.store,Notifier(self.settings),[e]);payload=request.call_args.kwargs['json']
        self.assertEqual(payload['template'],'html');self.assertEqual(payload['title'],'真实账户 · AI · 浙商')
        doc=Text(payload['content']);text=''.join(doc.text)
        self.assertNotIn('codex',text.lower());self.assertIn('真实账户 · AI · 浙商',text)
        self.assertEqual(doc.links,[self.url])

    def test_serverchan_fallback_keeps_display_alias_without_changing_markdown_links(self):
        settings=replace(self.settings,pushplus_token='',serverchan_sendkey='unit-label-sendkey')
        self.response.json.return_value={'code':0}
        with patch('monitor.notify.requests.post',return_value=self.response) as request:
            dispatch(self.store,Notifier(settings),[self.event(real=True)])
        data=request.call_args.kwargs['data']
        self.assertIn(' · AI · ',data['title']);self.assertIn('AI本轮判断',data['desp'])
        self.assertIn(self.url,data['desp']);self.assertNotIn('Codex',data['desp'])

    def test_plain_urls_attributes_and_other_models_are_not_rewritten(self):
        self.assertEqual(wechat_text('Codex '+self.url+' Claude'),'AI '+self.url+' Claude')
        html='<a class="codex" href="'+self.url+'">Codex</a>'
        self.assertEqual(wechat_text(html,html=True),html.replace('>Codex<','>AI<'))
