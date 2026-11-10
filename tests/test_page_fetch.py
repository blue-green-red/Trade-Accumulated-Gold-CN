import io
import gzip
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from monitor.page_fetch import Article,PageError,PublicPageReader,download,excerpt,target,NAME
from monitor.history_tool import serve
from monitor.history_access import session
import test_autonomous as fixtures

URL='https://example.org/article'
PUBLIC=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
DATA={'url':URL,'http_status':200,'content_type':'text/html','title':'Public source',
      'published_at':'2026-11-10','fetched_at':'2026-11-10T09:00:00+00:00',
      'blocks':['Publication date November 10, 2026.','If inflation rises, a 50 basis point hike could be considered.']}


class PageReaderTests(unittest.TestCase):
    def test_article_extraction_removes_navigation_scripts_and_svg_titles(self):
        p=Article();p.feed('<html><head><title>Official speech</title></head><body><nav>MENU</nav><script>EXECUTE</script>'
            '<main>General page content.<p class="article__time">November 10, 2026</p><div id="article"><p>Date: November 10.</p>'
            '<p>'+('Relevant article text. '*10)+'</p><svg><title>Lock</title></svg></div></main><footer>FOOTER</footer></body></html>')
        r=p.result();text=' '.join(r['blocks'])
        self.assertEqual(r['title'],'Official speech')
        self.assertEqual(r['published_at'],'November 10, 2026')
        for excluded in ('MENU','EXECUTE','FOOTER','General page','Lock'):self.assertNotIn(excluded,text)
        self.assertIn('Relevant article',text)

    def test_focus_excerpt_retains_conditional_context_and_marks_truncation(self):
        blocks=['Date: November 10.','Introduction.']+['Background '+str(i)+' x'*100 for i in range(20)]
        blocks+=['The decision depends on upcoming inflation.','If inflation rises, 50 basis points could be considered.','This is not a predetermined decision.']
        r=excerpt({**DATA,'blocks':blocks},'50 basis',700)
        self.assertLessEqual(len(r['text']),700);self.assertTrue(r['truncated'])
        for phrase in ('If inflation rises','depends on upcoming inflation','not a predetermined'):self.assertIn(phrase,r['text'])

    def test_metadata_and_nested_markup_are_bounded(self):
        p=Article();p.feed('<title>'+'x'*500+'</title><meta name="date" content="'+'x'*200+'"><article><p>'+'body '*30+'</p></article>')
        result=p.result();self.assertEqual(len(result['title']),240);self.assertEqual(len(result['published_at']),80)
        with self.assertRaises(PageError):Article().feed('<div>'*300)

    @patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC)
    def test_public_urls_reject_credentials_methods_and_sensitive_query_params(self,dns):
        for url in ('file:///etc/passwd','http://user:password@example.org','http://example.org:18080/',
                    'https://example.org/?api_key=secret','https://example.org/\r\nheader'):
            with self.assertRaises(PageError):target(url)
        r=PublicPageReader(fetcher=Mock()).call({'url':URL,'headers':{'Authorization':'secret'}})
        self.assertEqual(r['error_code'],'invalid_arguments')

    def test_private_loopback_metadata_and_mixed_dns_answers_are_blocked(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','192.168.1.1','100.64.0.1','224.0.0.1'):
            with patch('monitor.page_fetch.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,80))]):
                with self.assertRaises(PageError):target(URL)
        with patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC+[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]):
            with self.assertRaises(PageError):target(URL)

    @patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC)
    def test_cache_budget_and_exact_tool_audit(self,dns):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/'cache';audit=Path(tmp)/'audit.jsonl';fetch=Mock(return_value=DATA)
            reader=PublicPageReader(2,audit,cache,fetch)
            first=reader.call({'url':URL});second=reader.call({'url':URL})
            self.assertFalse(first['cached']);self.assertTrue(second['cached']);fetch.assert_called_once()
            self.assertEqual(reader.call({'url':URL})['error_code'],'budget_exhausted')
            records=[json.loads(s) for s in audit.read_text().splitlines()]
            self.assertEqual(records[0]['result'],first)
            cache_file=next(cache.glob('*.json'));value=json.loads(cache_file.read_text());value['cached_at']=0;cache_file.write_text(json.dumps(value))
            PublicPageReader(1,cache=cache,fetcher=fetch).call({'url':URL});self.assertEqual(fetch.call_count,2)

    @patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC)
    def test_timeout_and_http_failure_do_not_return_false_body(self,dns):
        for exception,code in ((TimeoutError(),'timeout'),(PageError('http_error','HTTP 403',http_status=403),'http_error')):
            r=PublicPageReader(fetcher=Mock(side_effect=exception)).call({'url':URL})
            self.assertEqual(r['error_code'],code);self.assertNotIn('text',r)

    def test_mcp_fetch_only_catalog_and_call(self):
        reader=PublicPageReader(fetcher=lambda url:DATA)
        packets=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':NAME,'arguments':{'url':URL}}}]
        out=io.StringIO()
        with patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC):
            serve(SimpleNamespace(maximum=0),io.StringIO('\n'.join(map(json.dumps,packets))+'\n'),out,page_reader=reader)
        values=[json.loads(line)['result'] for line in out.getvalue().splitlines()]
        self.assertEqual([t['name'] for t in values[0]['tools']],[NAME]);self.assertFalse(values[1]['isError'])
        self.assertIn('50 basis',json.loads(values[1]['content'][0]['text'])['text'])


class Response:
    def __init__(self,body=b'',status=200,kind='text/html',**headers):
        self.status=status;self.headers=Message();self.headers['Content-Type']=kind
        for key,value in headers.items():self.headers[key.replace('_','-')]=value
        self.buffer=io.BytesIO(body)
    def getheader(self,key,default=None):return self.headers.get(key,default)
    def read(self,n=-1):return self.buffer.read(n)
    def close(self):self.buffer.close()


class DownloadTests(unittest.TestCase):
    def run_download(self,responses,max_bytes=1024):
        transport=Mock();transport.fileno.return_value=10
        conn=Mock();conn.getresponse.side_effect=responses
        context=Mock();context.wrap_socket.return_value=transport
        with patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC),patch('monitor.page_fetch.socket.create_connection',return_value=transport) as connect,patch('monitor.page_fetch.http.client.HTTPConnection',return_value=conn),patch('monitor.page_fetch.ssl.create_default_context',return_value=context):
            result=download(URL,max_bytes=max_bytes)
        return result,conn,connect,context
    def test_https_uses_validated_ip_and_keeps_host_sni_and_certificate_context(self):
        result,conn,connect,context=self.run_download([Response(b'<html><title>Source</title><article><p>'+b'public body '*20+b'</p></article></html>')])
        self.assertIn('public body',' '.join(result['blocks']))
        self.assertEqual(connect.call_args.args[0],('93.184.216.34',443))
        self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'],'example.org')
        headers=conn.request.call_args.kwargs['headers'];self.assertEqual(headers['Host'],'example.org')
        self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers)
    def test_http_error_unsupported_body_and_gzip_size_limit(self):
        for response,code in ((Response(status=403),'http_error'),(Response(kind='application/pdf'),'unsupported_type'),
            (Response(gzip.compress(b'x'*2000),Content_Encoding='gzip'),'page_too_large')):
            with self.subTest(code=code),self.assertRaises(PageError) as raised:self.run_download([response])
            self.assertEqual(raised.exception.code,code)
    def test_redirect_revalidates_and_blocks_a_private_target(self):
        transport=Mock();conn=Mock();conn.getresponse.return_value=Response(status=302,Location='http://127.0.0.1/private')
        def resolve(host,*args,**kwargs):return PUBLIC if host=='example.org' else [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
        context=Mock();context.wrap_socket.return_value=transport
        with patch('monitor.page_fetch.socket.getaddrinfo',side_effect=resolve),patch('monitor.page_fetch.socket.create_connection',return_value=transport) as connect,patch('monitor.page_fetch.http.client.HTTPConnection',return_value=conn),patch('monitor.page_fetch.ssl.create_default_context',return_value=context):
            with self.assertRaises(PageError) as raised:download(URL)
            self.assertEqual(raised.exception.code,'non_public_address');self.assertEqual(connect.call_count,1)


class BackendWiringTests(unittest.TestCase):
    def setUp(self):self.f=fixtures.AutonomousTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_fetch_can_be_enabled_without_history_and_disabled_with_network(self):
        f=self.f;f.a.settings=replace(f.settings,ai_history_max_queries=0,ai_max_searches=2,ai_page_fetch_max_calls=1)
        with session(f.a,{}, {}) as binding:
            self.assertEqual([d['name'] for d in binding['definitions']],[NAME])
            self.assertIn('mcp__account_history__fetch_public_page',binding['claude_names'])
            self.assertIn('--max-page-fetches',binding['claude']['mcpServers']['account_history']['args'])
        f.a.settings=replace(f.a.settings,ai_max_searches=0)
        with session(f.a,{}, {}) as binding:self.assertIsNone(binding)
    def test_native_api_fetches_within_same_conversation_before_deciding(self):
        f=self.f;f.a.backend='api';f.a._check_active=Mock();f.a._add_usage=Mock()
        f.a.settings=replace(f.settings,ai_max_searches=2,ai_page_fetch_max_calls=1)
        f.a._create=Mock(side_effect=[SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',id='page1',name=NAME,input={'url':URL})]),
            SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',id='final',name='submit_portfolio_decisions',input=fixtures.response().model_dump())])])
        with patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC),patch('monitor.page_fetch.download',return_value=DATA):
            result=f.a.analyze(f.context(),'test',run_id='page-native')
        self.assertIsNotNone(result);self.assertEqual(f.a._create.call_count,2)
        self.assertEqual(result['_performance']['page_fetches'],1)
        messages=f.a._create.call_args.kwargs['messages']
        self.assertEqual(messages[2]['content'][0]['tool_use_id'],'page1')
        self.assertIn('50 basis',messages[2]['content'][0]['content'])
    def test_classic_research_api_also_reads_fallback_and_preserves_source(self):
        f=self.f;f.a._check_active=Mock();f.a._add_usage=Mock();f.a.settings=replace(f.settings,ai_max_searches=2)
        f.a._create=Mock(side_effect=[SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',id='page1',name=NAME,input={'url':URL})]),
            SimpleNamespace(stop_reason='end_turn',content=[SimpleNamespace(type='text',text='根据实际原文形成的研究摘要。')])])
        with patch('monitor.page_fetch.socket.getaddrinfo',return_value=PUBLIC),patch('monitor.page_fetch.download',return_value=DATA):
            notes,sources=f.a._research({}, {})
        self.assertIn('实际原文',notes);self.assertEqual(sources,[{'title':DATA['title'],'url':URL}])
