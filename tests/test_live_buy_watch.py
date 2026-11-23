import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from monitor.ai_events import acknowledge, enqueue, read
from monitor.jd_live_buy_audit import (evaluate, first_observed_quote, followup_quote, independent_branch_replay,
                                       observed_review_horizons, observed_reviews)
from monitor.jd_live_buy_guard import remember_reviewed_confirmations, remove_submitted_confirmations
from monitor.jd_live_buy_watch import advance, pending_events, quote_context, record, request, set_watch, valid_route
from monitor.jd_live_conditions import condition_key, condition_state, monitor_prices, validate_structure
from monitor.jd_live_expiry import request_expiry_review
from monitor.jd_live_readonly import ensure_schema
from monitor.store import Store

START = datetime(2026, 11, 16, 2, tzinfo=timezone.utc)


def route(direction='at_or_below'):
    return {'condition': direction, 'price_cny': 800 if direction == 'at_or_below' else 805,
            'quote_side': 'buy', 'buy_review': {'grams': 10, 'recovery_price_cny': 803,
                                                 'hold_minutes': 2, 'max_buy_price_cny': 808}}


def point(minute, price):
    at = START + timedelta(minutes=minute)
    return at, at + timedelta(seconds=10), price, price


def zoned_route(direction='at_or_below'):
    condition = route(direction)
    condition['buy_review'].update(max_buy_price_cny=804, zone={
        'floor_cny': 798, 'weak_target_cny': 818, 'risk_exit_cny': 797, 'basis': '本行15分钟报价区间。'})
    return condition


class BuyWatchTests(unittest.TestCase):
    def advance_all(self, quotes, direction='at_or_below', condition=None):
        state = {}
        condition = condition or route(direction)
        for minute, price in quotes:
            state = advance(state, condition, point(minute, price)[0], price)
        return state.get('stage')

    def test_dip_requires_recovery_and_sustained_distinct_quotes(self):
        self.assertEqual(self.advance_all([(0, 799), (1, 803), (1, 803)]), 'holding')
        self.assertEqual(self.advance_all([(0, 799), (1, 803), (2, 801), (3, 803), (4, 804)]), 'holding')
        self.assertEqual(self.advance_all([(0, 799), (1, 803), (2, 804), (3, 804)]), 'confirmed')
        self.assertIsNone(self.advance_all([(0, 799), (1, 803), (5, 804)]))
        self.assertEqual(self.advance_all([(0, 799), (1, 809), (2, 804), (4, 804)]), 'above_cap')

    def test_breakout_must_retest_even_if_price_stays_above_trigger(self):
        self.assertEqual(self.advance_all([(0, 805), (1, 807), (3, 807)], 'at_or_above'), 'touched')
        self.assertEqual(self.advance_all([(0, 805), (1, 807), (2, 806), (4, 806)], 'at_or_above'), 'touched')
        self.assertEqual(self.advance_all([(0, 805), (1, 807), (2, 804), (4, 804)], 'at_or_above'), 'confirmed')

    def test_shallow_range_recovers_without_testing_its_floor(self):
        condition = zoned_route()
        self.assertEqual(self.advance_all([(0, 804), (1, 799.7), (2, 803), (4, 804)],
                                         condition=condition), 'confirmed')
        self.assertEqual(self.advance_all([(0, 804), (1, 797), (2, 803), (4, 804)],
                                         condition=condition), 'pullback_lost')

    def test_upper_edge_can_exceed_the_recovery_price_and_entry_cap(self):
        condition = zoned_route()
        condition['price_cny'] = 805
        self.assertTrue(valid_route(condition))
        self.assertEqual(self.advance_all([(0, 809), (1, 805), (2, 804), (4, 803)],
                                         condition=condition), 'touched')
        self.assertEqual(self.advance_all([(0, 809), (1, 805), (2, 802), (3, 803), (5, 804)],
                                         condition=condition), 'confirmed')
        source = {'sell_fee_rate': .004, 'recent_bank_quotes': {'status': 'available'}}
        value = {'action': 'wait', 'order_management': [], 'review_conditions': [condition], 'valid_for_minutes': 15}
        self.assertEqual(validate_structure(value, source, START), [])

    def test_breakout_above_entry_cap_can_retest_and_confirm(self):
        condition = zoned_route('at_or_above')
        self.assertTrue(valid_route(condition))
        self.assertEqual(self.advance_all([(0, 809), (1, 805), (2, 803), (4, 804)],
                                         condition=condition), 'confirmed')
        self.assertEqual(self.advance_all([(0, 809), (1, 797), (2, 803), (4, 804)],
                                         condition=condition), 'pullback_lost')

    def test_bank_high_requires_breakout_then_a_held_retest(self):
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                     'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
        self.assertIsNone(self.advance_all([(0, 805), (3, 805)], condition=condition))
        self.assertEqual(self.advance_all([(0, 807), (2, 806.5), (4, 807)], condition=condition), 'touched')
        self.assertEqual(self.advance_all([(0, 807), (2, 805.5), (4, 806), (5, 806.5)],
                                         condition=condition), 'confirmed')
        self.assertEqual(self.advance_all([(0, 807), (2, 804.99)], condition=condition), 'breakout_failed')
        self.assertEqual(self.advance_all([(0, 807), (2, 804.99), (4, 806), (5, 806.5)],
                                         condition=condition), 'touched')

    def test_bank_high_continuation_confirms_below_the_periodic_threshold(self):
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                     'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
        rising = [(0, 805.4), (1, 806.0), (2, 806.3), (3, 806.5)]
        self.assertLess(806.5 / 805 - 1, .004)
        self.assertEqual(self.advance_all(rising[:-1], condition=condition), 'touched')
        self.assertEqual(self.advance_all(rising, condition=condition), 'confirmed')
        self.assertEqual(self.advance_all([(0, 805.4), (0, 805.4), (3, 806.5)],
                                         condition=condition), 'touched')
        self.assertEqual(self.advance_all([(0, 805.4), (1, 806.5), (3, 806.5), (4, 806.5)],
                                         condition=condition), 'touched')
        self.assertEqual(self.advance_all([(0, 805.4), (5, 806.1), (6, 806.5)],
                                         condition=condition), 'touched')

    def test_continuation_can_confirm_after_an_above_threshold_retest(self):
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                     'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
        prices = [(0, 806.4), (1, 805.8), (2, 806.1), (3, 806.7)]
        self.assertEqual(self.advance_all(prices, condition=condition), 'confirmed')
        prices[1] = (1, 805)
        self.assertEqual(self.advance_all(prices, condition=condition), 'holding')

    def test_bank_watch_can_rearm_after_a_failed_confirmation_without_duplicate_analysis(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            expires = (START + timedelta(minutes=30)).isoformat()
            condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                         'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
            with store.conn:
                store.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                                   ('zheshang', 'codex', 1, START.isoformat(), expires, 'v1', json.dumps({})))
            advice = {'id': 1, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                      'created_at': START.isoformat(), 'expires_at': expires, 'review_conditions': [condition]}
            account = {'bank': 'zheshang', 'connected': True}

            def observe(stream):
                for minute, price in stream:
                    quote = point(minute, price)
                    request(store, account, advice, quote[1], quote)
                return read(store.conn, 'codex')['pending']

            first = observe([(0, 806.4), (1, 806.5), (3, 806.7)])[0]
            self.assertEqual(first['data']['routes'][0]['confirmation_mode'], 'continuation')
            self.assertEqual(observe([(4, 804.9)]), [])
            second = observe([(5, 806.4), (6, 806.6), (8, 806.8)])[0]
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(second['data']['routes'][0]['touched_at'], point(5, 806.4)[0].isoformat(timespec='microseconds'))
            with store.conn:
                acknowledge(store.conn, 'codex', [second['id']])
            self.assertEqual(observe([(9, 804.9), (10, 806.3), (11, 806.5), (13, 806.9)]), [])

    def test_continuous_quotes_keep_confirmation_but_a_gap_removes_the_pending_event(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            expires = (START + timedelta(minutes=30)).isoformat()
            condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                         'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
            with store.conn:
                store.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                                   ('zheshang', 'codex', 1, START.isoformat(), expires, 'v1', json.dumps({})))
            advice = {'id': 1, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                      'created_at': START.isoformat(), 'expires_at': expires, 'review_conditions': [condition]}
            account = {'bank': 'zheshang', 'connected': True}
            for minute, price in [(0, 806.1), (1, 806.2), (3, 806.4), (5, 806.4), (7, 806.4)]:
                quote = point(minute, price)
                request(store, account, advice, quote[1], quote)
                self.assertEqual(len(read(store.conn, 'codex')['pending']), int(minute >= 3))
            quote = point(11, 806.4)
            request(store, account, advice, quote[1], quote)
            self.assertEqual(read(store.conn, 'codex')['pending'], [])

    def test_program_breakout_can_coexist_with_a_buy_retest_at_the_same_level(self):
        with TemporaryDirectory() as tmp:
            with Store(Path(tmp) / 'test.db').conn as conn:
                self.addCleanup(conn.close)
                ensure_schema(conn)
                source = {'status': 'fresh', 'revision': 'v1', 'quote': {'status': 'fresh'},
                          'recent_bank_quotes': {'status': 'available', 'buy_price_windows_15m': [{'low': 801, 'high': 805}]}}
                advice = {'action': 'wait', 'valid_for_minutes': 15,
                          'review_conditions': [zoned_route('at_or_above')]}
                set_watch(conn, 'zheshang', 'codex', advice, source, START, 10)
        conditions = advice['review_conditions']
        self.assertEqual(len(conditions), 2)
        self.assertNotEqual(condition_key(conditions[0]), condition_key(conditions[1]))
        self.assertTrue(conditions[1]['bank_breakout']['companion'])

    def test_full_model_conditions_leave_room_for_an_early_continuation_review(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            source = {'status': 'fresh', 'revision': 'v1', 'quote': {'status': 'fresh'},
                      'recent_bank_quotes': {'status': 'available', 'buy_price_windows_15m': [{'low': 801, 'high': 805}]}}
            model_conditions = [zoned_route(), zoned_route('at_or_above'),
                                {'condition': 'at_or_above', 'quote_side': 'sell', 'price_cny': 815},
                                {'condition': 'at_or_below', 'quote_side': 'sell', 'price_cny': 795}]
            deadline = (START + timedelta(minutes=15)).isoformat()
            advice = {'id': 1, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                      'snapshot_revision': 'v1', 'created_at': START.isoformat(), 'expires_at': deadline,
                      'action': 'hold', 'valid_for_minutes': 15, 'review_conditions': list(model_conditions),
                      'position_path': {'boundary_price_cny': 799}}
            with store.conn:
                set_watch(store.conn, 'zheshang', 'codex', advice, source, START, 10)
                store.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                                   (1, 'zheshang', 'codex', 1, START.isoformat(), deadline, 'v1', json.dumps(advice)))
            self.assertEqual(advice['review_conditions'][:-1], model_conditions)
            self.assertEqual(advice['review_conditions'][-1]['price_cny'], 805)
            account = {'bank': 'zheshang', 'connected': True, 'revision': 'v1', 'quote': {'sell_price_cny': 805}}
            for minute, price in [(0, 805.5), (1, 805.9), (3, 806.4), (4, 806.5)]:
                quote = point(minute, price)
                request(store, account, advice, quote[1], quote)
                events = read(store.conn, 'codex')['pending']
                self.assertEqual(len(events), int(minute >= 3))
            self.assertLess(806.4 / 805 - 1, .004)
            self.assertEqual(events[0]['data']['routes'][0]['confirmation_mode'], 'continuation')
            self.assertEqual(events[0]['data']['routes'][0]['position_boundary_cny'], 799)
            self.assertEqual(events[0]['min_gap_minutes'], 0)
            self.assertEqual(advice['action'], 'hold')

    def test_watch_survives_action_expiry_and_an_unchanged_advice(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            source = {'status': 'fresh', 'revision': 'v1', 'quote': {'status': 'fresh'},
                      'recent_bank_quotes': {'status': 'available', 'buy_price_windows_15m': [{'low': 801, 'high': 805}]}}
            account = {'bank': 'zheshang', 'connected': True, 'revision': 'v1'}

            def publish(id, minute):
                now = point(minute, 801)[0]
                deadline = now + timedelta(minutes=15)
                value = {'action': 'wait', 'valid_for_minutes': 15, 'review_conditions': [zoned_route()]}
                with store.conn:
                    set_watch(store.conn, 'zheshang', 'codex', value, source, now, 10)
                    store.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                                       (id, 'zheshang', 'codex', id, now.isoformat(), deadline.isoformat(),
                                        'v1', json.dumps(value)))
                return {**value, 'id': id, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                        'snapshot_revision': 'v1', 'created_at': now.isoformat(), 'expires_at': deadline.isoformat()}

            first = publish(1, 0)
            first.update(status='expired', action_status='expired')
            quote = point(16, 799)
            account['quote'] = {'status': 'fresh', 'quoted_at': quote[0].isoformat(), 'buy_price_cny': 799}
            request(store, account, first, quote[1], quote)
            observed = condition_state(store.conn, account, first, first['review_conditions'][0], quote[1])
            self.assertEqual((observed['status'], observed['confirmation_stage']), ('monitoring', 'touched'))
            second = publish(2, 17)
            self.assertEqual(second['buy_watch'], first['buy_watch'])
            for minute in (17, 18, 19):
                quote = point(minute, 804)
                request(store, account, second, quote[1], quote)
            pending = read(store.conn, 'codex')['pending']
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['data']['plan_id'], first['buy_watch']['id'])
            with store.conn:
                acknowledge(store.conn, 'codex', [pending[0]['id']])
            third = publish(3, 20)
            self.assertEqual(third['buy_watch'], first['buy_watch'])
            for minute in (20, 21, 23):
                quote = point(minute, 804)
                request(store, account, third, quote[1], quote)
            self.assertEqual(read(store.conn, 'codex')['pending'], [])

    def test_observation_expiry_suppresses_the_legacy_advice_expiry(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            action_deadline = START + timedelta(minutes=15)
            watch_deadline = START + timedelta(hours=1)
            advice = {'id': 1, 'analyst': 'codex', 'action': 'wait', 'schema_version': 6, 'status': 'expired',
                      'created_at': START.isoformat(), 'snapshot_revision': 'v1',
                      'buy_watch': {'id': 'one-plan', 'expires_at': watch_deadline.isoformat()}}
            with store.conn:
                store.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                                   ('zheshang', 'codex', 1, START.isoformat(), action_deadline.isoformat(),
                                    'v1', json.dumps(advice)))
            account = {'bank': 'zheshang', 'revision': 'v1'}
            request_expiry_review(store, account, advice, action_deadline)
            self.assertEqual(read(store.conn, 'codex')['pending'], [])
            enqueue(store.conn, 'codex', '旧版建议到期', 0, 'live-expired:legacy', {'advice_id': 1})
            self.assertEqual(pending_events(store.conn, 'codex', action_deadline), [])
            request_expiry_review(store, account, advice, watch_deadline)
            request_expiry_review(store, account, advice, watch_deadline)
            events = read(store.conn, 'codex')['pending']
            self.assertEqual([e['id'] for e in events], ['live-watch-expired:codex:zheshang:one-plan'])

    def test_same_plan_reopens_after_expiry_without_another_timer_review(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            source = {'status': 'fresh', 'revision': 'v1', 'quote': {'status': 'fresh'},
                      'recent_bank_quotes': {'status': 'available', 'buy_price_windows_15m': [{'low': 801, 'high': 805}]}}
            account = {'bank': 'zheshang', 'connected': True, 'revision': 'v1'}

            def publish(advice_id, now):
                deadline = (now + timedelta(minutes=15)).isoformat()
                advice = {'id': advice_id, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                          'snapshot_revision': 'v1', 'created_at': now.isoformat(), 'expires_at': deadline,
                          'action': 'hold', 'schema_version': 6, 'valid_for_minutes': 15, 'review_conditions': []}
                with store.conn:
                    set_watch(store.conn, 'zheshang', 'codex', advice, source, now, 10)
                    store.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                        (advice_id, 'zheshang', 'codex', advice_id, now.isoformat(), deadline, 'v1', json.dumps(advice)))
                return advice

            first = publish(1, START)
            expiry = START + timedelta(hours=1)
            request_expiry_review(store, account, first, expiry)
            expiry_event = read(store.conn, 'codex')['pending'][0]['id']
            renewed = publish(2, expiry + timedelta(minutes=2))
            self.assertNotEqual(first['buy_watch']['id'], renewed['buy_watch']['id'])
            self.assertEqual(first['buy_watch']['signature'], renewed['buy_watch']['signature'])
            with store.conn:
                acknowledge(store.conn, 'codex', [expiry_event])
            for minute, price in [(62, 805.5), (63, 806), (65, 806.5)]:
                quote = point(minute, price)
                request(store, account, renewed, quote[1], quote)
            events = read(store.conn, 'codex')['pending']
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['data']['routes'][0]['confirmation_mode'], 'continuation')
            self.assertEqual(events[0]['data']['plan_id'], renewed['buy_watch']['id'])
            with store.conn:
                acknowledge(store.conn, 'codex', [events[0]['id']])
            request_expiry_review(store, account, renewed, expiry + timedelta(hours=1, minutes=2))
            self.assertEqual(read(store.conn, 'codex')['pending'], [])

    def test_new_route_requires_recent_bank_context_and_calculates_with_fee(self):
        condition = zoned_route('at_or_above')
        source = {'sell_fee_rate': .005, 'recent_bank_quotes': {'status': 'available'}}
        value = {'action': 'wait', 'order_management': [], 'review_conditions': [condition], 'valid_for_minutes': 15}
        self.assertEqual(validate_structure(value, source, START, minimum_buy_grams=10), [])
        self.assertEqual(condition['buy_review_costs'], {'weak_target_net_cny': 99.1, 'risk_exit_net_cny': -109.85})
        value['review_conditions'] = [route()]
        self.assertTrue(validate_structure(value, source, START, minimum_buy_grams=10))

    def test_recent_context_uses_only_its_bank_and_keeps_current_verified_quote(self):
        with TemporaryDirectory() as tmp:
            with Store(Path(tmp) / 'test.db').conn as conn:
                self.addCleanup(conn.close)
                ensure_schema(conn)
                for minute, price in [(-60, 700), (-16, 790), (-3, 799), (-1, 802), (1, 900)]:
                    record(conn, 'zheshang', point(minute, price))
                record(conn, 'minsheng', point(-2, 765))
                quote = {'status': 'fresh', 'quoted_at': START.isoformat(), 'buy_price_cny': 804, 'sell_price_cny': 804}
                account = {'bank': 'zheshang', 'connected': True, 'quote': quote}
                result = quote_context(conn, account, START + timedelta(seconds=20))
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['recent_samples'], [[-180, 799], [-60, 802], [0, 804]])
        self.assertEqual([bucket['low'] for bucket in result['buy_price_windows_15m']], [790, 799])

    def test_live_event_is_one_per_advice_and_removed_on_an_above_cap_quote(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            expires = (START + timedelta(minutes=30)).isoformat()
            conditions = [route(), route('at_or_above')]
            with store.conn:
                store.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                                  ('zheshang', 'codex', 1, START.isoformat(), expires, 'v1', json.dumps({})))
            advice = {'id': 1, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                      'created_at': START.isoformat(), 'expires_at': expires, 'review_conditions': conditions}
            account = {'bank': 'zheshang', 'connected': True}

            for minute, price in [(0, 799), (1, 805), (2, 804), (3, 804), (4, 804)]:
                sample = point(minute, price)
                account['quote'] = {'status': 'fresh', 'quoted_at': sample[0].isoformat(), 'buy_price_cny': price}
                monitor_prices(store, account, advice, sample[1])
                record(store.conn, 'zheshang', sample)
                record(store.conn, 'zheshang', sample)
                request(store, account, advice, sample[1], sample)
                pending = read(store.conn, 'codex')['pending']
                self.assertEqual(len(pending), int(minute >= 3))
            self.assertEqual(len(pending[0]['data']['routes']), 2)
            self.assertEqual(pending[0]['min_gap_minutes'], 0)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM jd_live_quote_samples').fetchone()[0], 5)

            event = pending[0]
            submitted = {**event, 'data': {**event['data'], 'routes': event['data']['routes'][:1]}}
            with store.conn:
                remove_submitted_confirmations(store.conn, 'codex', [submitted])
            self.assertEqual([condition_state(store.conn, account, advice, c, sample[1])['status']
                              for c in conditions], ['review_required', 'queued'])
            with store.conn:
                remember_reviewed_confirmations(store.conn, 'codex', [submitted])
            self.assertEqual([condition_state(store.conn, account, advice, c, sample[1])['status']
                              for c in conditions], ['reviewed', 'queued'])

            above = point(5, 809)
            request(store, account, advice, above[1], above)
            self.assertEqual(read(store.conn, 'codex')['pending'], [])

    def test_legacy_collector_touch_cannot_schedule_a_buy_review(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            enqueue(store.conn, 'codex', '旧版触价', 0, 'live-price:legacy', {'buy_review': route()['buy_review']})
            enqueue(store.conn, 'codex', '卖出触价', 0, 'live-price:exit')
            events = pending_events(store.conn, 'codex', START)
            self.assertEqual([e['id'] for e in events], ['live-price:exit'])
            self.assertEqual(read(store.conn, 'codex')['covered'], [])


class BuyAuditTests(unittest.TestCase):
    def test_shallow_replay_uses_only_the_contemporaneous_bank_low(self):
        end = START + timedelta(minutes=8)
        now = end + timedelta(minutes=1)
        quote_at = (START - timedelta(minutes=1)).isoformat()
        value = {'input_quote_at': quote_at, 'input_quote_status': 'fresh', 'input_data_status': {'account': 'fresh'},
                 'input_participation_cash_cny': 10_000, 'input_sell_fee_rate': .005,
                 'review_conditions': [zoned_route()]}
        archived = {'status': 'available', 'as_of': quote_at, 'buy_price_windows_15m': [{'low': 805, 'high': 810}]}
        points = [point(m, p) for m, p in ((1, 804.9), (2, 808.1), (4, 808.2), (6, 808.2), (7, 808.2))]

        row, = independent_branch_replay(value, START, end, points, now, archived_quotes=archived)
        self.assertEqual(row['branch'], 'deep_pullback')
        self.assertEqual(row['confirmation']['status'], 'no_confirmation')
        shadow = row['sampled_low_sensitivity']
        self.assertEqual(shadow['status'], 'replayed')
        self.assertEqual(shadow['replay']['branch'], 'shallow_pullback')
        self.assertEqual(shadow['candidate_max_buy_cny'], 809)
        self.assertEqual(shadow['replay']['confirmation']['buy_cny_per_gram'], 808.2)
        self.assertEqual(datetime.fromisoformat(shadow['replay']['confirmation']['quoted_at']), point(4, 808.2)[0])
        self.assertEqual(shadow['payoff_10g_at_candidate_cap_cny']['risk_exit_net_cny'],
                         round(10 * (797 * .995 - 809), 2))
        self.assertEqual(value['review_conditions'], [zoned_route()])

        limited = {**value, 'limit_buy_reviews': [{'decision': 'needs_review', 'entry_price_cny': 804,
                   'grams': 10, 'risk_exit_cny': 797, 'expires_at': (START + timedelta(minutes=3)).isoformat()}]}
        row, = independent_branch_replay(limited, START, end, points, now, archived_quotes=archived)
        shadow = row['sampled_low_sensitivity']
        self.assertEqual(shadow['status'], 'replayed')
        self.assertEqual(shadow['replay']['confirmation']['status'], 'no_confirmation')

        value['review_conditions'][0]['buy_review']['zone']['weak_target_cny'] = 808
        row, = independent_branch_replay(value, START, end, points, now, archived_quotes=archived)
        self.assertEqual(row['sampled_low_sensitivity']['status'], 'shifted_zone_incompatible_with_original_terms')
        row, = independent_branch_replay(value, START, end, points, now,
                                        archived_quotes={**archived, 'as_of': now.isoformat()})
        self.assertEqual(row['branch'], 'zoned_pullback')
        self.assertEqual(row['sampled_low_sensitivity']['status'], 'archived_bank_quotes_unavailable')

    def test_replay_distinguishes_the_stages_before_a_shallow_pullback_confirms(self):
        end = START + timedelta(minutes=8)
        value = {'input_quote_status': 'fresh', 'input_data_status': {'account': 'fresh'},
                 'input_participation_cash_cny': 10_000, 'input_sell_fee_rate': .005,
                 'review_conditions': [zoned_route()]}
        for prices, phase, gap in (([803, 804, 803, 802], 'awaiting_trigger', 2),
                                   ([799, 800, 801, 802], 'touched', 0),
                                   ([799, 801, 802, 803.5], 'holding', 0),
                                   ([797, 801, 803, 804], 'pullback_lost', 0),
                                   ([799, 806, 803, 804], 'above_cap', 0)):
            with self.subTest(phase=phase):
                points = [point(i, p) for i, p in zip((1, 3, 5, 7), prices)]
                row, = independent_branch_replay(value, START, end, points, end + timedelta(minutes=1))
                self.assertEqual(row['confirmation']['status'], 'no_confirmation')
                self.assertEqual(row['observation_progress'], {'last_stage': phase,
                    'closest_trigger_gap_cny_per_gram': gap, 'quote_samples_examined': len(points)})

    def test_independent_replay_keeps_the_later_retest_and_shallow_pullback(self):
        breakout = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                    'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
        end = START + timedelta(minutes=15)
        value = {'input_quote_status': 'fresh', 'input_data_status': {'account': 'fresh'},
                 'input_participation_cash_cny': 10_000, 'input_sell_fee_rate': .005,
                 'buy_watch': {'starts_at': START.isoformat(), 'expires_at': end.isoformat()},
                 'review_conditions': [breakout, zoned_route()]}
        samples = [point(m, p) for m, p in ((0, 805.1), (1, 806.3), (2, 805.8), (3, 806.5), (4, 806.4),
                                             (5, 805.9), (6, 799), (7, 803.5), (9, 804), (1441, 812))]
        now = START + timedelta(days=2)
        original, _ = evaluate(value, START, end, samples, now)
        self.assertEqual(original['confirmation_branch'], 'breakout_continuation')
        quote_at = (START - timedelta(minutes=1)).isoformat()
        value['input_quote_at'] = quote_at
        bank_quotes = {'status': 'available', 'as_of': quote_at,
                       'buy_price_windows_15m': [{'low': 798, 'high': 805}]}
        cases = {c['branch']: c for c in independent_branch_replay(value, START, end, samples, now,
                                                                  archived_quotes=bank_quotes)}
        for branch, minute, price in (('breakout_continuation', 3, 806.5), ('breakout_retest', 5, 805.9),
                                       ('shallow_pullback', 9, 804)):
            case = cases[branch]
            self.assertEqual(datetime.fromisoformat(case['confirmation']['quoted_at']), START + timedelta(minutes=minute))
            self.assertEqual(case['confirmation']['buy_cny_per_gram'], price)
            self.assertEqual(case['horizons'][0]['confirmed_10g']['net_cny'], round(10 * (812*.995-price), 2))
        self.assertEqual(independent_branch_replay(value, START + timedelta(minutes=1), end, samples, now), [])
        empty = independent_branch_replay(value, START, end, [], now)
        self.assertTrue(all(c['confirmation']['status'] == 'confirmation_quotes_missing' for c in empty))

    def test_actual_branch_marks_both_timings_on_same_day_quote_and_drawdown(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'horizon.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            confirmed = datetime(2026, 11, 20, 18, 24, tzinfo=timezone.utc)
            published = confirmed + timedelta(minutes=1)
            monday_open = datetime(2026, 11, 23, 1, tzinfo=timezone.utc)
            for at, buy, sell in ((confirmed, 800, 800), (published, 802, 801),
                                  (confirmed + timedelta(minutes=2), 810, 810),
                                  (confirmed + timedelta(minutes=4), 798, 798),
                                  (monday_open, 803, 801)):
                record(store.conn, 'zheshang', (at, at, buy, sell))
            publication = first_observed_quote(store.conn, 'zheshang', published, published + timedelta(minutes=3))
            day, later = observed_review_horizons(store.conn, 'zheshang', confirmed, 800, .005, published,
                                                  publication, monday_open + timedelta(minutes=1))
            self.assertEqual(datetime.fromisoformat(day['target_at']), monday_open)
            self.assertEqual(day['wait']['net_cny'], 0)
            for key, entry in (('at_confirmation_10g', 800), ('after_analysis_10g', 802)):
                self.assertEqual(day[key]['net_cny'], round((801*.995-entry)*10, 2))
                self.assertEqual(day[key]['sampled_max_drawdown_cny'], round((810-798)*.995*10, 2))
            self.assertEqual(later['at_confirmation_10g']['status'], 'pending')
            missing, _ = observed_review_horizons(store.conn, 'zheshang', confirmed, 800, .005, published,
                                                   publication, monday_open - timedelta(seconds=1))
            self.assertEqual(missing['after_analysis_10g']['status'], 'pending')

    def test_intrawindow_payoff_separates_prepublication_recovery_from_available_prices(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'intrawindow.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            published = START + timedelta(minutes=2)
            for minute, buy, sell in ((0, 800, 800), (1, 806, 806), (2, 807, 804), (3, 803, 803),
                                      (6, 803, 803), (9, 802, 802), (12, 803, 803), (15, 802, 802)):
                record(store.conn, 'zheshang', (*point(minute, buy)[:3], sell))
            after = first_observed_quote(store.conn, 'zheshang', published, published + timedelta(minutes=3))
            now = START + timedelta(minutes=16)
            result = followup_quote(store.conn, 'zheshang', START, 800, 15, now,
                                    fee=.005, published=published, publication_quote=after)
            early, late = result['at_confirmation_10g'], result['after_analysis_10g']
            self.assertEqual(early['net_cny'], -20.1)
            self.assertEqual(early['sampled_best_net_cny'], 19.7)
            self.assertEqual(datetime.fromisoformat(early['sampled_first_break_even_at']), point(1, 806)[1])
            self.assertEqual(late['sampled_best_net_cny'], -70.2)
            self.assertIsNone(late['sampled_first_break_even_at'])

            with store.conn:
                store.conn.execute('DELETE FROM jd_live_quote_samples WHERE quoted_at=?',
                                   (point(6, 803)[0].isoformat(timespec='microseconds'),))
            missing = followup_quote(store.conn, 'zheshang', START, 800, 15, now,
                                     fee=.005, published=published, publication_quote=after)
            self.assertEqual(missing['at_confirmation_10g']['net_cny'], -20.1)
            self.assertIsNone(missing['at_confirmation_10g']['sampled_best_net_cny'])
            self.assertIsNone(missing['at_confirmation_10g']['sampled_first_break_even_at'])

    def test_report_counts_actual_review_branches_and_never_implies_a_fill(self):
        from monitor.ai import ensure_schema as ensure_ai_schema
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'review.db')
            self.addCleanup(store.conn.close)
            ensure_schema(store.conn)
            ensure_ai_schema(store)
            started = START + timedelta(seconds=30)
            two = START + timedelta(minutes=2)
            three = START + timedelta(minutes=3)
            bank_rule = {'hold_minutes': 3, 'retest_ceiling_cny': 807}
            paths = [
                {'condition': 'at_or_above', 'confirmation_mode': 'continuation', 'bank_breakout': bank_rule,
                 'threshold_cny': 805, 'confirmed_at': START.isoformat(), 'price_cny': 806},
                {'condition': 'at_or_below', 'buy_review': {'zone': {'floor_cny': 800}},
                 'threshold_cny': 802, 'confirmed_at': START.isoformat(), 'price_cny': 806},
            ]
            with store.conn:
                for i, action, covered in ((1, 'hold', True), (2, 'buy', False)):
                    event_id = f'live-buy-confirmed:codex:zheshang:fixture-{i}'
                    run = f'fixture-{i}'
                    store.conn.execute('INSERT INTO ai_views(ts,model,trigger,view_json,analyst) VALUES(?,?,?,?,?)',
                        (two.isoformat(), 'codex', 'test', json.dumps({'_covered_event_ids': [event_id] if covered else []}), 'codex'))
                    store.conn.execute('INSERT INTO ai_runs(analyst,started_at,trigger,run_id,status,completed_at,view_rowid) '
                        'VALUES(?,?,?,?,?,?,?)', ('codex', started.isoformat(), 'fixture', run, 'completed', two.isoformat(), i))
                    store.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)', (i, 'zheshang', 'codex', i,
                        two.isoformat(), (two + timedelta(minutes=15)).isoformat(), 'v1',
                        json.dumps({'action': action, 'input_sell_fee_rate': .005})))
                    context = {'event_coverage': {'codex': [event_id]}, 'trigger_events': [
                        {'id': event_id, 'data': {'bank': 'zheshang', 'advice_id': 3, 'routes': paths}}]}
                    if not covered:
                        context.pop('event_coverage')
                    store.conn.execute('INSERT INTO state VALUES(?,?,?)',
                        (f'ai_model_input:{run}:codex', json.dumps(context), START.isoformat()))
                original = (START - timedelta(minutes=1)).isoformat()
                store.conn.execute('INSERT INTO ai_views(ts,model,trigger,view_json,analyst,run_id) VALUES(?,?,?,?,?,?)',
                                   (original, 'codex', 'test', '{}', 'codex', 'origin'))
                store.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)', (3, 'zheshang', 'codex', 3,
                    original, two.isoformat(), 'v1', json.dumps({'input_quote_at': original})))
                evidence = {'status': 'available', 'as_of': original,
                            'buy_price_windows_15m': [{'low': 804, 'high': 807}]}
                store.conn.execute('INSERT INTO state VALUES(?,?,?)',
                    ('ai_model_input:origin:codex', json.dumps({'live_readonly_accounts': {'accounts': {
                        'zheshang': {'recent_bank_quotes': evidence}}}}), original))
            record(store.conn, 'zheshang', point(1, 830))
            record(store.conn, 'minsheng', point(2, 818))
            record(store.conn, 'zheshang', point(3, 807))
            record(store.conn, 'minsheng', point(15, 819))
            record(store.conn, 'zheshang', (*point(16, 805.5)[:3], 805.25))
            record(store.conn, 'zheshang', point(64, 810))
            result = observed_reviews(store.conn, 'zheshang', 'codex', 4, three + timedelta(seconds=20))
            self.assertEqual(result['examined_runs'], 2)
            self.assertEqual(result['by_branch']['breakout_continuation'], {'no_buy_suggested': 1, 'not_covered': 1})
            self.assertEqual(result['by_branch']['deep_pullback'], {'no_buy_suggested': 1, 'not_covered': 1})
            newer, older = result['cases']
            self.assertEqual((newer['advice_action'], newer['outcome']), ('buy', 'not_covered'))
            self.assertEqual(older['outcome'], 'no_buy_suggested')
            self.assertEqual(older['analysis_seconds'], 90.0)
            self.assertEqual(older['paths'][0]['seconds_to_start'], 30.0)
            self.assertEqual(older['paths'][0]['quote_change_to_publication_cny'], 1)
            self.assertEqual(older['paths'][0]['seconds_to_completion'], 120.0)
            self.assertEqual([q['status'] for q in older['paths'][0]['quote_followups']], ['pending', 'pending'])

            final = observed_reviews(store.conn, 'zheshang', 'codex', 4, START + timedelta(minutes=65))
            at_15, at_60 = final['cases'][1]['paths'][0]['quote_followups']
            self.assertEqual((at_15['status'], at_15['change_cny_per_gram']), ('observed', -.5))
            self.assertEqual(datetime.fromisoformat(at_15['quoted_at']), START + timedelta(minutes=16))
            self.assertEqual(at_15['hypothetical_10g_net_cny'], {
                'at_confirmation': round(10 * (805.25 * .995 - 806), 2),
                'after_analysis': round(10 * (805.25 * .995 - 807), 2), 'wait': 0.0})
            self.assertEqual(at_60, {'minutes': 60, 'status': 'quote_missing'})
            friday = datetime(2026, 11, 20, 18, 25, tzinfo=timezone.utc)
            after_close = followup_quote(store.conn, 'zheshang', friday, 806, 15, friday + timedelta(hours=1))
            self.assertEqual(after_close, {'minutes': 15, 'status': 'market_closed'})

    def test_same_horizon_only_post_publication_quotes_with_observed_fee(self):
        value = {'input_quote_status': 'fresh', 'input_data_status': {'account': 'fresh'},
                 'input_participation_cash_cny': 10_000, 'input_sell_fee_rate': .005,
                 'review_conditions': [route()]}
        samples = [point(m, p) for m, p in [(-1, 785), (1, 798), (2, 804), (4, 805),
                                              (1441, 812), (4321, 790)]]
        end = START + timedelta(minutes=10)
        rows = evaluate(value, START, end, samples, START + timedelta(days=4))
        for row, price in zip(rows, (812, 790)):
            self.assertEqual(row['immediate_10g']['entry_cny_per_gram'], 798)
            self.assertEqual(row['confirmed_10g']['entry_cny_per_gram'], 805)
            self.assertEqual(row['confirmed_10g']['net_cny'], round(10 * (price * .995 - 805), 2))
            self.assertEqual(row['wait']['net_cny'], 0.)
            self.assertIsNone(row['confirmed_10g']['sampled_adverse_excursion_cny'])
            self.assertIsNone(row['confirmed_10g']['sampled_max_drawdown_cny'])
        self.assertGreater(rows[0]['confirmed_10g']['missed_gain_by_waiting_cny'], 0)
        self.assertGreater(rows[1]['confirmed_10g']['loss_avoided_by_waiting_cny'], 0)

        sparse = [point(4, 805), point(10, 806)]
        rows = evaluate(value, START, end, sparse, START + timedelta(days=4))
        self.assertEqual(rows[0]['immediate_10g']['status'], 'publication_quote_missing')
        self.assertEqual(rows[0]['confirmed_10g']['status'], 'confirmation_quotes_missing')

    def test_replay_values_program_breakout_and_respects_position_boundary(self):
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805,
                     'bank_breakout': {'retest_ceiling_cny': 806, 'hold_minutes': 3}}
        end = START + timedelta(minutes=2)
        value = {'input_quote_status': 'fresh', 'input_data_status': {'account': 'fresh'},
                 'input_participation_cash_cny': 10_000, 'input_sell_fee_rate': .005,
                 'buy_watch': {'expires_at': (START + timedelta(hours=1)).isoformat()},
                 'review_conditions': [condition]}
        samples = [point(m, p) for m, p in [(0, 804), (1, 805.5), (2, 806), (4, 806.4),
                                             (1441, 812), (4321, 790)]]
        rows = evaluate(value, START, end, samples, START + timedelta(days=4))
        for row, price in zip(rows, (812, 790)):
            self.assertEqual(row['confirmation_branch'], 'breakout_continuation')
            self.assertEqual(row['confirmed_10g']['entry_cny_per_gram'], 806.4)
            self.assertEqual(row['confirmed_10g']['net_cny'], round(10 * (price * .995 - 806.4), 2))

        value['position_path'] = {'boundary_price_cny': 804}
        rows = evaluate(value, START, end, samples, START + timedelta(days=4))
        self.assertNotIn('confirmation_branch', rows[0])
        self.assertEqual(rows[0]['confirmed_10g'], {'status': 'no_confirmation', 'net_cny': 0.0})
