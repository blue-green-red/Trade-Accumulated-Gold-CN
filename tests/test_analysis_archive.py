import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_ai_speed as speed
from monitor.analysis_archive import decode,export
from monitor.controls import set_enabled


class AnalysisArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f=speed.SpeedTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def rows(self):
        c=self.f.store.conn
        return [dict(r) for r in c.execute('select * from ai_analysis_archives order by id')]
    def artifacts(self,archive_id):
        return [(r['kind'],decode(r['payload'])) for r in self.f.store.conn.execute('select * from ai_analysis_artifacts where archive_id=? order by id',(archive_id,))]

    def test_exact_codex_request_schema_context_and_resume_lineage(self):
        f=self.f
        self.assertIsNotNone(f.a.analyze(f.ctx,'first',run_id='archive-first'))
        self.assertIsNotNone(f.a.analyze(f.ctx,'second',run_id='archive-second'))
        rows=self.rows();self.assertEqual([r['status'] for r in rows],['completed','completed'])
        first=dict(self.artifacts(rows[0]['id']))
        self.assertEqual(len(first['input_context']['trading_accounts']['accounts']['short_full']['recent_fills']),6)
        self.assertEqual(len(first['request']['context']['trading_accounts']['accounts']['short_full']['recent_fills']),3)
        self.assertEqual(first['request']['prompt'],f.calls[0][0][-1])
        self.assertEqual(first['request']['schema'],json.loads(Path(f.calls[0][0][f.calls[0][0].index('--output-schema')+1]).read_text()))
        second=dict(self.artifacts(rows[1]['id']))
        self.assertEqual(second['request']['mode'],'resume')
        self.assertEqual(second['request']['parent_archive_id'],rows[0]['id'])
        self.assertEqual(second['request']['prompt'],f.calls[1][0][-1])
        self.assertIn('model_output',first);self.assertIn('runtime_event',first)
        self.assertIn('tool_definition',first)

    def test_failed_cancelled_and_no_run_id_are_not_lost(self):
        f=self.f;f.a._run_process=Mock(side_effect=RuntimeError('offline'))
        self.assertIsNone(f.a.analyze(f.ctx,'failure'))
        row=self.rows()[0];self.assertEqual(row['status'],'failed');self.assertIsNone(row['run_id'])
        self.assertIn('request',dict(self.artifacts(row['id'])))
        def cancelled(*args,**kwargs):
            set_enabled(f.store.conn,'codex',False)
            raise RuntimeError('cancelled')
        f.a._run_process=cancelled
        self.assertIsNone(f.a.analyze(f.ctx,'cancel'))
        self.assertEqual(self.rows()[-1]['status'],'cancelled')
        self.assertIn('outcome',dict(self.artifacts(self.rows()[-1]['id'])))

    def test_secrets_are_redacted_export_streams_and_never_overwrites(self):
        f=self.f;secret='test-credential-do-not-persist-982756'
        context={**f.ctx,'diagnostic':{'password':secret,'note':'message '+secret}}
        with patch.dict(os.environ,{'EXAMPLE_API_KEY':secret}):
            self.assertIsNotNone(f.a.analyze(context,'redact',run_id='redact'))
        row=self.rows()[0]
        text=json.dumps(self.artifacts(row['id']),ensure_ascii=False)
        self.assertNotIn(secret,text);self.assertIn('[REDACTED]',text)
        output=Path(f.tmp.name)/'export.jsonl'
        self.assertEqual(export(f.settings.db_path,output,'codex'),1)
        item=json.loads(output.read_text());self.assertTrue(item['execution_link_valid'])
        self.assertEqual(item['status'],'completed')
        self.assertEqual(output.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):export(f.settings.db_path,output)
        # A later reset must not silently join a historical archive to reused view IDs.
        f.store.conn.execute("update ai_views set started_at='2099-01-01T00:00:00+00:00'");f.store.conn.commit()
        second=Path(f.tmp.name)/'second.jsonl';export(f.settings.db_path,second)
        self.assertFalse(json.loads(second.read_text())['execution_link_valid'])

    def test_invalidated_output_is_retained_but_not_a_success_label(self):
        f=self.f
        def invalidated(context,usage):
            from monitor.price_guard import read,save
            state=read(f.store.conn,'ai_price_guard:codex')
            state.update(invalidated_at='2026-10-27T00:00:00+00:00',reason='price changed',source='jd',move_pct=1)
            save(f.store.conn,'ai_price_guard:codex',state);f.store.conn.commit()
            return 'notes',[],speed.response()
        f.a._run_codex=invalidated
        self.assertIsNotNone(f.a.analyze(f.ctx,'move',run_id='move'))
        row=self.rows()[0];self.assertEqual(row['status'],'invalidated')
        self.assertIn('model_output',dict(self.artifacts(row['id'])))

    def test_claude_code_archives_exact_system_user_and_raw_result(self):
        from types import SimpleNamespace as NS
        f=self.f;f.a.backend='claude_code';calls=[]
        def process(cmd,**kwargs):
            calls.append(cmd)
            return NS(stdout=json.dumps({'subtype':'success','structured_output':speed.response().model_dump()}),stderr='',returncode=0)
        f.a._run_process=process
        self.assertIsNotNone(f.a.analyze(f.ctx,'claude',run_id='claude'))
        artifacts=dict(self.artifacts(self.rows()[0]['id']))
        self.assertEqual(artifacts['request']['system'],calls[0][calls[0].index('--system-prompt')+1])
        self.assertEqual(artifacts['request']['user'],calls[0][calls[0].index('-p')+1])
        self.assertIn('structured_output',artifacts['cli_response']['stdout'])

    def test_native_api_requests_include_history_results_and_final_schema(self):
        from types import SimpleNamespace as NS
        f=self.f
        answer=speed.response()
        f.a.backend='api'
        f.a._create=Mock(side_effect=[
            NS(stop_reason='tool_use',content=[NS(type='tool_use',name='query_account_history',id='h1',input={'limit':1})]),
            NS(stop_reason='tool_use',content=[NS(type='tool_use',name='submit_portfolio_decisions',id='s1',input=answer.model_dump())])])
        f.a._add_usage=Mock()
        self.assertIsNotNone(f.a.analyze(f.ctx,'api',run_id='api'))
        artifacts=self.artifacts(self.rows()[0]['id'])
        requests=[v for k,v in artifacts if k=='request']
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0]['messages'][0]['role'],'user')
        self.assertEqual(requests[1]['messages'][-1]['content'][0]['type'],'tool_result')
        self.assertEqual(requests[0]['tools'][0]['name'],'submit_portfolio_decisions')
        self.assertIn('history_query',dict(artifacts))
