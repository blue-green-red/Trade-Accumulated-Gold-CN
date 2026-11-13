import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from monitor.ai import AIAnalyst
from monitor.analysis_archive import AnalysisArchive
from monitor.cadence import usage
from monitor.codex_runtime import Events,run_frozen
from monitor.config import Settings
from monitor.participation_audit import (CALIBRATION,actions,digest,evaluate,horizon_at,
    instant,markout,quote_series,refresh_evaluation,request_for,run,source)
from monitor.runtime_resources import slot
from monitor.store import Store,utc_now


def frozen():
    account={'cash':10000,'grams':2,'risk_budget':{'trade_risk_cny':10000,'buy_paused':False}}
    context={'snapshot_at':'2026-11-05T02:00:00+00:00','now_bj':'2026-11-05T10:00:00+08:00',
        'jd_accumulation_quote':{'price':1000},
        'trading_accounts':{'constraints':{'sell_fee_rate':.004,'spread_cny':1},
            'accounts':{k:deepcopy(account) for k in ('low_full','low_empty','standard_full','standard_empty','high_full','high_empty')}},
        'live_readonly_accounts':{'accounts':{'zheshang':{'cash_cny':10000,'total_grams':2,'sellable_grams':2,
            'status':'fresh','quote':{'status':'fresh','buy_price_cny':1000,'sell_price_cny':1000,'quoted_at':'2026-11-05T02:00:00+00:00'},
            'sell_fee_rate':.004,'pending_orders':{'complete':True,'records':[]}}}}}
    output={'account_decisions':{k:{'action':'hold','execution':'market'} for k in context['trading_accounts']['accounts']},
        'live_account_advice':{'zheshang':{'action':'hold','suggested_orders':[]}}}
    return {'archive_id':7,'view_id':7,'snapshot_at':context['snapshot_at'],'original_tools':[],
        'request':{'context':context,'system':'Original policy','user':'Frozen data\n'+json.dumps(context),'schema':{'type':'object'},'mode':'fresh','model':''},
        'baseline':output}


class ParticipationAuditTests(unittest.TestCase):
    def test_only_candidate_system_changes_no_future_labels_or_current_prompts(self):
        value=frozen();before=deepcopy(value)
        base=request_for(value,False);candidate=request_for(value,True)
        self.assertEqual(value,before)
        self.assertEqual(candidate['system'],base['system']+'\n'+CALIBRATION)
        for key in ('user','schema','context','model'):self.assertEqual(base[key],candidate[key])
        self.assertNotIn('markouts',json.dumps(candidate))

    def test_incremental_markout_accounts_for_sale_fee_and_spread(self):
        a={'entry_cny':1000,'sell_fee_rate':.004,'spread_cny':1,'grams':10,'action':'buy'}
        self.assertEqual(markout(a,1020)['price_only_delta_cny'],200)
        self.assertEqual(markout(a,1020)['incremental_net_cny'],149.2)
        self.assertEqual(markout(a,980)['incremental_net_cny'],-249.2)
        a['action']='sell'
        # Both original and future liquidation paths pay the sale fee; spread cancels.
        self.assertEqual(markout(a,1020)['incremental_net_cny'],-199.2)
        a['action']='hold';self.assertEqual(markout(a,1020)['incremental_net_cny'],0)
        a['held_grams']=50
        self.assertEqual(markout(a,1020)['whole_account_net_change_cny'],996)

    def test_actions_distinguish_real_buys_from_reviews_drafts_and_conditional_orders(self):
        v=frozen();out=v['baseline'];live=out['live_account_advice']['zheshang']
        live.update(limit_buy_reviews=[{'status':'viable','grams':3}],suggested_orders=[{'kind':'limit_buy','grams':3}])
        records=actions(v['request']['context'],out)
        self.assertEqual(records['live:zheshang']['grams'],0)
        self.assertEqual(records['live:zheshang']['suggested_buy_count'],1)
        self.assertGreater(records['live:zheshang']['position_before_pct'],0)
        live.update(action='buy',grams=3,price_condition='at_or_below',price_cny=990)
        records=actions(v['request']['context'],out)
        self.assertEqual(records['live:zheshang']['markout_excluded'],'conditional_not_simulated')
        live['price_condition']='reference'
        records=actions(v['request']['context'],out)
        self.assertIsNone(records['live:zheshang']['markout_excluded'])
        self.assertGreater(records['live:zheshang']['position_after_pct'],records['live:zheshang']['position_before_pct'])

    def test_invalid_cash_risk_and_stale_inputs_excluded(self):
        v=frozen();ctx=v['request']['context'];out=v['baseline'];d=out['account_decisions']['high_empty']
        d.update(action='buy',amount_cny=20000,risk_exit_cny=900)
        self.assertEqual(actions(ctx,out)['sim:high_empty']['markout_excluded'],'cash_insufficient_or_unknown')
        d['amount_cny']=1000;ctx['trading_accounts']['accounts']['high_empty']['risk_budget']['trade_risk_cny']=1
        self.assertEqual(actions(ctx,out)['sim:high_empty']['markout_excluded'],'risk_budget_exceeded')
        ctx['live_readonly_accounts']['accounts']['zheshang']['status']='stale'
        self.assertEqual(actions(ctx,out)['live:zheshang']['markout_excluded'],'stale_or_incomplete_account')

    def test_limit_quantity_uses_order_price_and_unacceptable_market_sale_excluded(self):
        v=frozen();ctx=v['request']['context'];out=v['baseline'];d=out['account_decisions']['high_empty']
        d.update(action='buy',execution='limit',limit_price_cny=900,amount_cny=900)
        a=actions(ctx,out)['sim:high_empty']
        self.assertEqual(a['grams'],1);self.assertEqual(a['amount_cny'],900)
        self.assertIsNone(a['position_after_pct']);self.assertEqual(a['markout_excluded'],'conditional_not_simulated')
        d.update(action='sell',execution='market',grams=1,min_execution_price_cny=1050,max_execution_price_cny=1100)
        self.assertEqual(actions(ctx,out)['sim:high_empty']['markout_excluded'],'outside_execution_range')

    def test_equal_horizons_and_products_missing_future_stays_unknown(self):
        v=frozen();proposals=actions(v['request']['context'],v['baseline'])
        self.assertEqual(horizon_at(v['snapshot_at'],3).isoformat(),'2026-11-10T10:00:00+08:00')
        series={'sim':[(instant('2026-11-10T02:03:00+00:00'),1100)]}
        result=evaluate(v['snapshot_at'],proposals,series)
        self.assertEqual(result['sim:high_empty'][0]['quote_cny'],1100)
        self.assertIn('unavailable',result['sim:high_empty'][1])
        self.assertIn('unavailable',result['live:zheshang'][0])
        series['live:zheshang']=[(instant('2026-11-10T03:01:00+00:00'),1200)]
        self.assertIn('unavailable',evaluate(v['snapshot_at'],proposals,series)['live:zheshang'][0])

    def test_tool_events_fail_closed_even_if_reported_search_count_zero(self):
        e=Events({'_frozen_no_tools':True})
        warning=json.dumps({'type':'item.completed','item':{'type':'error','message':'CLI warning'}})+'\n'
        e.feed(warning)
        with self.assertRaisesRegex(RuntimeError,'tool call'):
            e.feed(warning+json.dumps({'type':'item.started','item':{'type':'mcp_tool_call','name':'web'}}),final=True)

    def test_frozen_runner_rejects_enabled_tools_or_resumed_sources(self):
        a=SimpleNamespace(settings=replace(Settings(),codex_resume_enabled=True))
        with self.assertRaises(ValueError):run_frozen(a,frozen()['request'],{})
        a.settings=replace(Settings(),codex_resume_enabled=False,ai_max_searches=0,ai_history_max_queries=0,ai_page_fetch_max_calls=0)
        req=frozen()['request'];req['mode']='resume'
        with self.assertRaisesRegex(ValueError,'Resumed'):run_frozen(a,req,{})

    def test_source_integrity_and_bank_series_use_archived_requests_only(self):
        with tempfile.TemporaryDirectory() as temp:
            s=replace(Settings(),db_path=Path(temp)/'test.db');store=Store(s.db_path);a=AIAnalyst(s,store,'codex','codex');v=frozen()
            try:
                archive=AnalysisArchive(store,a,1,'test','test',v['request']['context'])
                archive.add('request',v['request']);archive.add('model_output',v['baseline']);archive.finish('completed')
                original=source(s.db_path,archive.id)
                self.assertEqual(digest(original['request']['context']),digest(v['request']['context']))
                series=quote_series(s.db_path)
                self.assertEqual(series['live:zheshang'][0][1],1000)
                self.assertNotIn('live:minsheng',series)
                store.conn.execute("UPDATE ai_analysis_artifacts SET sha256='broken' WHERE kind='request'");store.conn.commit()
                with self.assertRaisesRegex(ValueError,'checksum'):source(s.db_path,archive.id)
            finally:store.conn.close()

    def test_run_isolated_budgeted_and_only_reads_future_after_all_generations(self):
        with tempfile.TemporaryDirectory() as temp:
            s=replace(Settings(),db_path=Path(temp)/'production.db');store=Store(s.db_path);AIAnalyst(s,store,'codex','codex')
            calls=[];value=frozen()
            def generate(a,request,metrics):
                self.assertNotEqual(a.settings.db_path,s.db_path)
                self.assertEqual(a.settings.ai_max_searches,0);self.assertEqual(a.settings.ai_page_fetch_max_calls,0)
                self.assertEqual(a.settings.ai_history_max_queries,0);self.assertFalse(a.settings.codex_resume_enabled)
                self.assertEqual(request['context'],value['request']['context'])
                with slot(s.db_path) as acquired:self.assertFalse(acquired)
                calls.append(request['system']);metrics.update(input=20,output=5)
                return '',[],SimpleNamespace(model_dump=lambda **kwargs:value['baseline'])
            def labels(db):self.assertEqual(len(calls),2);return {}
            try:
                with patch('monitor.participation_audit.source',return_value=value),patch('monitor.codex_runtime.run_frozen',generate),patch('monitor.participation_audit.quote_series',labels),patch.object(AIAnalyst,'analyze',side_effect=AssertionError('publication')):
                    report=run(s,[7],Path(temp)/'private',[7])
                self.assertEqual(calls[1],calls[0]+'\n'+CALIBRATION)
                self.assertEqual(usage(store.conn,'codex',utc_now()),2)
                self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM ai_views').fetchone()[0],0)
                if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='jd_live_advice'").fetchone():
                    self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM jd_live_advice').fetchone()[0],0)
                self.assertFalse(report['published']);self.assertFalse(report['execution_invoked'])
                original_bytes=(Path(temp)/'private'/'report.json').read_bytes()
                series={'sim':[(horizon_at(value['snapshot_at'],3),1020)]}
                with patch('monitor.participation_audit.quote_series',return_value=series),patch('monitor.codex_runtime.run_frozen',side_effect=AssertionError('no extra model call')):
                    refreshed=refresh_evaluation(s.db_path,Path(temp)/'private')
                self.assertEqual((Path(temp)/'private'/'report.json').read_bytes(),original_bytes)
                self.assertEqual(refreshed['cases'][0]['arms']['candidate']['markouts']['sim:high_empty'][0]['whole_account_net_change_cny'],39.84)
                for path in (Path(temp)/'private').iterdir():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            finally:store.conn.close()

    def test_budget_failure_makes_no_call_and_generation_failure_charges_no_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            s=replace(Settings(),db_path=Path(temp)/'production.db',ai_max_runs_per_day=1);store=Store(s.db_path);AIAnalyst(s,store,'codex','codex')
            try:
                with patch('monitor.participation_audit.source',return_value=frozen()),patch('monitor.codex_runtime.run_frozen') as call:
                    with self.assertRaisesRegex(RuntimeError,'daily Codex budget'):run(s,[7],Path(temp)/'no-budget',[7])
                    call.assert_not_called()
                with patch('monitor.participation_audit.source',return_value=frozen()),patch('monitor.codex_runtime.run_frozen',side_effect=RuntimeError('offline')),patch('monitor.participation_audit.quote_series') as labels:
                    with self.assertRaisesRegex(RuntimeError,'offline'):run(s,[7],Path(temp)/'failed')
                    labels.assert_not_called()
                self.assertEqual(usage(store.conn,'codex',utc_now()),1)
                self.assertEqual(store.conn.execute('SELECT status FROM ai_runs').fetchone()[0],'diagnostic_failed')
                self.assertTrue((Path(temp)/'failed'/'7.candidate.progress.json').exists())
            finally:store.conn.close()

    def test_busy_resource_slot_does_not_start_or_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            s=replace(Settings(),db_path=Path(temp)/'production.db');output=Path(temp)/'private'
            with patch('monitor.participation_audit.source',return_value=frozen()),slot(s.db_path),patch('monitor.codex_runtime.run_frozen') as call:
                with self.assertRaisesRegex(RuntimeError,'busy'):run(s,[7],output)
                call.assert_not_called();self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
