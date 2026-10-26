import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
from monitor.ai_inputs import market_context
import test_four_account as fixtures


class InputScopeTests(unittest.TestCase):
    def test_wave_signals_and_levels_removed_without_changing_accounts_or_snapshot(self):
        original={'trading_accounts':{'accounts':{'short_full':{'cash':123.456,'grams':9.87,'stop_price':800,'last_plan':{'decision':{'limit_price_cny':850}}}}},
          'signals_tradingview':{'30m':'买入','1H':'中性','4H':'卖出','日线':'买入','周线':'中性'},
          'levels_by_code':{'support':[800],'intraday_pivot':850},'day_high':900,'day_low':800,
          'jd_accumulation_quote':{'price':850},'trigger_events':[{'id':'event','data':{'actual':0}}]}
        before=deepcopy(original);value=market_context(original)
        self.assertEqual(original,before)
        self.assertEqual(value['signals_tradingview'],{'日线':'买入','周线':'中性'})
        self.assertNotIn('levels_by_code',value)
        for key in ('trading_accounts','day_high','day_low','jd_accumulation_quote','trigger_events'):
            self.assertEqual(value[key],original[key])

    def test_jd_market_evidence_retained_and_operations_fields_removed(self):
        original={'jd_research':{'source':'京东','url':'https://example.test','quota':{'remaining':0},
          'funds':{'status':'fresh','published_at':'date','fetched_at':'fetch','long_pct':0,
                   'attempted_at':'attempt','refresh_status':'rate_limited','retry_at':'retry','error_type':'ProviderRateLimit'},
          'orders':{'status':'budget_exhausted','published_at':'date','retry_at':'retry','error_type':'LocalDailyBudget'}}}
        value=market_context(original)['jd_research']
        self.assertNotIn('quota',value)
        self.assertEqual(value['funds'],{'status':'fresh','published_at':'date','fetched_at':'fetch','long_pct':0})
        self.assertEqual(value['orders'],{'status':'budget_exhausted','published_at':'date'})
        self.assertIn('retry_at',original['jd_research']['orders'])

    def test_optimize_off_still_filters_actual_model_input(self):
        fixture=fixtures.FourAccountTests();fixture.setUp()
        try:
            fixture.a.settings=replace(fixture.settings,ai_optimize=False)
            fixture.a._run_codex=Mock(return_value=('notes',[],fixture.value()))
            context=fixture.context();context.update(signals_tradingview={'1H':'买入','日线':'中性'},levels_by_code={'support':[800]})
            result=fixture.a.analyze(context,'test',run_id='input-scope')
            self.assertIsNotNone(result)
            sent=fixture.a._run_codex.call_args.args[0]
            self.assertEqual(sent['signals_tradingview'],{'日线':'中性'})
            self.assertNotIn('levels_by_code',sent)
            saved=fixture.m.store.get_state('ai_model_input:input-scope:codex')
            self.assertEqual(saved['signals_tradingview'],sent['signals_tradingview'])
            self.assertIn('levels_by_code',context)
        finally:fixture.tearDown()
