"""Synthetic notification formatting; no model calls, live bank reads or real WeChat sends."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from monitor.analysis_notice import fields
from monitor.config import Settings
from monitor.notification_control import set_subscription
from monitor.notification_outbox import dispatch
from monitor.notify import Notifier
from monitor.store import Store
from test_notification_html import Document


def view():
    return {'bias_short':'bearish','bias_long':'neutral','confidence':'low',
        'p_up_3d':.21,'p_down_3d':.65,'p_up_3m':.31,'p_down_3m':.32,
        'summary':'反弹逻辑削弱，关注新的价格和事件。',
        'public_content':{'analysis':{'change_summary':'本轮美元走强。'}},
        'drivers':[{'direction':'利空','factor':f'合成依据{i}','evidence':f'2026-11-12测试来源{i}：已知事实。'} for i in range(5)],
        'upcoming_events':[{'time_bj':f'11-{13+i} 21:30','name':f'合成事件{i}','impact':'公布后重新评估影响。'} for i in range(4)],
        '_completed_at':'2026-11-12T06:00:00+00:00','_snapshot_at':'2026-11-12T05:58:00+00:00',
        'live_account_advice':{'secret_marker':'PRIVATE_ACCOUNT_NOT_A_SUMMARY_SOURCE'}}


class AnalysisNoticeTests(unittest.TestCase):
    def test_summary_is_compact_without_hiding_the_full_view_or_inventing_a_valid_until_time(self):
        v=view();before=deepcopy(v);e=fields(v,'Codex')
        self.assertEqual(e['title'],'📊Codex行情分析｜短期偏空')
        self.assertNotIn('## Codex',e['body'])
        for text in ('关键变化：本轮美元走强','合成依据0','合成依据2','合成事件0','合成事件1',
                     '分析时间11-12 14:00','行情快照11-12 13:58','本轮快照参考','https://gold.goalgoal.fun/#ai/research'):
            self.assertIn(text,e['body'])
        for text in ('合成依据3','合成事件2','置信','3 日：','PRIVATE_ACCOUNT_NOT_A_SUMMARY_SOURCE','有效至'):
            self.assertNotIn(text,e['body'])
        self.assertEqual(v,before);self.assertEqual(v['p_up_3d'],.21)
        self.assertLess(len(e['body']),1000)

    def test_missing_metadata_stays_unknown_and_invalidated_or_hard_event_warnings_remain_visible(self):
        v=view();v.update(_completed_at=None,_snapshot_at=None,drivers=[],upcoming_events=[],
            hard_risk_event=True,hard_risk_note='合成重大风险：'+('必须保留的重要条件。'*50),
            _price_guard={'invalidated_at':'2026-11-12T06:01:00+00:00'})
        v.pop('public_content');e=fields(v,'Claude')
        self.assertIn(v['hard_risk_note'],e['body']);self.assertIn('已排队复评',e['body'])
        self.assertIn('分析时间待确认',e['body']);self.assertNotIn('关键变化：',e['body'])
        self.assertIn('本轮未列出具名后续事件',e['body'])


class AnalysisDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'notices.db');self.addCleanup(self.s.conn.close)
        set_subscription(self.s.conn,'codex:analysis',True)
        self.settings=replace(Settings(),pushplus_token='unit-layout-token',serverchan_sendkey='')
        self.ok=Mock(ok=True,status_code=200);self.ok.json.return_value={'code':200}
        self.e={'key':'ai:codex:layout-test','notification_topic':'codex:analysis',**fields(view(),'Codex')}

    def test_provider_sees_one_title_and_ordered_cards_and_private_records_keep_model_identity(self):
        with patch('monitor.notify.requests.post',return_value=self.ok) as request:
            dispatch(self.s,Notifier(self.settings),[self.e]);payload=request.call_args.kwargs['json']
            dispatch(self.s,Notifier(self.settings),[self.e]);self.assertEqual(request.call_count,1)
        doc=Document(payload['content'])
        self.assertEqual(payload['template'],'html');self.assertEqual(payload['title'],'📊AI行情分析｜短期偏空')
        self.assertEqual([x for x in doc.headings if x[0]=='h1'],[('h1',payload['title'])])
        self.assertEqual([text for tag,text in doc.headings if tag=='h2'],
            ['本轮结论','关键变化','关键依据','下一次观察','时间与有效性'])
        self.assertNotIn('codex',''.join(doc.text).lower())
        self.assertEqual(self.s.conn.execute('SELECT title FROM alerts').fetchone()[0],self.e['title'])
        original=json.loads(self.s.conn.execute('SELECT event_json FROM notification_outbox').fetchone()[0])
        self.assertEqual(original,self.e)

    def test_retry_is_deterministic_and_serverchan_uses_clean_markdown(self):
        bad=Mock(ok=False,status_code=503);bad.json.return_value={'code':500};at=datetime.now(timezone.utc)
        with patch('monitor.notify.requests.post',side_effect=[bad,self.ok]) as request:
            dispatch(self.s,Notifier(self.settings),[self.e],at)
            dispatch(self.s,Notifier(self.settings),[],at+timedelta(seconds=61))
            self.assertEqual(request.call_args_list[0].kwargs['json'],request.call_args_list[1].kwargs['json'])
        fallback=replace(self.settings,pushplus_token='',serverchan_sendkey='unit-layout-sendkey')
        self.ok.json.return_value={'code':0}
        with patch('monitor.notify.requests.post',return_value=self.ok) as request:
            dispatch(self.s,Notifier(fallback),[{**self.e,'key':'ai:codex:fallback'}],at)
        data=request.call_args.kwargs['data']
        self.assertIn('AI行情分析',data['title']);self.assertTrue(data['desp'].startswith('本轮结论：'))
        self.assertNotIn('<article',data['desp']);self.assertNotIn('## AI',data['desp'])
        self.assertIn('本轮美元走强',data['desp'])
