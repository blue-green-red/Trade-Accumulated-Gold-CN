from dataclasses import replace
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.config import Settings
from monitor.notification_control import set_subscription
from monitor.notification_html import real_advice
from monitor.notification_outbox import dispatch
from monitor.notify import Notifier
from monitor.store import Store


class Document(HTMLParser):
    def __init__(self,content):
        super().__init__();self.tags=[];self.headings=[];self.text=[];self.heading=None
        self.feed(content)
    def handle_starttag(self,tag,attrs):
        self.tags.append((tag,dict(attrs)))
        if tag in ('h1','h2'):self.heading=[tag,'']
    def handle_endtag(self,tag):
        if self.heading and tag==self.heading[0]:self.headings.append(tuple(self.heading));self.heading=None
    def handle_data(self,data):
        self.text.append(data)
        if self.heading:self.heading[1]+=data


def event(bank='zheshang'):
    return {'key':'live-html:'+bank,'notification_topic':'codex:live_'+bank,
            'title':'浙商真实账户 · 撤单建议' if bank=='zheshang' else '民生真实账户 · 当日首次评估',
            'body':'**本轮结论：继续持有，本轮不立即买卖。**\n\n'
                   '需要操作：请手动确认1笔撤单建议。\n\n'
                   '建议撤单 · 原单卖出5克 · ≥ 892元/克 · 截止11-17 23:59\n→ 修改后参数待核对\n\n'
                   '价格复评：银行报价 ≤ 840元/克。\n到价只触发重新分析，不执行交易。\n\n'
                   '有效至11-10 10:34 · 建议 #1\n时间均为北京时间\n\n'
                   '[查看完整分析](https://gold.goalgoal.fun/#strategies/live/'+bank+'/advice)'}


class HtmlDetailsTests(unittest.TestCase):
    def test_title_is_one_h1_before_body_and_sections_have_headings(self):
        e=event();content=real_advice(e['title'],[e]);doc=Document(content)
        self.assertEqual(doc.headings[0],('h1',e['title']))
        self.assertEqual(content.count(e['title']),1)
        self.assertLess(content.index('</h1>'),content.index('<main>'))
        for heading in ('本轮结论','需要操作','挂单处理','价格复评','有效期与数据时间'):
            self.assertIn(('h2',heading),doc.headings)
        text=''.join(doc.text)
        for required in ('5克','≥ 892元/克','11-17 23:59','≤ 840元/克','不执行交易','11-10 10:34'):
            self.assertIn(required,text)
        self.assertNotIn('**',text);self.assertNotIn('###',text)
        links=[attrs for tag,attrs in doc.tags if tag=='a']
        self.assertEqual(links[0]['href'],'https://gold.goalgoal.fun/#strategies/live/zheshang/advice')

    def test_merged_message_keeps_each_bank_heading_and_content(self):
        es=[event(),event('minsheng')];doc=Document(real_advice('两条真实账户建议',es))
        self.assertEqual([h for h in doc.headings if h[0]=='h1'],[('h1','两条真实账户建议')])
        for e in es:self.assertEqual(doc.headings.count(('h2',e['title'])),1)

    def test_model_html_and_attribute_characters_are_only_displayed_as_text(self):
        e=event();e['title']='<img src=x onerror=alert(1)>'
        e['body']='风险：<script>alert(1)</script> & "quoted"\n\n[危险](javascript:alert(1))\n\n[详情](https://example.com/?q="onclick="bad)'
        doc=Document(real_advice(e['title'],[e]))
        self.assertFalse(any(tag in ('img','script','iframe','style') for tag,_ in doc.tags))
        for tag,attrs in doc.tags:
            self.assertFalse(any(key.startswith('on') for key in attrs))
            if tag=='a':self.assertTrue(attrs['href'].startswith('https://'))
        self.assertIn('<script>alert(1)</script>',''.join(doc.text))


class HtmlDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'test.db')
        set_subscription(self.s.conn,'codex:live_zheshang',True)
        set_subscription(self.s.conn,'codex:analysis',True)
        self.settings=replace(Settings(),pushplus_token='unit-html-token',serverchan_sendkey='')
        self.response=Mock(ok=True,status_code=200)
        self.response.json.return_value={'code':200,'data':'unit-html-receipt'}
    def tearDown(self):self.s.conn.close();self.tmp.cleanup()

    def test_real_message_uses_html_and_summary_keeps_markdown_without_test_send(self):
        e=event();n=Notifier(self.settings)
        with patch('monitor.notify.requests.post',return_value=self.response) as request:
            dispatch(self.s,n,[e]);payload=request.call_args.kwargs['json']
            self.assertEqual(payload['title'],e['title']);self.assertEqual(payload['template'],'html')
            self.assertEqual(payload['channel'],'wechat')
            self.assertEqual(payload['content'].count(e['title']),1)
            self.assertIn('<h1',payload['content']);self.assertIn('<main>',payload['content'])
            dispatch(self.s,n,[e]);self.assertEqual(request.call_count,1)
            summary={'key':'html-summary','title':'分析摘要','body':'市场概况','notification_topic':'codex:analysis'}
            dispatch(self.s,n,[summary]);payload=request.call_args.kwargs['json']
            self.assertEqual(payload['template'],'markdown');self.assertEqual(payload['content'],'### 分析摘要\n\n市场概况')
        self.assertEqual(self.s.conn.execute('SELECT body FROM alerts WHERE key=?',(e['key'],)).fetchone()[0],e['body'])

    def test_serverchan_keeps_markdown_fallback_without_repeating_title(self):
        settings=replace(self.settings,pushplus_token='',serverchan_sendkey='unit-html-sendkey')
        self.response.json.return_value={'code':0}
        with patch('monitor.notify.requests.post',return_value=self.response) as request:
            e=event();dispatch(self.s,Notifier(settings),[e])
            data=request.call_args.kwargs['data']
            self.assertEqual(data['title'],e['title']);self.assertEqual(data['desp'],e['body'])
            self.assertNotIn('<h1',data['desp'])

    def test_failed_message_retries_html_once_then_deduplicates(self):
        bad=Mock(ok=False,status_code=503);bad.json.return_value={'code':500}
        at=datetime.now(timezone.utc);e=event();n=Notifier(self.settings)
        with patch('monitor.notify.requests.post',side_effect=[bad,self.response]) as request:
            dispatch(self.s,n,[e],at);dispatch(self.s,n,[],at+timedelta(seconds=61))
            self.assertEqual(request.call_count,2)
            self.assertEqual(request.call_args_list[0].kwargs['json']['content'],request.call_args_list[1].kwargs['json']['content'])
            self.assertEqual(request.call_args_list[1].kwargs['json']['template'],'html')
            dispatch(self.s,n,[e],at+timedelta(seconds=120));self.assertEqual(request.call_count,2)
