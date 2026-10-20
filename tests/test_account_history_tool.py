"""Readonly model-scoped history, bounded responses, MCP transport and backend tool wiring."""
import io
import json
import sqlite3
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import test_autonomous as fixtures
from monitor.history_tool import HistoryReader,NAME,serve
from monitor.history_access import session
from monitor.store import utc_now


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AutonomousTests();self.f.setUp()
        self.asof=utc_now().isoformat()
    def tearDown(self):self.f.tearDown()
    def reader(self,model='codex',maximum=6):return HistoryReader(self.f.settings.db_path,model,self.asof,maximum)

    def test_only_own_six_accounts_no_sql_or_model_override(self):
        reader=self.reader()
        result=reader.call({})
        self.assertEqual(len(result['items']),3)
        self.assertTrue(all(r['account'].startswith('codex:') for r in result['items']))
        for args in ({'account':'claude:short_full'},{'account':'codex:short_full'},{'model':'claude'},{'sql':'SELECT * FROM state'}):
            self.assertIn('error',reader.call(args))
        self.assertTrue(all(r['account'].startswith('claude:') for r in self.reader('claude').call({})['items']))

    def test_pagination_time_cutoff_precision_and_text_bound(self):
        f=self.f;initial=f.account('short_full')['grams']
        result=self.reader().call({'account':'short_full','limit':1})
        self.assertEqual(result['items'][0]['grams'],initial)
        first=self.reader().call({'limit':1});second=self.reader().call({'limit':1,'offset':first['next_offset']})
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertTrue(first['has_more'])
        before=(utc_now()-timedelta(days=1)).isoformat()
        self.assertEqual(self.reader().call({'end':before})['items'],[])
        future=(utc_now()+timedelta(days=1)).isoformat()
        self.assertIn('error',self.reader().call({'end':future}))
        f.store.conn.execute("UPDATE ai_portfolio_fills SET reason=? WHERE account='codex:short_full'",('long'*300,));f.store.conn.commit()
        self.assertEqual(len(self.reader().call({'account':'short_full'})['items'][0]['reason']),160)

    def test_invalid_inputs_and_query_budget(self):
        for args in ({'limit':11},{'limit':True},{'offset':-1},{'kind':'state'},{'start':'2026-01-01'}, {'start':self.asof,'end':'2020-01-01T00:00:00Z'}):
            self.assertIn('error',self.reader().call(args))
        r=self.reader(maximum=1)
        self.assertIn('items',r.call({}))
        self.assertIn('error',r.call({}))
        self.assertEqual(r.calls,2)

    def test_decisions_and_cycles_are_readonly_and_scoped(self):
        f=self.f;f.publish(short_empty=dict(reason='r'*500));self.asof=utc_now().isoformat()
        f.m.portfolios.on_tick(fixtures.snapshot());self.asof=utc_now().isoformat()
        old=f.store.conn.total_changes
        plans=self.reader().call({'kind':'plans'})
        self.assertTrue(plans['items'])
        self.assertTrue(all(r['account'].startswith('codex:') for r in plans['items']))
        self.assertNotIn('decision_json',plans['items'][0])
        self.assertTrue(all(len(r['decision']['reason'])<=160 for r in plans['items']))
        self.assertEqual(len(self.reader().call({'kind':'cycles'})['items']),3)
        self.assertEqual(f.store.conn.total_changes,old)

    def test_mcp_initialize_catalog_call_and_errors(self):
        packets=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
                 {'jsonrpc':'2.0','method':'notifications/initialized'},
                 {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                 {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':NAME,'arguments':{'limit':1}}},
                 {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'execute_sql'}}]
        output=io.StringIO();serve(self.reader(),io.StringIO('\n'.join(map(json.dumps,packets))+'\n'),output)
        results=[json.loads(l) for l in output.getvalue().splitlines()]
        self.assertEqual(len(results),4)
        self.assertTrue(results[1]['result']['tools'][0]['annotations']['readOnlyHint'])
        self.assertEqual(len(json.loads(results[2]['result']['content'][0]['text'])['items']),1)
        self.assertIn('error',results[-1])

    def test_standalone_stdio_server_works_without_project_env_or_imports(self):
        args=[sys.executable,'-I',str(Path(__file__).resolve().parents[1]/'monitor/history_tool.py'),'--db',str(self.f.settings.db_path),'--model','codex','--as-of',self.asof]
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':NAME,'arguments':{'limit':1}}}
        result=subprocess.run(args,input=json.dumps(request)+'\n',text=True,capture_output=True,check=True,timeout=5)
        packet=json.loads(result.stdout)
        self.assertFalse(packet['result']['isError'])
        self.assertEqual(result.stderr,'')

    def test_cli_session_binds_identity_and_collects_audit(self):
        f=self.f;usage={}
        with session(f.a,f.context(),usage) as binding:
            spec=binding['claude']['mcpServers']['account_history']
            self.assertIn('--model',spec['args']);self.assertEqual(spec['args'][spec['args'].index('--model')+1],'codex')
            packet={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':NAME,'arguments':{'limit':1}}}
            subprocess.run([spec['command'],*spec['args']],input=json.dumps(packet)+'\n',text=True,capture_output=True,check=True,timeout=5)
        self.assertEqual(usage['performance']['history_queries'],1)
        self.assertEqual(usage['performance']['history_rows'],1)
        self.assertEqual(usage['history_queries'][0]['result']['model'],'codex')
        cancelled={}
        with session(f.a,f.context(),cancelled) as binding:
            binding['reader'].call({'limit':1})
            with binding['reader'].audit.open('a') as out:out.write('{"partial":')
        self.assertEqual(cancelled['performance']['history_queries'],1)
        self.assertTrue(cancelled['performance']['history_audit_incomplete'])
        # Production Claude wiring must retain web permissions when history is added.
        f.a.settings=replace(f.settings,ai_max_searches=2)
        def process(cmd,**kwargs):
            allowed=cmd[cmd.index('--allowedTools')+1:cmd.index('--disallowedTools')]
            self.assertIn('WebSearch',allowed);self.assertIn('WebFetch',allowed)
            self.assertIn('mcp__account_history__query_account_history',allowed)
            self.assertIn('--mcp-config',cmd)
            return SimpleNamespace(returncode=0,stdout=json.dumps({'subtype':'success','structured_output':fixtures.response().model_dump()}),stderr='')
        f.a._run_process=process
        f.a._run_claude_code(f.context(),{})

    def test_api_queries_history_then_submits_and_archives_exact_response(self):
        f=self.f
        f.a.backend='api';f.a._check_active=Mock();f.a._add_usage=Mock()
        f.a._create=Mock(side_effect=[SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',id='h1',name=NAME,input={'limit':1})]),
            SimpleNamespace(stop_reason='tool_use',content=[SimpleNamespace(type='tool_use',id='final',name='submit_portfolio_decisions',input=fixtures.response().model_dump())])])
        value=f.a.analyze(f.context(),'test',run_id='history-native')
        self.assertIsNotNone(value)
        self.assertEqual(f.a._create.call_count,2)
        self.assertEqual(value['_performance']['history_queries'],1)
        records=f.store.get_state('ai_history_queries:history-native:codex')
        self.assertEqual(len(records[0]['result']['items']),1)
        sent=f.a._create.call_args.kwargs['messages']
        self.assertEqual(sent[2]['content'][0]['tool_use_id'],'h1')
        self.assertEqual(f.account()['cash'],100000)

    def test_zero_budget_removes_history_backend(self):
        f=self.f;f.a.settings=replace(f.settings,ai_history_max_queries=0)
        with session(f.a,f.context(),{}) as binding:self.assertIsNone(binding)
