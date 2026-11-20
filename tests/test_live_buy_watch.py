import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from monitor.ai_events import enqueue, read
from monitor.jd_live_buy_audit import evaluate
from monitor.jd_live_buy_watch import advance, pending_events, record, request
from monitor.jd_live_conditions import monitor_prices
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


class BuyWatchTests(unittest.TestCase):
    def advance_all(self, quotes, direction='at_or_below'):
        state = {}
        for minute, price in quotes:
            state = advance(state, route(direction), point(minute, price)[0], price)
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
        self.assertGreater(rows[0]['confirmed_10g']['missed_gain_by_waiting_cny'], 0)
        self.assertGreater(rows[1]['confirmed_10g']['loss_avoided_by_waiting_cny'], 0)

        sparse = [point(4, 805), point(10, 806)]
        rows = evaluate(value, START, end, sparse, START + timedelta(days=4))
        self.assertEqual(rows[0]['immediate_10g']['status'], 'publication_quote_missing')
        self.assertEqual(rows[0]['confirmed_10g']['status'], 'confirmation_quotes_missing')
