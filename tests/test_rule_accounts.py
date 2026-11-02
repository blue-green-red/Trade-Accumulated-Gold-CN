"""Retiring rule books must preserve the ongoing AI experiment and market alerts."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch, Mock

import test_risk_accounts as risk
from monitor.controls import daily_keys, short_labels
from monitor.performance import autonomous_comparison
from monitor.rule_accounts import MARKER, remove_rule_accounts
from monitor.store import utc_now
from monitor.web import dashboard
from monitor.__main__ import Monitor


class RuleAccountRemovalTests(unittest.TestCase):
    def setUp(self):
        self.f = risk.RiskAccountTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def test_migration_deletes_only_rule_ledgers_and_is_idempotent(self):
        f = self.f
        c = f.m.store.conn
        stamp = utc_now().isoformat()
        before = {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)]
                  for table in ('ai_portfolios', 'ai_portfolio_fills', 'ai_portfolio_equity')}
        with c:
            c.execute('DELETE FROM state WHERE key=?', (MARKER,))
            for key in ('hold', 'position', 'codex:position_ai'):
                c.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',
                          (key, key, 100000, stamp, '{}', None))
                c.execute('INSERT INTO account_fills(strategy,ts,side,grams,price,fee) VALUES(?,?,?,?,?,?)',
                          (key, stamp, 'buy', 100, 1000, 0))
                c.execute('INSERT INTO account_equity VALUES(?,?,?,?,?,?)',
                          (key, '2026-11-02', 100000, 1000, 100, 0))
                c.execute('INSERT INTO trade_cycles(strategy,opened_at) VALUES(?,?)', (key, stamp))
            for key in ('short', 'codex:short_ai'):
                c.execute('INSERT INTO paper_accounts VALUES(?,?,?)', (key, 100000, stamp))
                c.execute('INSERT INTO paper_equity VALUES(?,?,?)', (key, stamp, 100000))
                trade = c.execute('INSERT INTO paper_trades(bank,status,strategy) VALUES(?,?,?)',
                                  ('zheshang', 'open', key)).lastrowid
                c.execute('INSERT INTO paper_fills(trade_id,ts,side,grams,price,fee) VALUES(?,?,?,?,?,?)',
                          (trade, stamp, 'buy', 1, 1000, 0))
            for key in ('paper:short:open:1', 'acct:hold:day:buy', 'move:up', 'ai:codex:round'):
                c.execute('INSERT INTO alerts VALUES(?,?,?,?,?)', (stamp, key, key, key, 0))
                c.execute('INSERT INTO notification_outbox(event_key,created_at,expires_at,event_json,next_attempt_at,updated_at) VALUES(?,?,?,?,?,?)',
                          (key, stamp, stamp, json.dumps({'key': key}), stamp, stamp))
            c.execute('INSERT INTO state VALUES(?,?,?)', ('notify:rules:short', '{"enabled":true}', stamp))
        self.assertTrue(remove_rule_accounts(c))
        self.assertFalse(remove_rule_accounts(c))
        for table in ('accounts', 'account_fills', 'account_equity', 'trade_cycles'):
            self.assertEqual([r[0] for r in c.execute('SELECT strategy FROM ' + table)], ['codex:position_ai'])
        for table in ('paper_accounts', 'paper_trades', 'paper_equity'):
            self.assertEqual([r[0] for r in c.execute('SELECT strategy FROM ' + table)], ['codex:short_ai'])
        self.assertEqual(c.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0], 1)
        self.assertEqual({r[0] for r in c.execute('SELECT key FROM alerts')}, {'move:up', 'ai:codex:round'})
        self.assertEqual({r[0] for r in c.execute('SELECT event_key FROM notification_outbox')}, {'move:up', 'ai:codex:round'})
        self.assertIsNone(c.execute("SELECT 1 FROM state WHERE key='notify:rules:short'").fetchone())
        for table, rows in before.items():
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM ' + table)], rows, table)

    def test_old_configuration_cannot_recreate_removed_accounts(self):
        f = self.f
        settings = replace(f.settings, strategies=('hold', 'position', 'short', 'position_ai', 'short_ai'))
        self.assertEqual(daily_keys(settings), ())
        self.assertEqual(short_labels(settings), {})
        restarted = Monitor(settings, True)
        try:
            self.assertEqual(restarted.accounts.strategies, [])
            self.assertEqual(restarted.short_books, {})
            for table in ('accounts', 'paper_accounts'):
                self.assertEqual(restarted.store.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
            d = dashboard(settings)
            self.assertEqual(len(d['autonomous']['accounts']), 12)
            self.assertEqual(d['comparison']['baselines'], [])
        finally:
            restarted.store.conn.close()

    def test_same_start_benchmarks_remain_without_reference_accounts(self):
        f = self.f
        cmp = autonomous_comparison(f.m.store.conn, f.settings, [], [], {'series': []}, 1100)
        self.assertEqual(cmp['baselines'], [])
        for model in cmp['models']:
            self.assertEqual(len(model['accounts']), 6)
            for row in model['accounts']:
                expected = 1100 * (1-f.settings.sell_fee_rate) / row['initial_price'] - 1 if row['start_mode'].endswith('_full') else 0
                self.assertAlmostEqual(row['baseline_return'], expected)
                self.assertAlmostEqual(row['baseline_value'], row['budget'] * (1+expected))
                self.assertAlmostEqual(row['excess_return'], row['return']-expected)
                self.assertEqual(row['baseline_name'], '满仓持有不动' if row['start_mode'].endswith('_full') else '空仓持币不动')

    def test_market_alerts_still_run_without_rule_books(self):
        f = self.f
        m = f.m
        event = {'key': 'move:up', 'title': '行情异动', 'body': 'test'}
        with patch.object(m.market, 'refresh_fx_if_due'), patch.object(m.market, 'snapshot', return_value=f.snap), \
             patch('monitor.__main__.trading_session', return_value={'is_open': True}), \
             patch.object(m, 'poll_jin10_if_due'), patch.object(m, 'refresh_macro_if_due'), \
             patch.object(m, 'request_ai_after_data_release'), patch.object(m, 'run_ai_if_due', return_value=[]), \
             patch('monitor.cadence.request_periodic'), patch('monitor.jd_live_review.request_reviews'), \
             patch.object(m, 'digest_if_due', return_value=[]), patch.object(m, 'weekly_report_if_due', return_value=[]), \
             patch.object(m.alerts, 'check', return_value=[event]) as alerts:
            m.analysts = {'codex': Mock()}
            _, _, events = m.tick()
            alerts.assert_called_once()
            self.assertIn(event, events)
            m.analysts['codex'].request_refresh.assert_called_once()
