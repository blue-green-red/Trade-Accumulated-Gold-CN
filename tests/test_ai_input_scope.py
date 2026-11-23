import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
from monitor.ai_inputs import compact_context, market_context
import test_four_account as fixtures


class InputScopeTests(unittest.TestCase):
    def test_plain_buy_touch_uses_archived_direction_and_current_bank_quote(self):
        from contextlib import closing
        import json
        import sqlite3
        from monitor.jd_live_conditions import condition_key
        from monitor.jd_live_readonly import ensure_schema

        touched='2026-11-23T02:00:00+00:00'
        low={'condition':'at_or_below','quote_side':'buy','price_cny':800}
        high={**low,'condition':'at_or_above'}
        data={'bank':'zheshang','advice_id':1,'condition_key':condition_key(low),'quote_side':'buy',
              'price_cny':800,'threshold_cny':800,'quoted_at':touched}
        event={'id':'live-price:codex:zheshang:1:low','data':data}
        quote={'status':'fresh','buy_price_cny':801,'quoted_at':'2026-11-23T02:01:00+00:00'}
        source={'status':'fresh','quote':quote}
        original={'trigger_events':[event],'live_readonly_accounts':{'accounts':{'zheshang':source}}}
        with closing(sqlite3.connect(':memory:')) as conn:
            ensure_schema(conn)
            conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                         (1,'zheshang','codex',1,touched,'2026-11-23T02:15:00+00:00','rev',
                          json.dumps({'review_conditions':[high,low]})))
            packed=compact_context(original,conn)
            observed=packed['trigger_events'][0]['data']
            self.assertEqual(observed['condition'],'at_or_below')
            self.assertFalse(observed['threshold_met_at_input'])
            self.assertEqual(observed['price_cny'],800)
            self.assertEqual(compact_context(packed),packed)
            self.assertNotIn('condition',data)
            quote['buy_price_cny']=799
            self.assertTrue(compact_context(original,conn)['trigger_events'][0]['data']['threshold_met_at_input'])
            quote['quoted_at']='2026-11-23T01:59:59+00:00'
            self.assertNotIn('threshold_met_at_input',compact_context(original,conn)['trigger_events'][0]['data'])
            data['threshold_cny']=802
            self.assertNotIn('condition',compact_context(original,conn)['trigger_events'][0]['data'])

    def test_duplicate_legacy_move_records_trimmed_without_losing_coverage_or_directional_events(self):
        legacy = [{'id': f'old-{i}', 'reason': '30分钟异动', 'data': {}} for i in range(3)]
        new = [{'id': f'market-move:{side}:test', 'reason': '30分钟异动',
                'data': {'change_pct': pct}} for side, pct in (('up', 1), ('down', -1))]
        context = {'trigger_events': legacy + new, 'event_coverage': {'codex': [e['id'] for e in legacy + new]}}
        result = compact_context(context)
        self.assertEqual(result['trigger_events'], legacy[:1] + new)
        self.assertEqual(result['event_coverage'], context['event_coverage'])
        self.assertEqual(compact_context(result), result)

    def test_live_waiting_comparison_retains_returns_without_repeated_cash_examples(self):
        full = {'status': 'ready', 'reference_amounts_cny': [1000, 5000],
                'up_delta_cny': [15, 75], 'down_delta_cny': [-25, -125],
                'waiting': {'reference_amount_cny': 1000, 'buy_hold_delta_cny': -8.42,
                            'quote_change_pct': -0.442, 'observed_minutes': 35.2, 'rounds': 3}}
        account = {'cash_cny': 50_000, 'participation_review': {'status': 'ready', 'up_delta_cny': [15, 75]},
                   'previous_advice': [{'strategy_review': {'participation_review': full, 'weakened_rounds': 2}}]}
        source = {'live_readonly_accounts': {'accounts': {'zheshang': account}}}
        packed = compact_context(source)
        live = packed['live_readonly_accounts']['accounts']['zheshang']
        self.assertNotIn('participation_review', live)
        self.assertEqual(live['cash_cny'], 50_000)
        strategy = live['previous_advice'][0]['strategy_review']
        self.assertEqual(strategy['weakened_rounds'], 2)
        self.assertEqual(strategy['participation_review'], {'waiting': {
            'quote_change_pct': -0.442, 'observed_minutes': 35.2, 'rounds': 3, 'net_return_pct': -0.842}})
        self.assertEqual(compact_context(packed), packed)
        self.assertIn('reference_amount_cny', full['waiting'])
        simulated = compact_context({**source, 'trading_accounts': {'accounts': {'paper': {}}}})
        self.assertEqual(simulated['live_readonly_accounts']['accounts']['zheshang']['previous_advice'],
                         account['previous_advice'])

    def test_previous_shallow_pullback_compares_to_current_sampled_bank_low(self):
        as_of = '2026-11-23T02:00:00+00:00'
        condition = {'condition': 'at_or_below', 'quote_side': 'buy', 'price_cny': 792.4,
                     'buy_review': {'zone': {'floor_cny': 790}}}
        account = {
            'status': 'fresh', 'quote': {'status': 'fresh', 'quoted_at': as_of},
            'recent_bank_quotes': {'as_of': as_of, 'status': 'available',
                                   'buy_price_windows_15m': [{'low': 802}, {'low': 800}]},
            'previous_advice': [{'review_conditions': [condition, {**condition, 'price_cny': 801},
                                                      {**condition, 'condition': 'at_or_above'}]}],
        }
        original = {'live_readonly_accounts': {'accounts': {'zheshang': account}}}
        packed = compact_context(original)
        field = 'trigger_below_sampled_1h_low_cny_per_gram'
        readout = packed['live_readonly_accounts']['accounts']['zheshang']
        shallow, inside, breakout = readout['previous_advice'][0]['review_conditions']
        self.assertEqual(shallow[field], 7.6)
        self.assertNotIn(field, inside)
        self.assertNotIn(field, breakout)
        self.assertNotIn(field, condition)
        self.assertEqual(compact_context(packed), packed)
        readout['quote']['quoted_at'] = '2026-11-23T02:01:00+00:00'
        updated = compact_context(packed)['live_readonly_accounts']['accounts']['zheshang']
        self.assertNotIn(field, updated['previous_advice'][0]['review_conditions'][0])

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
        account = {'status': 'fresh', 'quote': quote, 'sell_fee_rate': .005, 'total_grams': 42,
                   'recent_bank_quotes': {'as_of': quote['quoted_at'], 'earlier_sampled_ranges': [
                       {'to': '2026-11-23T01:00:00+00:00', 'sell_high_cny': 811.5},
                       {'to': '2026-11-23T02:05:00+00:00', 'sell_high_cny': 890},
                   ]}}
        original = {'live_readonly_accounts': {'accounts': {'zheshang': account,
                    'minsheng': {**account, 'sell_fee_rate': .01}}}, 'trigger_events': [event]}
        result = compact_context(original)
        by_bank = result['live_readonly_accounts']['accounts']
        payoffs = by_bank['zheshang']['entry_review_payoffs_at_input']
        self.assertEqual(payoffs['pullback'], {
            'weak_target_net_cny_per_gram': round(812 * .995 - 806, 4),
            'risk_exit_net_cny_per_gram': -10.0, 'hold_only_at_exit_delta_cny': -208.95})
        self.assertEqual(payoffs['continuation'], {'at_breakout_level_net_cny_per_gram': round(805 * .995 - 806, 2),
                                                   'earlier_sampled_sell_high_cny': 811.5,
                                                   'at_earlier_sell_high_net_cny_per_gram': round(811.5 * .995 - 806, 2)})
        self.assertNotIn('entry_review_payoffs_at_input', by_bank['minsheng'])
        self.assertNotIn('entry_review_payoffs_at_input', account)
        self.assertEqual(compact_context(result), result)
        review['grams'] = 69
        resized = compact_context(original)['live_readonly_accounts']['accounts']['zheshang']
        self.assertEqual(resized['entry_review_payoffs_at_input'], payoffs)

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
        changed['live_readonly_accounts']['accounts']['zheshang']['quote']['buy_price_cny'] = 812
        above = market_context(changed)['live_readonly_accounts']['accounts']['zheshang']['entry_review_payoffs_at_input']
        self.assertNotIn('earlier_sampled_sell_high_cny', above['continuation'])
        changed['live_readonly_accounts']['accounts']['zheshang']['quote']['buy_price_cny'] = 806
        changed['live_readonly_accounts']['accounts']['zheshang']['recent_bank_quotes']['as_of'] = confirmed
        stale = market_context(changed)['live_readonly_accounts']['accounts']['zheshang']['entry_review_payoffs_at_input']
        self.assertNotIn('earlier_sampled_sell_high_cny', stale['continuation'])
        for holding, expected in ((0, 0.0), (None, None)):
            with self.subTest(holding=holding):
                account['total_grams'] = holding
                info = market_context(original)['live_readonly_accounts']['accounts']['zheshang']
                payoff = info['entry_review_payoffs_at_input']['pullback']
                self.assertEqual(payoff.get('hold_only_at_exit_delta_cny'), expected)
                self.assertEqual(payoff['risk_exit_net_cny_per_gram'], -10.0)
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
