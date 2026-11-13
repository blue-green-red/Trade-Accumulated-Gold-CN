from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

from monitor.analysis_archive import encode
from monitor.config import BJ
from monitor.research_evidence import (STATE,INPUT_CHARS,NEWS_CHARS,build,extracted,
    public_url,select_news,selected_text)

NOW=datetime(2026,11,13,8,tzinfo=timezone.utc)


def flash(text,minutes=0,important=True):
    return {'time_bj':(NOW-timedelta(minutes=minutes)).astimezone(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'important':important,'text':text}


class NewsSelectionTests(unittest.TestCase):
    def test_policy_counterpoint_survives_latest_important_saturation(self):
        rows=[flash('沃勒支持较大幅度加息。新闻'+str(i),i) for i in range(24)]
        rows += [flash('哈玛克支持渐进加息，并不支持更激进加息。',700,False)]
        original=deepcopy(rows);out=select_news(rows,NOW)
        self.assertEqual(rows,original);self.assertLessEqual(len(out),18)
        self.assertTrue(any('哈玛克' in r['text'] and '并不支持' in r['text'] for r in out))
        self.assertTrue(any('沃勒' in r['text'] for r in out))
        self.assertLessEqual(sum(len(r['text']) for r in out),NEWS_CHARS)

    def test_late_roundup_qualification_not_cut_off_at_160(self):
        text='美元汇总：'+''.join(f'{i}. 普通财政事项需要进一步了解。' for i in range(1,25))
        text+='25. 哈玛克：支持幅度更小、更稳定的加息，并不支持加速。'
        value=select_news([flash(text)],NOW)[0]
        self.assertIn('并不支持加速',value['text']);self.assertTrue(value['text_truncated'])
        self.assertLessEqual(len(value['text']),240)

    def test_future_stale_duplicate_and_nonpolicy_old_filtered(self):
        rows=[flash('美联储加息',-1),flash('美联储加息',1500),flash('一般市场消息',500,False),
              flash('哈玛克倾向渐进加息',1000,False),flash('黄金ETF持仓变化',10),flash('黄金ETF持仓变化',11)]
        values=select_news(rows,NOW)
        self.assertEqual(len(values),2)
        self.assertFalse(any(v['text']=='美联储加息' for v in values))
        self.assertEqual(values,sorted(values,key=lambda r:r['time_bj'],reverse=True))

    def test_long_english_body_keeps_named_qualifications(self):
        text=('Inflation remains elevated. '*30+'\nWaller considered larger rate hikes.\n'
              'But Hammack does not favor more aggressive rises and supports gradual smaller steps.\n'+'Other context. '*40)
        selected,truncated=selected_text(text,300)
        self.assertTrue(truncated);self.assertIn('Hammack does not favor',selected)
        self.assertIn('Waller',selected);self.assertLessEqual(len(selected),300)


class PublicEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
            CREATE TABLE ai_analysis_archives(id INTEGER PRIMARY KEY,view_id INTEGER,run_id TEXT,status TEXT,completed_at TEXT);
            CREATE TABLE ai_analysis_artifacts(archive_id INTEGER,id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,payload BLOB);
            CREATE TABLE ai_views(run_id TEXT,sources_json TEXT);''')
        self.addCleanup(self.db.close)

    def archive(self,aid,at,items,*,status='completed'):
        cited=[{'url':i['url']} for i in items]
        self.db.execute('INSERT INTO ai_analysis_archives VALUES(?,?,?,?,?)',(aid,aid,str(aid),status,at.isoformat()))
        self.db.execute('INSERT INTO ai_views(rowid,run_id,sources_json) VALUES(?,?,?)',(aid,str(aid),json.dumps(cited)))
        event={'type':'item.completed','item':{'type':'web_search','results':items}}
        self.artifact(aid,at,'runtime_event',event);self.db.commit()

    def artifact(self,aid,at,kind,value):
        self.db.execute('INSERT INTO ai_analysis_artifacts(archive_id,ts,kind,payload) VALUES(?,?,?,?)',
            (aid,at.isoformat(),kind,encode(value)[0]));self.db.commit()

    def test_both_public_views_reused_without_model_opinions_or_network(self):
        self.archive(1,NOW-timedelta(minutes=25),[{'url':'https://www.axios.com/2026/11/12/fed','title':'Fed risks',
            'snippet':'Some officials consider faster hikes.'}])
        self.archive(2,NOW-timedelta(minutes=2),[{'url':'https://www.kitco.com/news/hammack','title':'Gradual hikes',
            'snippet':'Hammack does not favor more aggressive rises.'}])
        self.artifact(2,NOW-timedelta(minutes=2),'model_output',{'reason':'private target 9123','cash':7654321})
        self.artifact(2,NOW-timedelta(minutes=2),'runtime_event',{'type':'item.completed','item':{'type':'agent_message','text':'private plan 9123'}})
        with patch('socket.getaddrinfo',side_effect=AssertionError('No network')):
            value=build(self.db,NOW)
        text=json.dumps(value,ensure_ascii=False)
        self.assertIn('faster hikes',text);self.assertIn('does not favor',text)
        self.assertNotIn('9123',text);self.assertNotIn('7654321',text)
        self.assertTrue(all(r['kind']=='search_excerpt' and r['published_at'] is None for r in value['items']))
        self.assertLessEqual(len(json.dumps(value,ensure_ascii=False,separators=(',',':'))),INPUT_CHARS)
        self.assertEqual(build(self.db,NOW),value)

    def test_observation_date_not_relabelled_as_publication_or_refreshed_by_repeat(self):
        url='https://www.kitco.com/news/hammack'
        first=NOW-timedelta(hours=20)
        self.archive(1,first,[{'url':url,'title':'Hammack','snippet':'Gradual hikes.'}]);build(self.db,NOW-timedelta(hours=1))
        self.archive(2,NOW-timedelta(minutes=1),[{'url':url,'title':'Hammack','snippet':'Gradual hikes.'}])
        item=build(self.db,NOW)['items'][0]
        self.assertEqual(item['first_observed_at'],first.isoformat());self.assertIsNone(item['published_at'])

    def test_explicit_publisher_timestamp_preserved_and_recent_counterpoint_not_crowded_by_old_body(self):
        self.archive(1,NOW-timedelta(hours=2),[{'url':'https://example.com/old','snippet':'Older policy statement.'}])
        self.artifact(1,NOW-timedelta(hours=2),'public_page_fetch',{'result':{'url':'https://example.com/old','text':'Fed policy. '*100}})
        self.archive(2,NOW-timedelta(minutes=1),[{'url':'https://example.com/new','snippet':
            'Date: Thu Nov 12 16:55:07 2026 UTC ... Hammack supports gradual hikes.'}])
        values=build(self.db,NOW)['items']
        self.assertTrue(values[0]['url'].endswith('/new'))
        self.assertEqual(values[0]['published_at'],'2026-11-12T16:55:07+00:00')
        self.assertNotEqual(values[0]['published_at'],values[0]['first_observed_at'])

    def test_fuller_page_not_downgraded_and_future_or_failed_pages_excluded(self):
        url='https://www.kitco.com/news/hammack'
        self.archive(1,NOW-timedelta(minutes=5),[{'url':url,'title':'Policy','snippet':'Hawkish policy.'}])
        self.artifact(1,NOW-timedelta(minutes=4),'public_page_fetch',{'result':{'url':url,'title':'Policy',
            'published_at':(NOW-timedelta(days=1)).isoformat(),'text':'But Hammack does not favor more aggressive hikes.'}})
        self.artifact(1,NOW-timedelta(minutes=3),'public_page_fetch',{'result':{'url':'https://example.com/future',
            'published_at':(NOW+timedelta(days=1)).isoformat(),'text':'future'}})
        self.artifact(1,NOW-timedelta(minutes=3),'public_page_fetch',{'result':{'url':'https://example.com/failed','error':'blocked','text':'not a body'}})
        build(self.db,NOW-timedelta(minutes=2))
        self.archive(2,NOW-timedelta(minutes=1),[{'url':url,'title':'Policy','snippet':'Shorter headline.'}])
        value=build(self.db,NOW);self.assertEqual(len(value['items']),1)
        self.assertEqual(value['items'][0]['kind'],'page_excerpt');self.assertIn('does not favor',value['items'][0]['text'])

    def test_asof_reads_do_not_leak_future_or_erase_newer_shared_cache(self):
        self.archive(1,NOW-timedelta(minutes=25),[{'url':'https://example.com/old','snippet':'Old public evidence.'}])
        self.archive(2,NOW-timedelta(minutes=2),[{'url':'https://example.com/new','snippet':'New public evidence.'}])
        build(self.db,NOW);saved=self.db.execute('SELECT value FROM state WHERE key=?',(STATE,)).fetchone()[0]
        past=build(self.db,NOW-timedelta(minutes=10),persist=False)
        self.assertNotIn('/new',json.dumps(past))
        self.assertEqual(self.db.execute('SELECT value FROM state WHERE key=?',(STATE,)).fetchone()[0],saved)
        build(self.db,NOW-timedelta(minutes=10))
        retained=json.loads(self.db.execute('SELECT value FROM state WHERE key=?',(STATE,)).fetchone()[0])
        self.assertTrue(any(r['url'].endswith('/new') for r in retained))
        self.assertEqual(build(self.db,NOW+timedelta(hours=49)),{})

    def test_cache_input_is_bounded_and_local_authenticated_api_links_rejected(self):
        self.archive(1,NOW-timedelta(minutes=1),[{'url':f'https://example.com/{i}','title':'x'*160,'snippet':'Fed policy. '*100} for i in range(50)])
        value=build(self.db,NOW)
        self.assertLessEqual(len(value['items']),4)
        self.assertLessEqual(len(json.dumps(value,ensure_ascii=False,separators=(',',':'))),INPUT_CHARS)
        for url in ('http://127.0.0.1/a','https://api.jdjygold.com/path','https://user:pass@example.com/a',
                    'https://example.com/a?token=secret','http://169.254.169.254/latest/meta-data'):
            self.assertIsNone(public_url(url))
        self.assertEqual(public_url('https://example.com/a?utm_source=test'),'https://example.com/a')


if __name__=='__main__':unittest.main()
