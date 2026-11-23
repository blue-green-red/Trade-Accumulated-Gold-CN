import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from monitor.ai import AIAnalyst, ClaudeCodeResult
from monitor.ai_events import enqueue, read, write
from monitor.config import Settings
from monitor.jd_live_advice import notification_events
from monitor.jd_live_buy_guard import block_entries, review_failures, retire_failed_events
from monitor.jd_live_semantics import notification_parts
from monitor.jd_live_buy_watch import record
from monitor.jd_live_readonly import accounts, ensure_schema, model_input, parse_account, record_success
from monitor.store import Store
from test_ai_event_timing import result
from test_jd_live import payload

START = datetime(2026, 11, 16, 2, tzinfo=timezone.utc)


class BuyGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = replace(Settings(), db_path=Path(tmp.name) / 'guard.db', ai_trading_mode='live_only',
                                ai_analysts=('codex',), ai_urgent_reserve=0)
        self.store = Store(self.settings.db_path)
        self.conn = self.store.conn
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)

    def update_quote(self, minute, price, bank='zheshang'):
        at = START + timedelta(minutes=minute)
        snapshot = parse_account(bank, payload(bank))
        snapshot.update(cash_cny=20_000, cash_source='jd_electronic_account_api',
                        quote={'buy_price_cny': price, 'sell_price_cny': price, 'quoted_at': at.isoformat()})
        record_success(self.conn, bank, snapshot, at.isoformat())
        record(self.conn, bank, (at, at, price, price))

    def context(self, routes):
        event_id = 'live-buy-confirmed:codex:zheshang:test-plan'
        data = {'bank': 'zheshang', 'routes': routes}
        enqueue(self.conn, 'codex', 'test', 0, event_id, data)
        return {'live_readonly_accounts': model_input(self.conn, START), 'xauusd': 4000,
                'snapshot_at': START.isoformat(), 'event_coverage': {'codex': [event_id]},
                'trigger_events': [{'id': event_id, 'data': data}]}, event_id

    def route(self, threshold=805, **fields):
        return {'condition_key': str(threshold), 'threshold_cny': threshold,
                'confirmed_at': START.isoformat(), **fields}

    def test_tape_catches_an_inflight_loss_and_ignores_other_banks(self):
        self.update_quote(0, 806)
        self.update_quote(0, 804, bank='minsheng')
        context, key = self.context([self.route(), self.route(803)])
        self.update_quote(1, 804, bank='minsheng')
        self.update_quote(1, 804.9)
        self.update_quote(2, 806.1)
        now = START + timedelta(minutes=2)
        self.assertEqual(review_failures(self.conn, context, [key], now), {})
        context['trigger_events'][0]['data']['routes'].pop()
        self.assertIn('zheshang', review_failures(self.conn, context, [key], now))
        self.assertEqual(review_failures(self.conn, context, [], now), {})

    def test_cap_breach_and_gap_invalidates_even_after_latest_quote_recovers(self):
        self.update_quote(0, 806)
        setup = {'recovery_price_cny': 805, 'max_buy_price_cny': 807}
        context, key = self.context([self.route(buy_review=setup)])
        self.update_quote(1, 807.01)
        self.update_quote(2, 806)
        self.assertIn('zheshang', review_failures(self.conn, context, [key], START + timedelta(minutes=2)))
        context['trigger_events'][0]['data']['routes'] = [self.route()]
        self.update_quote(6, 806)
        self.assertIn('zheshang', review_failures(self.conn, context, [key], START + timedelta(minutes=6)))

    def test_failed_observation_does_not_ack_a_later_confirmation(self):
        self.update_quote(0, 806)
        context, key = self.context([self.route()])
        later = read(self.conn, 'codex')
        later['pending'][0]['data']['routes'][0]['confirmed_at'] = (START + timedelta(minutes=3)).isoformat()
        with self.conn:
            write(self.conn, 'codex', later)
            self.assertEqual(retire_failed_events(self.conn, 'codex', context, [key], {'zheshang': 'stale'}), [])
        self.assertEqual(read(self.conn, 'codex'), later)

    def test_sell_and_existing_protection_can_still_pass_independent_validation(self):
        advice = {'action': 'sell', 'checks': [], 'suggested_orders': [
            {'kind': 'limit_buy'}, {'kind': 'take_profit_stop_loss'}], 'review_conditions': [],
            'order_management': [{'action': 'keep', 'input_order': {'action': 'sell'}}]}
        block_entries(advice, '买入触发已失效')
        visible = notification_parts(advice)
        self.assertEqual(visible['action'], 'sell')
        self.assertEqual([o['kind'] for o in visible['suggested_orders']], ['take_profit_stop_loss'])
        self.assertEqual(visible['order_management'], advice['order_management'])

    def test_publication_blocks_stale_buy_without_retrying_the_trigger(self):
        self.update_quote(0, 806)
        context, key = self.context([self.route()])
        analyst = AIAnalyst(self.settings, self.store, 'codex', 'codex')
        data = result().model_dump()
        data.update(research_notes='测试', sources=[])
        data['live_account_advice'] = {'zheshang': {
            'action': 'buy', 'grams': 10, 'price_cny': 806, 'price_condition': 'at_or_below',
            'reason': '突破后首笔买入。', 'risk_note': '注意回落。',
            'suggested_orders': [],
            'position_path': {'target_position_pct': 2, 'boundary_scenario_index': None,
                              'exit_scope': 'tranche', 'continuation': '若回踩不破，仅考虑首笔。'},
        }}
        response = ClaudeCodeResult.model_validate(data)
        finish = START + timedelta(minutes=2)

        def backend(*_):
            self.update_quote(1, 804.9)
            self.update_quote(2, 806.1)
            return 'notes', [], response

        with patch.object(analyst, '_run_codex', side_effect=backend), patch('monitor.ai.utc_now', return_value=finish), \
                patch('monitor.jd_live_advice.datetime') as clock:
            clock.now.return_value = finish
            view = analyst.analyze(context, '突破条件', run_id='guard-fixture')
        latest = accounts(self.conn, finish, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(latest['action_status'], 'review_required')
        self.assertEqual(latest['checks'], ['分析期间买入触发已失守、超价或报价中断，须按新行情复评'])
        self.assertEqual(notification_events(self.conn, 'codex', view['_view_id']), [])
        self.assertNotIn(key, read(self.conn, 'codex')['covered'])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0], 1)
