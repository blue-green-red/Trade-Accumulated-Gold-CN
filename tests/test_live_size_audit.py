import unittest

from monitor.jd_live_size_audit import position_marks, position_sizes, summarize_position_sizes


def snapshot():
    return {
        'input_state': {'total_grams': 10, 'cash_cny': 95_000, 'pending_order_count': 1},
        'input_account_risk': {'net_assets_cny': 104_960, 'risk_budget_cny': 3_500},
        'input_data_status': {'account': 'fresh', 'orders_complete': True},
        'input_quote_status': 'fresh',
        'input_quote_cny': 1_000,
        'input_participation_cash_cny': 90_000,
        'input_minimum_buy_grams': 10,
        'input_pending_buy_exposure': {'grams': 5, 'cost_cny': 5_000},
        'position_path': {'boundary_price_cny': 900, 'checks': []},
    }


class PositionSizeAuditTests(unittest.TestCase):
    def test_known_fee_reserved_cash_and_risk(self):
        advice = snapshot()
        sizing = position_sizes(advice, {}, 1_000, .004, {})
        sizes = sizing['targets_pct']
        self.assertEqual(sizes['5']['status'], 'at_or_above_target')
        self.assertEqual(sizes['10']['status'], 'below_bank_minimum')
        self.assertEqual(sizes['20']['additional_grams'], 11)
        self.assertEqual(sizes['20']['planned_full_position_loss_cny'], 2_653.6)
        self.assertTrue(sizes['20']['within_risk_reference'])
        self.assertFalse(sizes['40']['within_risk_reference'])
        self.assertEqual(sizes['100']['status'], 'insufficient_input_cash')
        advice['input_participation_cash_cny'] = 31_000
        self.assertEqual(position_sizes(advice, {}, 1_000, .004, {})['targets_pct']['40']['status'],
                         'insufficient_input_cash')
        self.assertEqual(position_sizes(advice, {}, 1_000, None, {})['status'], 'cash_holdings_or_fee_unknown')
        del advice['input_pending_buy_exposure']
        size = position_sizes(advice, {}, 1_000, .004, {})['targets_pct']['20']
        self.assertEqual(size['status'], 'fundable')
        self.assertNotIn('within_risk_reference', size)

    def test_only_matched_horizons_count_and_sampling_gaps_stay_unknown(self):
        sizing = position_sizes(snapshot(), {}, 1_000, .004, {})
        good = position_marks(sizing, {'status': 'valued', 'net_cny_per_gram': 2.375, 'gross_cny_per_gram': 6.5,
                                       'sampled_adverse_excursion_cny_per_gram': None})
        bad = position_marks(sizing, {'status': 'valued', 'net_cny_per_gram': -3, 'gross_cny_per_gram': 1.1,
                                      'sampled_adverse_excursion_cny_per_gram': 5})
        self.assertEqual(good['20']['additional_net_cny'], 26.12)
        self.assertIsNone(good['20']['sampled_additional_adverse_cny'])
        self.assertEqual(bad['40']['sampled_additional_adverse_cny'], 160)
        paths = [{'branch': 'breakout_continuation', 'position_size_at_publication': sizing,
                  'outcome': 'no_buy_suggested',
                  'quote_followups': [{'minutes': 60, 'position_size_marks': marks}],
                  'trading_day_followups': []} for marks in (good, bad, {'20': good['20']})]
        result = summarize_position_sizes(paths)['breakout_continuation']['60m']['pairs']['20_to_40']
        self.assertEqual(result['paired_samples'], 2)
        self.assertEqual(result['larger_earned_more'], 1)
        self.assertEqual(result['larger_lost_more'], 1)
        self.assertEqual(result['gross_up_but_net_unprofitable'], 1)
        self.assertEqual(result['no_buy_then_net_profitable'], 1)
        self.assertEqual(result['larger_within_risk_reference'], 0)
        self.assertEqual(result['complete_adverse_samples'], 1)
        self.assertEqual(result['mean_extra_adverse_cny'], 105)
        paths[0]['trading_day_followups'] = [
            {'trading_days': 1, 'status': 'horizon_quote_missing',
             'delayed_quote_sensitivity': {'trading_days': 1, 'status': 'observed', 'position_size_marks': good}},
            {'trading_days': 3, 'status': 'pending',
             'delayed_quote_sensitivity': {'trading_days': 3, 'status': 'pending'}},
        ]
        branch = 'breakout_continuation'
        strict = summarize_position_sizes(paths)[branch]
        delayed = summarize_position_sizes(paths, delayed=True)[branch]
        self.assertEqual(strict['1d']['pairs']['20_to_40']['paired_samples'], 0)
        self.assertEqual(delayed['1d']['pairs']['20_to_40']['no_buy_then_net_profitable'], 1)
        self.assertEqual(delayed['3d']['quote_status_counts'], {'pending': 1})
