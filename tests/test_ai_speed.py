"""Offline tests for compaction, explicit session isolation and CLI metrics."""
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from monitor.config import Settings
from monitor.store import Store,utc_now
from monitor.ai import AIAnalyst
from monitor.ai_inputs import compact_context
from monitor.codex_runtime import Events,choose_session
from monitor.controls import set_enabled
from test_autonomous import response,LABELS


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=replace(Settings(),db_path=Path(self.tmp.name)/'speed.db',
            ai_optimize=True,codex_resume_enabled=True,ai_max_searches=0,clawx_api_key='',anthropic_api_key='')
        self.store=Store(self.settings.db_path);self.a=AIAnalyst(self.settings,self.store,'codex','codex')
        self.ctx={'snapshot_at':utc_now().isoformat(),'xauusd':4000,'jin10_flash_recent':[{'time_bj':'10-19 12:00','text':'test'}],
            'trading_accounts':{'control_generation':0,'accounts':{k:{'key':'codex:'+k,'revision':1,'cash':100000.123456,
                'grams':.123456789,'stop_price':None,'target_price':1100,'recent_fills':[{'price':1000,'reason':'x'*300} for _ in range(6)]} for k in LABELS}}}
        self.calls=[]
        def process(cmd,**kwargs):
            self.calls.append((cmd,kwargs))
            self.assertNotIn('CLAWX_JR_API_KEY',kwargs['env'])
            out=Path(cmd[cmd.index('-o')+1]);out.write_text(response().model_dump_json())
            records=[{'type':'thread.started','thread_id':'11111111-1111-1111-1111-111111111111'},
                {'type':'item.started','item':{'id':'a','type':'agent_message'}},
                {'type':'turn.completed','usage':{'input_tokens':1000,'cached_input_tokens':800,'output_tokens':120}}]
            stdout='\n'.join(json.dumps(r) for r in records)+'\n'
            kwargs['on_stdout'](stdout)
            return SimpleNamespace(stdout=stdout,stderr='',returncode=0)
        self.a._run_process=process
    def tearDown(self):self.store.conn.close();self.tmp.cleanup()

    def test_compaction_preserves_all_account_numbers_protections_and_trigger_facts(self):
        original=deepcopy(self.ctx);original['trigger_events']=[{'id':'news','data':{'text':'test','time_bj':'10-19 12:00','actual':0}}]
        packed=compact_context(original)
        self.assertEqual(len(packed['trading_accounts']['accounts']),6)
        for key,a in packed['trading_accounts']['accounts'].items():
            for f in ('cash','grams','stop_price','target_price','revision'):self.assertEqual(a[f],original['trading_accounts']['accounts'][key][f])
            self.assertEqual(len(a['recent_fills']),3)
        self.assertEqual(packed['trigger_events'],original['trigger_events'])
        self.assertEqual(packed['jin10_flash_recent'],[])
        self.assertEqual(len(original['trading_accounts']['accounts']['short_full']['recent_fills']),6)

    def test_session_resume_uses_explicit_id_stable_paths_and_counts_cache_once(self):
        first={};self.a._run_codex(self.ctx,first)
        second={};self.a._run_codex(self.ctx,second)
        a,b=self.calls
        self.assertEqual(a[1]['cwd'],b[1]['cwd']);self.assertNotIn('--ephemeral',a[0])
        self.assertIn('resume',b[0]);self.assertIn('11111111-1111-1111-1111-111111111111',b[0]);self.assertNotIn('--last',b[0])
        self.assertIn('-s',b[0]);self.assertIn('read-only',b[0]);self.assertIn('--output-schema',b[0])
        self.assertEqual(second['cached'],800);self.assertEqual(second['input'],1000)
        self.assertEqual(second['performance']['session_turn'],2)
        self.assertIn('previously_provided',b[0][-1])
        self.assertLess(second['performance']['input_chars_after'],second['performance']['input_chars_before'])

    def test_switch_generation_resets_session(self):
        self.a._run_codex(self.ctx,{})
        set_enabled(self.store.conn,'codex',False);set_enabled(self.store.conn,'codex',True)
        self.a._run_codex(self.ctx,{})
        self.assertNotIn('resume',self.calls[-1][0])

    def test_turn_limit_rebuilds_session_and_failure_clears_it(self):
        self.a.settings=replace(self.settings,codex_session_max_turns=1)
        self.a._run_codex(self.ctx,{});self.a._run_codex(self.ctx,{})
        self.assertNotIn('resume',self.calls[-1][0])
        self.a._run_process=Mock(side_effect=RuntimeError('offline'))
        with self.assertRaises(RuntimeError):self.a._run_codex(self.ctx,{})
        self.assertEqual(self.store.get_state('codex_session:codex'),{})

    def test_old_output_cannot_be_replayed_when_current_cli_does_not_write_file(self):
        self.a._run_codex(self.ctx,{})
        self.a._run_process=Mock(return_value=SimpleNamespace(stdout='',stderr='',returncode=0))
        with self.assertRaises(RuntimeError):self.a._run_codex(self.ctx,{})

    def test_reset_conditions_for_age_day_model_and_generation(self):
        now=utc_now();prev={'id':'s','fingerprint':'f','generation':0,'day':now.astimezone(__import__('monitor.config',fromlist=['BJ']).BJ).date().isoformat(),'started_at':now.isoformat(),'turns':1}
        self.assertIsNotNone(choose_session(prev,'f',0,self.settings,now)[0])
        self.assertEqual(choose_session(prev,'other',0,self.settings,now)[1],'configuration_changed')
        self.assertEqual(choose_session(prev,'f',1,self.settings,now)[1],'switch_changed')
        prev['day']='2000-01-01';self.assertEqual(choose_session(prev,'f',0,self.settings,now)[1],'new_day')
        prev['day']=now.astimezone(__import__('monitor.config',fromlist=['BJ']).BJ).date().isoformat();prev['started_at']=(now-timedelta(hours=5)).isoformat()
        self.assertEqual(choose_session(prev,'f',0,self.settings,now)[1],'session_age')

    def test_legacy_toggle_keeps_ephemeral_independent_requests(self):
        self.a.settings=replace(self.settings,ai_optimize=False)
        self.a._run_codex(self.ctx,{});self.a._run_codex(self.ctx,{})
        self.assertNotEqual(self.calls[0][1]['cwd'],self.calls[1][1]['cwd'])
        self.assertTrue(all('--ephemeral' in cmd for cmd,_ in self.calls))

    def test_analyze_persists_metrics_and_model_input_without_executing_trades(self):
        result=self.a.analyze(self.ctx,'test',run_id='speed')
        self.assertEqual(result['_performance']['cached_input_tokens'],800)
        row=self.store.conn.execute('SELECT metrics_json FROM ai_runs').fetchone()
        self.assertEqual(json.loads(row[0])['input_tokens'],1000)
        saved=self.store.get_state('ai_model_input:speed:codex')
        self.assertEqual(len(saved['trading_accounts']['accounts']),6)

    def test_events_handle_partial_utf8_and_missing_cache_usage(self):
        usage={};events=Events(usage)
        raw=(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'中文'}},ensure_ascii=False)+'\n'+json.dumps({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':2}})).encode()
        for i in range(1,len(raw)):events.feed(raw[:i])
        events.feed(raw,final=True)
        self.assertEqual(usage['input'],5);self.assertNotIn('cached',usage)


if __name__=='__main__':unittest.main()
