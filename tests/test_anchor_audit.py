import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from monitor.ai import AIAnalyst
from monitor.ai_inputs import compact_context
from monitor.anchor_audit import paired_inputs,run_pair,source_context
from monitor.cadence import usage
from monitor.config import Settings
from monitor.runtime_resources import slot
from monitor.store import Store,utc_now


def frozen():
    account={'cash':123456.789,'grams':12.3456789,'avg_cost':870.01,'revision':7,
        'stop_price':843.27,'target_price':900.32,
        'position_comparison':{'net_sell_price_cny':865.5,'scenarios':{'target':{'price_cny':900.32}}},
        'last_plan':{'status':'pending','reason':'执行价错误900.32','expires_at':'later',
            'decision':{'action':'buy','amount_cny':1000,'limit_price_cny':865.01,
                        'risk_exit_cny':843.27,'stop_loss_cny':843.27,'take_profit_cny':900.32,
                        'reason':'旧目标900.32','invalidation':'跌破843.27'}},
        'recent_fills':[{'ts':'yesterday','side':'buy','grams':1.123,'price':850.15,'fee':0,
                         'reason':'按旧目标900.32、退出843.27'}]}
    return {'snapshot_at':utc_now().isoformat(),'xauusd':4000,
        'jd_daily_samples':{'low':843.27},'jd_accumulation_quote':{'price':868.46},
        'trigger_events':[{'id':'review','data':{'price':900.32}}],'event_coverage':{'codex':['review']},
        'trading_accounts':{'constraints':{'sell_fee_rate':.004,'spread_cny':0},
            'accounts':{k:deepcopy(account) for k in ('low_full','low_empty','standard_full','standard_empty','high_full','high_empty')}},
        'live_readonly_accounts':{'risk_profile':{'key':'high'},'accounts':{'zheshang':{
            'total_grams':210.2859,'cash_cny':753800.97,'quote':{'sell_price_cny':868.44},
            'pending_orders':{'records':[{'kind':'take_profit','price_cny':899.99}]},
            'previous_advice':[{'reason':'旧目标900.32','suggested_orders':[{'loss_price_cny':843.27}]}]}}},
        'previous_public_analysis':{'risk':'旧退出843.27'}}


class AnchorAuditTests(unittest.TestCase):
    def test_mask_does_not_change_source_market_balances_fills_or_actual_orders(self):
        original=frozen();before=deepcopy(original)
        reference,shadow,manifest=paired_inputs(original)
        self.assertEqual(original,before)
        self.assertEqual(reference['trading_accounts']['accounts']['low_full']['stop_price'],843.27)
        for key in ('jd_daily_samples','jd_accumulation_quote','xauusd','snapshot_at'):
            self.assertEqual(reference[key],shadow[key])
            self.assertEqual(original[key],shadow[key])
        self.assertEqual(shadow['jd_daily_samples']['low'],843.27)
        for key in ('trigger_events','event_coverage'):
            self.assertNotIn(key,reference);self.assertNotIn(key,shadow)
        for name,a in shadow['trading_accounts']['accounts'].items():
            r=reference['trading_accounts']['accounts'][name]
            for key in ('cash','grams','revision','avg_cost','recent_fills'):
                self.assertEqual(a[key],r[key])
            self.assertNotIn('stop_price',a);self.assertNotIn('target_price',a)
            self.assertNotIn('scenarios',a['position_comparison'])
            decision=a['last_plan']['decision']
            self.assertEqual(decision,{'action':'buy','amount_cny':1000,'limit_price_cny':865.01})
        live=shadow['live_readonly_accounts']['accounts']['zheshang']
        for key in ('total_grams','cash_cny','quote','pending_orders'):
            self.assertEqual(live[key],reference['live_readonly_accounts']['accounts']['zheshang'][key])
        self.assertNotIn('previous_advice',live);self.assertNotIn('previous_public_analysis',shadow)
        self.assertTrue(manifest['masked_in_shadow'])

    def test_normal_compaction_is_idempotent_and_keeps_ledger_numbers_and_exit_terms(self):
        source=frozen();source['trading_accounts']['accounts']['low_full']['recent_fills']+=[
            {'reason':'持仓止损','price':843.27},{'reason':'实验初始化：满仓起点','price':850}]
        value=compact_context(source);a=value['trading_accounts']['accounts']['low_full']
        self.assertEqual(compact_context(value),value)
        self.assertEqual(a['last_plan'],source['trading_accounts']['accounts']['low_full']['last_plan'])
        self.assertEqual([f['reason'] for f in a['recent_fills']],['AI委托成交','持仓止损','实验初始化：满仓起点'])
        self.assertEqual(a['recent_fills'][0]['price'],850.15)
        self.assertEqual(a['recent_fills'][0]['grams'],1.123)

    def test_pair_uses_isolated_databases_same_controls_and_only_charges_production_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=replace(Settings(),db_path=Path(temp)/'production.db')
            store=Store(settings.db_path);AIAnalyst(settings,store,'codex','codex')
            source=frozen();key='ai_model_input:frozen:codex';store.set_state(key,source)
            calls=[]
            def generate(analyst,context,metrics):
                self.assertNotEqual(analyst.settings.db_path,settings.db_path)
                self.assertEqual(analyst.settings.ai_max_searches,0)
                self.assertEqual(analyst.settings.ai_history_max_queries,0)
                self.assertFalse(analyst.settings.codex_resume_enabled)
                with slot(settings.db_path) as acquired:self.assertFalse(acquired)
                calls.append((analyst.settings.db_path,context))
                metrics.update(input=123,output=45)
                return '',[],SimpleNamespace(model_dump=lambda **kwargs:{'diagnostic':True})
            out=Path(temp)/'private'
            try:
                before=store.conn.execute('SELECT COUNT(*) FROM ai_views').fetchone()[0]
                with patch.object(AIAnalyst,'_run_codex',generate),patch.object(AIAnalyst,'analyze',side_effect=AssertionError('publishing prohibited')):
                    report=run_pair(settings,source,key,out)
                self.assertEqual(len(report['results']),2)
                self.assertNotEqual(calls[0][0],calls[1][0])
                self.assertEqual(usage(store.conn,'codex',utc_now()),2)
                self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM ai_views').fetchone()[0],before)
                self.assertEqual([r[0] for r in store.conn.execute('SELECT status FROM ai_runs')],['diagnostic_completed']*2)
                self.assertEqual(source_context(settings.db_path,key),(key,source))
                self.assertEqual(json.loads((out/'report.json').read_text())['published'],False)
                for path in out.iterdir():self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            finally:store.conn.close()

    def test_no_call_when_resource_slot_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=replace(Settings(),db_path=Path(temp)/'production.db')
            with slot(settings.db_path),patch.object(AIAnalyst,'_run_codex') as call:
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    run_pair(settings,frozen(),'key',Path(temp)/'result')
                call.assert_not_called()

    def test_pair_requires_two_available_daily_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=replace(Settings(),db_path=Path(temp)/'production.db',ai_max_runs_per_day=1)
            store=Store(settings.db_path);AIAnalyst(settings,store,'codex','codex')
            try:
                with patch.object(AIAnalyst,'_run_codex') as call:
                    with self.assertRaisesRegex(RuntimeError,'daily Codex budget'):
                        run_pair(settings,frozen(),'key',Path(temp)/'result')
                    call.assert_not_called()
                self.assertEqual(usage(store.conn,'codex',utc_now()),0)
            finally:store.conn.close()

    def test_second_arm_failure_is_charged_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=replace(Settings(),db_path=Path(temp)/'production.db')
            store=Store(settings.db_path);AIAnalyst(settings,store,'codex','codex')
            good=('',[],SimpleNamespace(model_dump=lambda **kwargs:{'diagnostic':True}))
            try:
                with patch.object(AIAnalyst,'_run_codex',side_effect=[good,RuntimeError('offline')]):
                    with self.assertRaisesRegex(RuntimeError,'offline'):
                        run_pair(settings,frozen(),'key',Path(temp)/'result')
                self.assertEqual(usage(store.conn,'codex',utc_now()),2)
                self.assertEqual([r[0] for r in store.conn.execute('SELECT status FROM ai_runs')],['diagnostic_completed','diagnostic_failed'])
                self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM ai_views').fetchone()[0],0)
                self.assertTrue((Path(temp)/'result'/'shadow.trace.jsonl').exists())
            finally:store.conn.close()


if __name__=='__main__':unittest.main()
