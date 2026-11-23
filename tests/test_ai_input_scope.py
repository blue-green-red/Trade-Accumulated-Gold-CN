import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
from monitor.ai_inputs import compact_context, market_context
import test_four_account as fixtures


class InputScopeTests(unittest.TestCase):
    def test_live_entry_payoff_uses_same_bank_input_quote_and_its_fee(self):
        confirmed = '2026-11-23T02:00:00+00:00'
        quote = {'status': 'fresh', 'buy_price_cny': 806, 'sell_price_cny': 805,
                 'quoted_at': '2026-11-23T02:01:00+00:00'}
        review = {'grams': 12, 'recovery_price_cny': 803, 'max_buy_price_cny': 807,
                  'zone': {'weak_target_cny': 812, 'risk_exit_cny': 800}}
        route = {'condition_key': 'pullback', 'confirmed_at': confirmed, 'price_cny': 804, 'buy_review': review}
        automatic = {'condition_key': 'continuation', 'confirmed_at': confirmed,
                     'price_cny': 805, 'threshold_cny': 805, 'bank_breakout': {'hold_minutes': 3}}
        event = {'id': 'live-buy-confirmed:codex:zheshang:test',
                 'data': {'bank': 'zheshang', 'routes': [route, automatic]}}
        account = {'status': 'fresh', 'quote': quote, 'sell_fee_rate': .005, 'total_grams': 42}
        original = {'live_readonly_accounts': {'accounts': {'zheshang': account,
                    'minsheng': {**account, 'sell_fee_rate': .01}}}, 'trigger_events': [event]}
        result = compact_context(original)
        by_bank = result['live_readonly_accounts']['accounts']
        payoffs = by_bank['zheshang']['entry_review_payoffs_at_input']
        self.assertEqual(payoffs['pullback'], {
            'weak_target_net_cny': round(12 * (812 * .995 - 806), 2),
            'risk_exit_net_cny': -120.0, 'hold_only_at_exit_delta_cny': -208.95,
            'hold_plus_candidate_at_exit_delta_cny': -328.95})
        self.assertEqual(payoffs['continuation'], {'at_breakout_level_net_cny_per_gram': round(805 * .995 - 806, 2)})
        self.assertNotIn('entry_review_payoffs_at_input', by_bank['minsheng'])
        self.assertNotIn('entry_review_payoffs_at_input', account)
        self.assertEqual(compact_context(result), result)

        for changes in ({'status': 'historical'}, {'buy_price_cny': 802},
                        {'quoted_at': '2026-11-23T01:59:00+00:00'}):
            with self.subTest(changes=changes):
                changed = deepcopy(original)
                changed['live_readonly_accounts']['accounts']['zheshang']['quote'].update(changes)
                info = market_context(changed)['live_readonly_accounts']['accounts']['zheshang']
                self.assertNotIn('entry_review_payoffs_at_input', info)
        changed = deepcopy(original)
        changed['live_readonly_accounts']['accounts']['zheshang']['quote']['buy_price_cny'] = 808
        latest = market_context(changed)['live_readonly_accounts']['accounts']['zheshang']['entry_review_payoffs_at_input']
        self.assertEqual(set(latest), {'continuation'})
        for holding, expected in ((0, -120.0), (None, None)):
            with self.subTest(holding=holding):
                account['total_grams'] = holding
                info = market_context(original)['live_readonly_accounts']['accounts']['zheshang']
                payoff = info['entry_review_payoffs_at_input']['pullback']
                self.assertEqual(payoff.get('hold_plus_candidate_at_exit_delta_cny'), expected)
                self.assertEqual(payoff['risk_exit_net_cny'], -120.0)
        account['sell_fee_rate'] = None
        info = market_context(original)['live_readonly_accounts']['accounts']['zheshang']
        self.assertNotIn('entry_review_payoffs_at_input', info)

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
