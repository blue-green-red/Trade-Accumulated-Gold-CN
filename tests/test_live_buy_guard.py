import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, ClaudeCodeResult
from monitor.ai_events import acknowledge, enqueue, read, write
from monitor.config import Settings
from monitor.jd_live_advice import notification_events
from monitor.jd_live_buy_guard import block_entries, remember_reviewed_breakouts, review_failures, retire_failed_events
from monitor.jd_live_semantics import notification_parts
from monitor.jd_live_buy_watch import pending_events, record, request
from monitor.jd_live_conditions import condition_key, read_watch
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
                                ai_analysts=('codex',), ai_urgent_reserve=0, ai_schedule_bj=(),
                                codex_cli='missing-codex', clawx_api_key='')
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
        data = {'bank': 'zheshang', 'advice_id': 1, 'routes': routes}
        enqueue(self.conn, 'codex', 'test', 0, event_id, data)
        return {'live_readonly_accounts': model_input(self.conn, START), 'xauusd': 4000,
                'snapshot_at': START.isoformat(), 'event_coverage': {'codex': [event_id]},
                'trigger_events': [{'id': event_id, 'data': data}]}, event_id

    def route(self, threshold=805, **fields):
        return {'condition_key': str(threshold), 'threshold_cny': threshold,
                'confirmed_at': START.isoformat(), 'price_cny': 806, **fields}

    def prepare_signal(self, routes):
        self.update_quote(0, 806)
        revision = accounts(self.conn, START, include_advice=False, selected_bank='zheshang')[0]['revision']
        with self.conn:
            self.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                ('zheshang', 'codex', 1, START.isoformat(), (START + timedelta(minutes=15)).isoformat(),
                 revision, '{}'))
        return self.context(routes)

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

    def test_queue_keeps_only_confirmations_that_stayed_valid(self):
        _, key = self.prepare_signal([self.route(), self.route(803)])
        self.update_quote(1, 804.9)
        self.update_quote(2, 806.1)
        queued = pending_events(self.conn, 'codex', START + timedelta(minutes=2))
        self.assertEqual([e['id'] for e in queued], [key])
        self.assertEqual([r['threshold_cny'] for r in queued[0]['data']['routes']], [803])
        self.assertEqual(read(self.conn, 'codex')['pending'], queued)

    def test_unchanged_breakout_is_deduplicated_across_advice_until_it_fails(self):
        rule = {'retest_ceiling_cny': 806, 'hold_minutes': 3}
        condition = {'condition': 'at_or_above', 'price_cny': 805, 'quote_side': 'buy', 'bank_breakout': rule}
        context, key = self.prepare_signal([self.route(bank_breakout=rule)])
        revision = context['live_readonly_accounts']['accounts']['zheshang']['revision']
        now = START + timedelta(minutes=1)
        expires = START + timedelta(minutes=16)
        with self.conn:
            remember_reviewed_breakouts(self.conn, 'codex', context, [key], 0)
            acknowledge(self.conn, 'codex', [key])
            self.conn.execute('INSERT INTO jd_live_advice VALUES(2,?,?,?,?,?,?,?)',
                ('zheshang', 'codex', 2, now.isoformat(), expires.isoformat(), revision, '{}'))
        advice = {'id': 2, 'analyst': 'codex', 'status': 'current', 'action_status': 'current',
                  'created_at': now.isoformat(), 'expires_at': expires.isoformat(), 'snapshot_revision': revision,
                  'review_conditions': [condition]}

        def observe(prices):
            for minute, price in prices:
                at = START + timedelta(minutes=minute)
                self.update_quote(minute, price)
                account = accounts(self.conn, at, include_advice=False, selected_bank='zheshang')[0]
                request(self.store, account, advice, at, (at, at, price, price))
            return read(self.conn, 'codex')['pending']

        self.assertEqual(observe([(1, 806.1), (2, 806.2), (3, 806.3), (4, 806.4)]), [])
        state = read_watch(self.conn, 'live-buy-watch:codex:zheshang')['routes'][condition_key(condition)]
        self.assertEqual(state['stage'], 'episode_reviewed')
        self.assertEqual(state['confirmed_at'], START.isoformat())
        self.assertEqual(state['event_id'], key)

        queued = observe([(5, 804.9), (6, 806.01), (7, 806.05), (8, 806.1), (9, 806.5)])
        self.assertEqual([event['id'] for event in queued], ['live-buy-confirmed:codex:zheshang:2'])
        self.assertEqual(queued[0]['data']['routes'][0]['confirmation_mode'], 'continuation')

    def test_queue_keeps_the_independent_entry_when_the_breakout_was_reviewed(self):
        rule = {'retest_ceiling_cny': 806, 'hold_minutes': 3}
        context, previous = self.prepare_signal([self.route(805, bank_breakout=rule)])
        with self.conn:
            remember_reviewed_breakouts(self.conn, 'codex', context, [previous], 0)
            acknowledge(self.conn, 'codex', [previous])
        newer = 'live-buy-confirmed:codex:zheshang:next-plan'
        at = START + timedelta(minutes=1)
        self.update_quote(1, 806.1)
        enqueue(self.conn, 'codex', '同批两条路径', 0, newer, {'bank': 'zheshang', 'advice_id': 1, 'routes': [
            self.route(805, bank_breakout=rule, confirmed_at=at.isoformat()),
            self.route(800, buy_review={'recovery_price_cny': 803, 'max_buy_price_cny': 807},
                       confirmed_at=at.isoformat())]})
        kept = pending_events(self.conn, 'codex', at)
        self.assertEqual([e['id'] for e in kept], [newer])
        self.assertEqual([r['threshold_cny'] for r in kept[0]['data']['routes']], [800])

    def test_signal_that_fails_during_preflight_consumes_no_model_run(self):
        _, key = self.prepare_signal([self.route()])
        m = Monitor(self.settings, True)
        self.addCleanup(m.store.conn.close)
        analyst = AIAnalyst(self.settings, m.store, 'codex', 'codex')
        analyst.analyze = Mock()
        m.analysts = {'codex': analyst}
        m.ai_context = Mock()

        def preflight(_):
            self.update_quote(1, 804.9)
            return {'can_analyze': True, 'status': 'ready'}

        with patch('monitor.jd_live_preflight.prepare', side_effect=preflight), \
                patch('monitor.__main__.utc_now', return_value=START + timedelta(minutes=1)):
            m._ai_worker({}, {}, {'codex': '已进入队列'}, automatic={'codex'})
        m.ai_context.assert_not_called()
        analyst.analyze.assert_not_called()
        self.assertNotIn(key, read(self.conn, 'codex')['covered'])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0], 0)

    def test_late_failure_leaves_other_event_eligible_without_stale_evidence(self):
        context, key = self.prepare_signal([self.route()])
        enqueue(self.conn, 'codex', 'calendar', 0, 'calendar:release')
        m = Monitor(self.settings, True)
        self.addCleanup(m.store.conn.close)
        analyst = AIAnalyst(self.settings, m.store, 'codex', 'codex')
        analyst.analyze = Mock(return_value=None)
        m.analysts = {'codex': analyst}
        observed = START

        def assemble(*_):
            nonlocal observed
            observed = START + timedelta(minutes=1)
            self.update_quote(1, 804.9)
            return context

        with patch.object(m, 'ai_context', side_effect=assemble), \
                patch('monitor.jd_live_preflight.prepare', return_value={'can_analyze': True, 'status': 'ready'}), \
                patch('monitor.__main__.utc_now', side_effect=lambda: observed), \
                patch('monitor.ai.utc_now', side_effect=lambda: observed):
            m._ai_worker({}, {}, {'codex': '已进入队列'}, automatic={'codex'})
        analyst.analyze.assert_called_once()
        sent, reason = analyst.analyze.call_args.args
        self.assertEqual(reason, 'calendar')
        self.assertEqual([e['id'] for e in sent['trigger_events']], ['calendar:release'])
        self.assertEqual(sent['event_coverage']['codex'], ['calendar:release'])
        self.assertNotIn(key, read(self.conn, 'codex')['covered'])

    def test_manual_analysis_still_uses_latest_input_after_entry_signal_is_removed(self):
        context, _ = self.prepare_signal([self.route()])
        m = Monitor(self.settings, True)
        self.addCleanup(m.store.conn.close)
        analyst = AIAnalyst(self.settings, m.store, 'codex', 'codex')
        analyst.analyze = Mock(return_value=None)
        m.analysts = {'codex': analyst}
        m.ai_context = Mock(return_value=context)

        def preflight(_):
            self.update_quote(1, 804.9)
            return {'can_analyze': True, 'status': 'ready'}

        with patch('monitor.jd_live_preflight.prepare', side_effect=preflight), \
                patch('monitor.__main__.utc_now', return_value=START + timedelta(minutes=1)):
            m._ai_worker({}, {}, {'codex': '手动'})
        analyst.analyze.assert_called_once()
        sent, trigger = analyst.analyze.call_args.args
        self.assertEqual(trigger, '手动')
        self.assertEqual(sent['event_coverage']['codex'], [])
        self.assertEqual(sent['trigger_events'], [])

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
