import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from monitor.__main__ import Monitor
from monitor.ai import AIAnalyst, ClaudeCodeResult
from monitor.ai_events import acknowledge, enqueue, read, write
from monitor.ai_inputs import compact_context
from monitor.config import Settings
from monitor.jd_live_advice import notification_events, save
from monitor.jd_live_buy_guard import (block_entries, remember_reviewed_breakouts, remember_reviewed_confirmations,
                                       remember_reviewed_price_observations, remove_submitted_confirmations,
                                       review_failures, retire_failed_events)
from monitor.jd_live_semantics import notification_parts
from monitor.jd_live_buy_watch import carry_entry_observations, pending_events, plain_buy_discards, record, request, set_watch
from monitor.jd_live_conditions import condition_key, condition_state, read_watch
from monitor.jd_live_readonly import accounts, ensure_schema, model_input, parse_account, record_success
from monitor.notification_control import set_subscription
from monitor.notification_outbox import dispatch
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
                                codex_cli='missing-codex', clawx_api_key='', ai_optimize=True)
        self.store = Store(self.settings.db_path)
        self.conn = self.store.conn
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)

    def update_quote(self, minute, price, bank='zheshang', *, sell_price=None, pending=()):
        at = START + timedelta(minutes=minute)
        sell_price = price if sell_price is None else sell_price
        snapshot = parse_account(bank, payload(bank))
        snapshot.update(cash_cny=20_000, cash_source='jd_electronic_account_api',
                        quote={'buy_price_cny': price, 'sell_price_cny': sell_price, 'quoted_at': at.isoformat()},
                        pending_order_count=len(pending),
                        pending_orders={'records': list(pending), 'total_count': len(pending), 'complete': True},
                        coverage={'pending_orders': 'available'}, component_times={'pending_orders': at.isoformat()})
        record_success(self.conn, bank, snapshot, at.isoformat())
        record(self.conn, bank, (at, at, price, sell_price))

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

    def test_exit_boundary_clears_pending_confirmation_even_after_a_rebound(self):
        context, key = self.prepare_signal([self.route()])
        with self.conn:
            self.conn.execute('UPDATE jd_live_advice SET advice_json=? WHERE id=1', (json.dumps({
                'buy_watch': {'id': 'test-plan', 'expires_at': (START + timedelta(minutes=15)).isoformat()},
                'position_path': {'boundary_price_cny': 805.75},
            }),))
        self.update_quote(1, 805.75, bank='minsheng')
        self.update_quote(1, 806, sell_price=805.8)
        queued = pending_events(self.conn, 'codex', START + timedelta(minutes=1))
        self.assertEqual([e['id'] for e in queued], [key])
        context['trigger_events'] = queued
        compact = compact_context(context)
        self.assertNotIn('position_boundary_cny', compact['trigger_events'][0]['data']['routes'][0])
        self.assertEqual(queued[0]['data']['routes'][0]['position_boundary_cny'], 805.75)
        self.update_quote(2, 806, sell_price=805.75)
        self.update_quote(3, 806.1)
        after_recovery = START + timedelta(minutes=3)
        self.assertEqual(pending_events(self.conn, 'codex', after_recovery), [])
        self.assertIn('zheshang', review_failures(self.conn, context, [key], after_recovery))

    def test_queued_paths_keep_their_own_deadlines_in_a_shared_watch(self):
        short = self.route(804, buy_review={'recovery_price_cny': 805, 'max_buy_price_cny': 808})
        breakout = self.route(bank_breakout={'retest_ceiling_cny': 806, 'hold_minutes': 3})
        context, key = self.prepare_signal([short, breakout])
        short_end = START + timedelta(minutes=1)
        watch_end = START + timedelta(hours=1)
        with self.conn:
            self.conn.execute('UPDATE jd_live_advice SET expires_at=?,advice_json=? WHERE id=1', (
                short_end.isoformat(), json.dumps({'buy_watch': {'id': 'test-plan', 'expires_at': watch_end.isoformat()}})))
            events = read(self.conn, 'codex')
            events['pending'][0]['data']['plan_id'] = 'test-plan'
            write(self.conn, 'codex', events)
        submitted, = pending_events(self.conn, 'codex', START)
        context['trigger_events'] = [{**submitted, 'data': {**submitted['data'], 'routes': [submitted['data']['routes'][0]]}}]
        self.update_quote(1, 806)
        pending, = pending_events(self.conn, 'codex', short_end)
        self.assertEqual(pending['id'], key)
        self.assertEqual([r['condition_key'] for r in pending['data']['routes']], [breakout['condition_key']])
        self.assertIn('zheshang', review_failures(self.conn, context, [key], short_end))

    def test_plain_buy_observations_do_not_repeat_on_the_same_price_move(self):
        def queue(advice_id, minute, quote, threshold, *, quote_side='buy', direction='at_or_above'):
            at = START + timedelta(minutes=minute)
            self.update_quote(minute, quote)
            revision = accounts(self.conn, at, include_advice=False)[0]['revision']
            condition = {'condition': direction, 'quote_side': quote_side, 'price_cny': threshold}
            origin = {'review_conditions': [condition]}
            with self.conn:
                self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                    (advice_id, 'zheshang', 'codex', advice_id, at.isoformat(),
                     (at + timedelta(minutes=15)).isoformat(), revision, json.dumps(origin)))
            key = condition_key(condition)
            event_id = f'live-price:codex:zheshang:{advice_id}:{key}'
            enqueue(self.conn, 'codex', 'test', 0, event_id, {'bank': 'zheshang', 'advice_id': advice_id,
                'condition_key': key, 'quote_side': quote_side, 'quoted_at': at.isoformat()})
            return event_id

        first = queue(1, 0, 806, 805)
        original = read(self.conn, 'codex')['pending']
        now = START + timedelta(minutes=1)
        second = queue(2, 1, 806.2, 806.1)
        self.assertEqual([e['id'] for e in pending_events(self.conn, 'codex', now)], [second])
        with self.conn:
            context = {'trigger_events': original}
            remember_reviewed_price_observations(self.conn, 'codex', context, [first], 0)
            acknowledge(self.conn, 'codex', [first])
        self.assertEqual(pending_events(self.conn, 'codex', now), [])
        discards = plain_buy_discards(self.conn, 'codex')
        self.assertEqual([(p['event_id'], p['reason']) for p in discards],
                         [(first, 'advice_replaced'), (second, 'same_price_episode_already_reviewed')])
        self.assertTrue(all(datetime.fromisoformat(p['discarded_at']) == now for p in discards))
        self.assertEqual(pending_events(self.conn, 'codex', now), [])
        self.assertEqual(plain_buy_discards(self.conn, 'codex'), discards)

        sell = queue(3, 2, 806.3, 806, quote_side='sell')
        down = queue(4, 2, 806.3, 807, direction='at_or_below')
        self.assertEqual([e['id'] for e in pending_events(self.conn, 'codex', START + timedelta(minutes=2))], [sell, down])
        self.update_quote(3, 804.9)
        recross = queue(5, 4, 806.5, 806.4)
        self.assertEqual([e['id'] for e in pending_events(self.conn, 'codex', START + timedelta(minutes=4))], [sell, recross])

    def test_pending_plain_buy_survives_an_identical_successor_until_original_expiry(self):
        self.update_quote(0, 806)
        revision = accounts(self.conn, START, include_advice=False)[0]['revision']
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805}
        with self.conn:
            for i, minute, length in ((1, 0, 6), (2, 1, 15)):
                at = START + timedelta(minutes=minute)
                self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                    (i, 'zheshang', 'codex', i, at.isoformat(), (at + timedelta(minutes=length)).isoformat(),
                     revision, json.dumps({'review_conditions': [{**condition, 'reason': f'第{i}次观察'}]})))
        key = condition_key(condition)
        event_id = f'live-price:codex:zheshang:1:{key}'
        enqueue(self.conn, 'codex', 'test', 0, event_id, {'bank': 'zheshang', 'advice_id': 1,
            'condition_key': key, 'quote_side': 'buy', 'quoted_at': START.isoformat()})
        self.assertEqual([e['id'] for e in pending_events(self.conn, 'codex', START + timedelta(minutes=1))], [event_id])
        self.assertFalse(pending_events(self.conn, 'codex', START + timedelta(minutes=6)))
        discard, = plain_buy_discards(self.conn, 'codex')
        self.assertEqual((discard['event_id'],discard['reason']), (event_id,'advice_expired'))

    def test_two_explicit_thresholds_in_one_direction_are_reviewed_independently(self):
        shallow={'condition':'at_or_above','quote_side':'buy','price_cny':805}
        high={**shallow,'price_cny':807}

        def save_advice(advice_id, minute):
            at=START+timedelta(minutes=minute)
            revision=accounts(self.conn,at,include_advice=False)[0]['revision']
            with self.conn:
                self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                    (advice_id,'zheshang','codex',advice_id,at.isoformat(),(at+timedelta(minutes=15)).isoformat(),
                     revision,json.dumps({'review_conditions':[shallow,high]})))

        def queue(advice_id, minute, condition, price):
            self.update_quote(minute,price)
            key=condition_key(condition)
            event_id=f'live-price:codex:zheshang:{advice_id}:{key}'
            enqueue(self.conn,'codex','分层买价复评',0,event_id,{'bank':'zheshang','advice_id':advice_id,
                'condition_key':key,'quote_side':'buy','price_cny':price,
                'quoted_at':(START+timedelta(minutes=minute)).isoformat()})
            return event_id

        self.update_quote(0,806)
        save_advice(1,0)
        first=queue(1,0,shallow,806)
        events=pending_events(self.conn,'codex',START)
        with self.conn:
            remember_reviewed_price_observations(self.conn,'codex',{'trigger_events':events},[first],0)
            acknowledge(self.conn,'codex',[first])
        higher=queue(1,1,high,807.5)
        events=pending_events(self.conn,'codex',START+timedelta(minutes=1))
        self.assertEqual([e['id'] for e in events],[higher])
        with self.conn:
            remember_reviewed_price_observations(self.conn,'codex',{'trigger_events':events},[higher],0)
            acknowledge(self.conn,'codex',[higher])

        save_advice(2,1)
        queue(2,2,shallow,807.6)
        queue(2,2,high,807.6)
        self.assertEqual(pending_events(self.conn,'codex',START+timedelta(minutes=2)),[])

    def test_same_plain_buy_can_recross_when_successful_analysis_leaves_the_advice_in_place(self):
        from monitor.jd_live_conditions import monitor_prices

        self.update_quote(0,806)
        revision=accounts(self.conn,START,include_advice=False)[0]['revision']
        condition={'condition':'at_or_above','quote_side':'buy','price_cny':805,'reason':'买价上穿后复评。'}
        with self.conn:
            self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                (1,'zheshang','codex',1,START.isoformat(),(START+timedelta(minutes=10)).isoformat(),
                 revision,json.dumps({'action':'hold','review_conditions':[condition]})))

        def watch(minute, price):
            self.update_quote(minute,price)
            now=START+timedelta(minutes=minute)
            account=accounts(self.conn,now,selected_bank='zheshang')[0]
            monitor_prices(self.store,account,account['advice'][0],now)
            return pending_events(self.conn,'codex',now)

        first,=watch(0,806)
        with self.conn:
            remember_reviewed_price_observations(self.conn,'codex',{'trigger_events':[first]},[first['id']],0)
            acknowledge(self.conn,'codex',[first['id']])
        self.assertEqual(watch(1,806.1),[])
        self.assertEqual(watch(2,804.9),[])
        second,=watch(3,806.2)
        self.assertNotEqual(second['id'],first['id'])
        self.assertEqual(second['data']['advice_id'],1)
        self.assertEqual(second['data']['price_cny'],806.2)
        self.assertEqual(watch(4,804.9),[second])
        self.assertEqual(watch(5,806.7),[second])

    def test_failed_observation_does_not_ack_a_later_confirmation(self):
        self.update_quote(0, 806)
        context, key = self.context([self.route()])
        later = read(self.conn, 'codex')
        later['pending'][0]['data']['routes'][0]['confirmed_at'] = (START + timedelta(minutes=3)).isoformat()
        with self.conn:
            write(self.conn, 'codex', later)
            self.assertEqual(retire_failed_events(self.conn, 'codex', context, [key], {'zheshang': 'stale'}), [])
        self.assertEqual(read(self.conn, 'codex'), later)

    def test_successful_review_keeps_a_confirmation_that_arrived_during_analysis(self):
        context, key = self.prepare_signal([self.route()])
        analyst = AIAnalyst(self.settings, self.store, 'codex', 'codex')
        data = result().model_dump()
        data.update(research_notes='测试', sources=[], live_account_advice={})
        response = ClaudeCodeResult.model_validate(data)
        finish = START + timedelta(minutes=2)
        late = self.route(804, confirmed_at=(START + timedelta(minutes=1)).isoformat(), confirmation_mode='continuation',
                          bank_breakout={'hold_minutes': 3, 'retest_ceiling_cny': 805})
        watch_key = 'live-buy-watch:codex:zheshang'
        first = context['trigger_events'][0]['data']['routes'][0]
        stages = {route['condition_key']: {'stage': 'confirmed', 'confirmed_at': route['confirmed_at']}
                  for route in (first, late)}
        self.store.set_state(watch_key, {'advice_id': 1, 'routes': stages})

        def backend(*_):
            self.update_quote(1, 806)
            events = read(self.conn, 'codex')
            events['pending'][0]['data']['routes'].append(late)
            with self.conn:
                write(self.conn, 'codex', events)
            self.update_quote(2, 806)
            return 'notes', [], response

        with patch.object(analyst, '_run_codex', side_effect=backend), patch('monitor.ai.utc_now', return_value=finish):
            view = analyst.analyze(context, '突破条件', run_id='first-review')
        self.assertEqual(view['_covered_event_ids'], [key])
        events = read(self.conn, 'codex')
        self.assertNotIn(key, events['covered'])
        self.assertEqual(events['pending'][0]['data']['routes'], [late])
        self.assertEqual(events['pending'][0]['data']['quoted_at'], late['confirmed_at'])
        self.assertIn('突破后持续走高', events['pending'][0]['reason'])
        steps = read_watch(self.conn, watch_key)['routes']
        self.assertEqual(steps[first['condition_key']]['reviewed_confirmation_at'], first['confirmed_at'])
        self.assertNotIn('reviewed_confirmation_at', steps[late['condition_key']])
        context['trigger_events'] = pending_events(self.conn, 'codex', finish)

        with patch.object(analyst, '_run_codex', return_value=('notes', [], response)), \
                patch('monitor.ai.utc_now', return_value=finish):
            view = analyst.analyze(context, '后到的复评信号', run_id='second-review')
        self.assertEqual(view['_covered_event_ids'], [key])
        self.assertEqual(read(self.conn, 'codex'), {'pending': [], 'covered': [key]})
        self.assertEqual(read_watch(self.conn, watch_key)['routes'][late['condition_key']]['reviewed_confirmation_at'],
                         late['confirmed_at'])

    def test_publication_only_marks_the_surviving_confirmation_reviewed(self):
        from monitor.jd_live_buy_audit import observed_reviews
        from monitor.jd_live_buy_guard import confirmation_key

        rule = {'hold_minutes': 3, 'retest_ceiling_cny': 806}
        expired = self.route(805, bank_breakout=rule, confirmation_mode='continuation',
                             signal_expires_at=(START + timedelta(minutes=1)).isoformat())
        live = self.route(805, condition_key='805:retest', bank_breakout=rule, confirmation_mode='retest')
        context, key = self.prepare_signal([expired, live])
        watch_key = 'live-buy-watch:codex:zheshang'
        self.store.set_state(watch_key, {'advice_id': 1, 'routes': {
            r['condition_key']: {'stage': 'confirmed', 'confirmed_at': r['confirmed_at'],
                                'confirmation_mode': r['confirmation_mode']}
            for r in (expired, live)}})
        analyst = AIAnalyst(self.settings, self.store, 'codex', 'codex')
        data = result().model_dump()
        data.update(research_notes='测试', sources=[], live_account_advice={})
        response = ClaudeCodeResult.model_validate(data)
        finish = START + timedelta(minutes=2)

        def backend(*_):
            self.update_quote(2, 806)
            return 'notes', [], response

        with patch.object(analyst, '_run_codex', side_effect=backend), patch('monitor.ai.utc_now', return_value=finish), \
                patch('monitor.store.utc_now', return_value=finish):
            view = analyst.analyze(context, '合并复评', run_id='mixed-expiry')

        self.assertEqual(view['_covered_event_ids'], [key])
        self.assertEqual(view['_valid_entry_confirmations'], {key: [confirmation_key(live)]})
        state = read_watch(self.conn, watch_key)
        self.assertEqual(state['reviewed_routes'], {live['condition_key']: key})
        self.assertNotIn('reviewed_confirmation_at', state['routes'][expired['condition_key']])
        self.assertIsNone(read_watch(self.conn, 'live-breakout-reviewed:codex:zheshang:continuation').get('event_id'))
        self.assertEqual(read_watch(self.conn, 'live-breakout-reviewed:codex:zheshang:retest')['event_id'], key)
        self.assertFalse(read(self.conn, 'codex')['pending'])
        report = observed_reviews(self.conn, 'zheshang', 'codex', 12, finish)
        self.assertEqual(report['by_branch']['breakout_continuation'], {'signal_invalidated_at_publication': 1})
        self.assertEqual(report['by_branch']['breakout_retest'], {'no_advice': 1})

    def publish_watch(self, advice_id, at, pullback, boundary=795, *, retest_price=None, exit_scope=None,
                      basis='依据本行近一小时采样。', max_buy_price=806, candidate_until=None, review_grams=10):
        revision = accounts(self.conn, at, include_advice=False)[0]['revision']
        source = {'status': 'fresh', 'quote': {'status': 'fresh'}, 'revision': revision,
                  'recent_bank_quotes': {'status': 'available', 'buy_price_windows_15m': [{'low': 801, 'high': 805}]}}
        deadline = (at + timedelta(minutes=15)).isoformat()
        conditions = [{'condition': 'at_or_below', 'quote_side': 'buy', 'price_cny': pullback,
                       'buy_review': {'grams': review_grams, 'recovery_price_cny': 803, 'hold_minutes': 3,
                                      'max_buy_price_cny': max_buy_price, 'zone': {'floor_cny': 797,
                                          'weak_target_cny': 818, 'risk_exit_cny': 796, 'basis': basis}}}]
        if retest_price is not None:
            conditions.append({**conditions[0], 'condition': 'at_or_above', 'price_cny': retest_price})
        advice = {'id': advice_id, 'analyst': 'codex', 'action': 'hold', 'status': 'current', 'action_status': 'current',
                  'valid_for_minutes': 15, 'review_conditions': conditions, 'created_at': at.isoformat(),
                  'expires_at': deadline, 'snapshot_revision': revision,
                  'position_path': {'boundary_price_cny': boundary, 'exit_scope': exit_scope}}
        if candidate_until:
            advice['limit_buy_reviews'] = [{'decision': 'needs_review', 'entry_price_cny': max_buy_price,
                                            'risk_exit_cny': 796, 'grams': 10, 'expires_at': candidate_until.isoformat()}]
        with self.conn:
            set_watch(self.conn, 'zheshang', 'codex', advice, source, at, 10)
            self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                (advice_id, 'zheshang', 'codex', advice_id, at.isoformat(), deadline, revision, json.dumps(advice)))
        return advice

    def observe_watch(self, advice, quotes):
        for minute, price in quotes:
            at = START + timedelta(minutes=minute)
            self.update_quote(minute, price)
            account = accounts(self.conn, at, include_advice=False)[0]
            request(self.store, account, advice, at, (at, at, price, price))

    def test_low_entry_candidate_expires_while_its_breakout_watch_keeps_running(self):
        self.update_quote(0, 801)
        cutoff = START + timedelta(minutes=6)
        advice = self.publish_watch(1, START, 799, candidate_until=cutoff)
        self.observe_watch(advice, [(1, 799), (2, 803.5), (4, 804), (5, 804.1)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=5))
        route, = event['data']['routes']
        self.assertEqual(datetime.fromisoformat(route['signal_expires_at']), cutoff)

        self.assertEqual(pending_events(self.conn, 'codex', cutoff), [])
        account = accounts(self.conn, cutoff, include_advice=False)[0]
        expired = condition_state(self.conn, account, advice, advice['review_conditions'][0], cutoff)
        self.assertEqual(expired['status'], 'expired')
        self.assertEqual(datetime.fromisoformat(expired['expires_at']), cutoff)
        from monitor.jd_live_readonly import model_input
        previous = model_input(self.conn, cutoff)['accounts']['zheshang']['previous_advice'][0]
        low, breakout = previous['review_conditions']
        self.assertEqual(low['monitoring'], {'status': 'expired', 'confirmation_stage': 'confirmed',
                                            'expires_at': cutoff.isoformat()})
        self.assertEqual(breakout['monitoring']['status'], 'monitoring')
        self.assertIn('retest_ceiling_cny', breakout['bank_breakout'])
        self.assertNotIn('reason', breakout)

        self.observe_watch(advice, [(6, 805.5), (7, 806.5), (9, 807)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=9))
        breakout, = event['data']['routes']
        self.assertEqual(breakout['confirmation_mode'], 'continuation')
        self.assertEqual(breakout['signal_expires_at'], advice['buy_watch']['expires_at'])
        previous = model_input(self.conn, START + timedelta(minutes=9))['accounts']['zheshang']['previous_advice'][0]
        self.assertEqual(previous['review_conditions'][0]['monitoring'],
                         {'status': 'expired', 'confirmation_stage': 'confirmed', 'expires_at': cutoff.isoformat()})

    def test_reissued_low_entry_after_its_deadline_requires_new_recovery(self):
        self.update_quote(0, 799)
        advice = self.publish_watch(1, START, 799, candidate_until=START + timedelta(minutes=5))
        self.observe_watch(advice, [(0, 799), (1, 803.5), (4, 804)])
        first, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])
        self.update_quote(6, 804.2)
        later = self.publish_watch(2, START + timedelta(minutes=6), 799, candidate_until=START + timedelta(minutes=14))
        self.assertEqual(later['buy_watch']['id'], advice['buy_watch']['id'])
        self.observe_watch(later, [(7, 804.3)])
        self.assertEqual(pending_events(self.conn, 'codex', START + timedelta(minutes=7)), [])
        self.observe_watch(later, [(8, 799), (9, 803.5), (11, 804), (12, 804.2)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=12))
        low, = event['data']['routes']
        self.assertNotEqual(event['id'], first['id'])
        self.assertEqual(datetime.fromisoformat(low['confirmed_at']), START + timedelta(minutes=12))
        self.assertEqual(datetime.fromisoformat(low['signal_expires_at']), START + timedelta(minutes=14))

    def test_untouched_expired_observation_retains_its_deadline_without_a_stage(self):
        self.update_quote(0, 801)
        end = START + timedelta(minutes=5)
        advice = self.publish_watch(1, START, 799, candidate_until=end)
        self.observe_watch(advice, [(1, 801), (3, 802), (6, 802)])
        at = START + timedelta(minutes=6)
        previous = model_input(self.conn, at)['accounts']['zheshang']['previous_advice'][0]
        self.assertEqual(previous['review_conditions'][0]['monitoring'],
                         {'status': 'expired', 'expires_at': end.isoformat()})

    def test_reissued_low_observation_rejects_a_delayed_quote_from_before_publication(self):
        self.update_quote(0, 799)
        advice = self.publish_watch(1, START, 799, candidate_until=START + timedelta(minutes=5))
        self.observe_watch(advice, [(0, 799), (1, 803.5), (4, 804), (5.5, 804)])
        fingerprint = condition_key(advice['review_conditions'][0])
        state = read_watch(self.conn, 'live-buy-watch:codex:zheshang')
        self.assertNotIn(fingerprint, state['routes'])
        self.assertIn(fingerprint, state['expired_routes'])

        later = self.publish_watch(2, START + timedelta(minutes=6), 799)
        self.assertEqual(later['buy_watch']['id'], advice['buy_watch']['id'])
        stamped, received = START + timedelta(minutes=5.75), START + timedelta(minutes=6.25)
        self.update_quote(5.75, 799)
        with self.conn:
            self.conn.execute('UPDATE jd_live_quote_samples SET observed_at=? WHERE bank=? AND quoted_at=?',
                              (received.isoformat(timespec='microseconds'), 'zheshang', stamped.isoformat(timespec='microseconds')))
        account = accounts(self.conn, received, include_advice=False)[0]
        request(self.store, account, later, received, (stamped, received, 799, 799))
        self.observe_watch(later, [(7, 803.5), (8, 804), (10, 804.2)])
        self.assertEqual(pending_events(self.conn, 'codex', START + timedelta(minutes=10)), [])

        self.observe_watch(later, [(11, 799), (12, 803.5), (15, 804)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=15))
        low, = event['data']['routes']
        self.assertEqual(datetime.fromisoformat(low['touched_at']), START + timedelta(minutes=11))

    def test_carried_low_entry_keeps_its_earlier_candidate_deadline(self):
        self.update_quote(0, 801)
        cutoff = START + timedelta(minutes=6)
        advice = self.publish_watch(1, START, 799, candidate_until=cutoff)
        self.observe_watch(advice, [(1, 799), (2, 803.5), (4, 804)])
        at = START + timedelta(minutes=4)
        successor = self.publish_watch(2, at, 799, retest_price=808, candidate_until=START + timedelta(minutes=14))
        self.assertNotEqual(successor['buy_watch']['id'], advice['buy_watch']['id'])
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.observe_watch(successor, [(5, 804.1)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=5))
        route, = event['data']['routes']
        self.assertEqual(datetime.fromisoformat(route['signal_expires_at']), cutoff)
        self.assertFalse(pending_events(self.conn, 'codex', cutoff))

    def test_observation_without_quantity_carries_confirmation_and_candidate_deadline(self):
        self.update_quote(0, 801)
        cutoff = START + timedelta(minutes=6)
        original = self.publish_watch(1, START, 799, candidate_until=START + timedelta(minutes=14))
        self.observe_watch(original, [(1, 799), (2, 803.5), (4, 804), (5, 804.1)])
        at = START + timedelta(minutes=5, seconds=10)
        successor = self.publish_watch(2, at, 799, candidate_until=cutoff, review_grams=None)
        self.assertNotEqual(successor['buy_watch']['id'], original['buy_watch']['id'])
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        event, = pending_events(self.conn, 'codex', at)
        review, = event['data']['routes']
        self.assertEqual(event['data']['advice_id'], 2)
        self.assertIsNone(review['buy_review']['grams'])
        self.assertEqual(datetime.fromisoformat(review['confirmed_at']), START + timedelta(minutes=5))
        self.assertEqual(datetime.fromisoformat(review['signal_expires_at']), cutoff)
        self.assertEqual(pending_events(self.conn, 'codex', cutoff), [])

    def test_retest_can_trigger_after_continuation_across_advice_in_the_same_move(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5), (1, 806.3), (3, 807)])
        first, = pending_events(self.conn, 'codex', START + timedelta(minutes=3))
        continuation, = first['data']['routes']
        self.assertEqual(continuation['confirmation_mode'], 'continuation')
        context = {'trigger_events': [first], 'live_readonly_accounts': {'accounts': {
            'zheshang': {'revision': advice['snapshot_revision']}}}}
        with self.conn:
            remember_reviewed_breakouts(self.conn, 'codex', context, [first['id']], 0)
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])

        self.observe_watch(advice, [(4, 805.6)])
        at = START + timedelta(minutes=4)
        from monitor.jd_live_readonly import model_input
        previous = model_input(self.conn, at)['accounts']['zheshang']['previous_advice'][0]
        breakout = previous['review_conditions'][-1]
        self.assertEqual(breakout['monitoring'], {
            'continuation': {'status': 'reviewed', 'confirmation_stage': 'confirmed'},
            'retest': {'status': 'monitoring', 'confirmation_stage': 'holding'},
        })
        self.assertEqual(breakout['bank_breakout']['continuation_min_quotes'], 3)
        successor = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.observe_watch(successor, [(5, 805.5), (7, 805.7)])
        now = START + timedelta(minutes=7)
        event, = pending_events(self.conn, 'codex', now)
        retest, = event['data']['routes']
        self.assertEqual(retest['confirmation_mode'], 'retest')
        self.assertNotEqual(retest['condition_key'], continuation['condition_key'])
        self.assertEqual(retest['price_cny'], 805.7)
        self.assertEqual(retest['signal_expires_at'], advice['buy_watch']['expires_at'])
        account = accounts(self.conn, now, include_advice=False)[0]
        state = condition_state(self.conn, account, successor, successor['review_conditions'][-1], now)
        self.assertEqual((state['status'], state['confirmation_mode']), ('queued', 'retest'))

    def test_later_continuation_stays_pending_when_earlier_retest_is_acknowledged(self):
        self.update_quote(0, 805.7)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.7), (1, 805.4), (2, 805.45), (4, 805.45)])
        first, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        retest, = first['data']['routes']
        self.assertEqual(retest['confirmation_mode'], 'retest')
        self.observe_watch(advice, [(5, 806.2)])
        now = START + timedelta(minutes=5)
        event, = pending_events(self.conn, 'codex', now)
        self.assertEqual(event['id'], first['id'])
        self.assertEqual({r['confirmation_mode'] for r in event['data']['routes']}, {'retest', 'continuation'})
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            self.assertEqual(remove_submitted_confirmations(self.conn, 'codex', [first]), {first['id']})
        remaining, = pending_events(self.conn, 'codex', now)
        continuation, = remaining['data']['routes']
        self.assertEqual(continuation['confirmation_mode'], 'continuation')
        self.assertNotEqual(continuation['condition_key'], retest['condition_key'])

    def test_legacy_pending_retest_keeps_its_confirmation_when_loading_the_split_watch(self):
        self.update_quote(0, 805.7)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.7), (1, 805.4), (2, 805.45), (4, 805.45)])
        event_state = read(self.conn, 'codex')
        event, = event_state['pending']
        route, = event['data']['routes']
        key = 'live-buy-watch:codex:zheshang'
        watch = read_watch(self.conn, key)
        legacy_key = condition_key(advice['review_conditions'][-1])
        watch['routes'] = {legacy_key: watch['routes'][route['condition_key']]}
        route['condition_key'] = legacy_key
        self.store.set_state(key, watch)
        with self.conn:
            write(self.conn, 'codex', event_state)

        self.observe_watch(advice, [(5, 805.8)])
        now = START + timedelta(minutes=5)
        event, = pending_events(self.conn, 'codex', now)
        retest, = event['data']['routes']
        self.assertNotEqual(retest['condition_key'], legacy_key)
        self.assertEqual(retest['confirmed_at'], (START + timedelta(minutes=4)).isoformat(timespec='microseconds'))
        self.assertEqual(retest['price_cny'], 805.45)

    def test_pullback_can_trigger_after_breakout_was_reviewed_in_the_same_window(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5), (1, 806), (3, 806.5)])
        first, = read(self.conn, 'codex')['pending']
        self.assertEqual(first['data']['routes'][0]['confirmation_mode'], 'continuation')
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])

        self.observe_watch(advice, [(4, 799), (5, 803.5), (7, 804), (8, 804.1)])
        now = START + timedelta(minutes=8)
        second, = pending_events(self.conn, 'codex', now)
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(second['data']['plan_id'], first['data']['plan_id'])
        condition = advice['review_conditions'][0]
        route, = second['data']['routes']
        self.assertEqual(route['condition_key'], condition_key(condition))
        account = accounts(self.conn, now, include_advice=False)[0]
        state = condition_state(self.conn, account, advice, condition, now)
        self.assertEqual((state['status'], state['event_id']), ('queued', second['id']))
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [second])
            acknowledge(self.conn, 'codex', [second['id']])
        state = condition_state(self.conn, account, advice, condition, now)
        self.assertEqual((state['status'], state['event_id']), ('reviewed', second['id']))

        self.observe_watch(advice, [(9, 802.8), (10, 804), (12, 804), (13, 804.1)])
        self.assertEqual(read(self.conn, 'codex'), {'pending': [], 'covered': [first['id'], second['id']]})

    def test_continuation_can_trigger_after_a_pullback_review_without_a_new_window(self):
        self.update_quote(0, 799)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 799), (1, 803.5), (3, 804), (4, 804.1)])
        first, = read(self.conn, 'codex')['pending']
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])
        now = START + timedelta(minutes=4)
        account = accounts(self.conn, now, include_advice=False)[0]
        state = condition_state(self.conn, account, advice, advice['review_conditions'][-1], now)
        self.assertEqual(state['status'], 'monitoring')

        self.observe_watch(advice, [(5, 805.5), (6, 806), (8, 806.5), (9, 806.6)])
        second, = pending_events(self.conn, 'codex', START + timedelta(minutes=9))
        route, = second['data']['routes']
        self.assertEqual(route['confirmation_mode'], 'continuation')
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(second['data']['plan_id'], first['data']['plan_id'])

    def test_pending_breakout_survives_a_change_to_an_independent_pullback(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806), (3, 806.5)])
        old_event, = read(self.conn, 'codex')['pending']
        at = START + timedelta(minutes=4)
        second = self.publish_watch(2, at, 800)
        self.assertNotEqual(first['buy_watch']['id'], second['buy_watch']['id'])
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        carried, = pending_events(self.conn, 'codex', at)
        self.assertEqual(carried['data']['plan_id'], second['buy_watch']['id'])
        self.assertNotEqual(carried['id'], old_event['id'])
        self.assertEqual(carried['data']['routes'], old_event['data']['routes'])
        self.assertEqual(read_watch(self.conn, 'live-buy-watch:codex:zheshang')['plan_id'], second['buy_watch']['id'])

        self.observe_watch(second, [(4, 806.6)])
        self.assertEqual(pending_events(self.conn, 'codex', at), [carried])

        later = START + timedelta(minutes=5)
        self.publish_watch(3, later, 800, 794)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 3, later)
        self.assertEqual(pending_events(self.conn, 'codex', later), [])

    def test_two_breakout_quotes_survive_a_change_to_an_independent_pullback(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

        at = START + timedelta(minutes=2)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        step = read_watch(self.conn, 'live-buy-watch:codex:zheshang')['routes'][condition_key(second['review_conditions'][-1])]
        self.assertEqual(step['breakout_quote_count'], 2)
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.observe_watch(second, [(3, 806.5)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=3))
        self.assertEqual(event['data']['plan_id'], second['buy_watch']['id'])
        self.assertEqual(event['data']['routes'][0]['confirmation_mode'], 'continuation')
        self.assertEqual(datetime.fromisoformat(event['data']['routes'][0]['touched_at']), START)

    def test_live_monitor_checks_a_completed_sync_during_the_idle_interval(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5), (1, 806)])
        monitor = Monitor(self.settings, True)
        self.addCleanup(monitor.store.conn.close)
        monitor.analysts = {'codex': Mock()}
        monitor.store.set_state('ai_status:codex', {'available': True})
        monitor.market.snapshot = Mock()
        monitor.dispatch = Mock()
        elapsed = 0
        arrived = START + timedelta(minutes=3)
        starts = []

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds
            if elapsed == 5:
                monitor.store.set_state('jd_live_sync_status', {'status': 'running', 'at': arrived.isoformat()})
            if elapsed == 10:
                self.update_quote(3, 806.5)
                monitor.store.set_state('jd_live_sync_status', {'status': 'completed', 'at': arrived.isoformat()})

        def schedule(_):
            event, = [event for event in read(monitor.store.conn, 'codex')['pending']
                      if event['id'].startswith('live-buy-confirmed:')]
            starts.append((elapsed, event['data']['routes'][0]['confirmation_mode']))
            return []

        monitor.run_ai_if_due = Mock(side_effect=schedule)
        with patch('monitor.__main__.time.monotonic', side_effect=lambda: elapsed), \
                patch('monitor.__main__.time.sleep', side_effect=sleep), \
                patch('monitor.__main__.utc_now', side_effect=lambda: arrived + timedelta(seconds=elapsed)):
            monitor.wait_for_next_tick(60, {'market_open': True, 'xauusd': 4000})
        self.assertEqual(starts, [(10, 'continuation')])
        self.assertEqual(elapsed, 60)
        monitor.market.snapshot.assert_not_called()
        monitor.dispatch.assert_not_called()

    def test_live_watch_uses_the_quote_that_confirmed_between_monitor_checks(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5)])
        self.update_quote(1, 805.9)
        self.update_quote(3, 806.5)
        self.observe_watch(advice, [(4, 806.4)])

        now = START + timedelta(minutes=4)
        event, = pending_events(self.conn, 'codex', now)
        route, = event['data']['routes']
        self.assertEqual(route['confirmation_mode'], 'continuation')
        self.assertEqual(datetime.fromisoformat(route['confirmed_at']), START + timedelta(minutes=3))
        self.assertEqual(route['price_cny'], 806.5)
        self.observe_watch(advice, [(4, 806.4)])
        self.assertEqual(pending_events(self.conn, 'codex', now), [event])

    def test_live_watch_restarts_after_an_intervening_breakout_failure(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5), (1, 806)])
        self.update_quote(2, 804.9)
        self.observe_watch(advice, [(3, 806.5)])

        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        watch = read_watch(self.conn, 'live-buy-watch:codex:zheshang')
        step = watch['routes'][condition_key(advice['review_conditions'][-1])]
        self.assertEqual(datetime.fromisoformat(step['touched_at']), START + timedelta(minutes=3))
        self.assertEqual(step['breakout_quote_count'], 1)

    def test_live_watch_remains_invalid_after_an_intervening_exit_boundary_hit(self):
        self.update_quote(0, 805.5)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 805.5), (1, 806), (3, 806.5)])
        self.assertEqual(len(read(self.conn, 'codex')['pending']), 1)
        self.update_quote(4, 794)
        self.observe_watch(advice, [(5, 806.4), (6, 807), (8, 807.2)])

        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        watch = read_watch(self.conn, 'live-buy-watch:codex:zheshang')
        self.assertEqual(datetime.fromisoformat(watch['invalidated_at']), START + timedelta(minutes=4))

    def test_delayed_quote_cannot_start_a_condition_published_after_its_timestamp(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806)])
        at = START + timedelta(minutes=1, seconds=30)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.update_quote(1.25, 799)
        self.observe_watch(second, [(2, 803.6), (4, 804), (5, 804.2)])

        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        watch = read_watch(self.conn, 'live-buy-watch:codex:zheshang')
        self.assertNotIn('stage', watch['routes'][condition_key(second['review_conditions'][0])])

    def test_quote_recorded_during_plan_change_can_finish_breakout_confirmation(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806)])
        self.update_quote(3, 806.5)

        at = START + timedelta(minutes=3, seconds=10)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        event, = pending_events(self.conn, 'codex', at)
        self.assertEqual(event['data']['plan_id'], second['buy_watch']['id'])
        route, = event['data']['routes']
        self.assertEqual(datetime.fromisoformat(route['confirmed_at']), START + timedelta(minutes=3))
        self.assertEqual(route['price_cny'], 806.5)
        self.observe_watch(second, [(4, 806.8)])
        self.assertEqual(pending_events(self.conn, 'codex', START + timedelta(minutes=4)), [event])

    def test_new_retest_plan_preserves_pullback_progress_when_only_its_explanation_changes(self):
        self.update_quote(0, 799)
        first = self.publish_watch(1, START, 799, retest_price=805)
        self.observe_watch(first, [(0, 799), (1, 803.5), (3, 804)])

        at = START + timedelta(minutes=3, seconds=10)
        second = self.publish_watch(2, at, 799, retest_price=806, basis='从浙商近一小时数据得出。')
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        account = accounts(self.conn, at, include_advice=False)[0]
        state = condition_state(self.conn, account, second, second['review_conditions'][0], at)
        original_end = datetime.fromisoformat(first['buy_watch']['expires_at'])
        self.assertEqual(state['confirmation_stage'], 'holding')
        self.assertEqual(datetime.fromisoformat(state['expires_at']), original_end)

        self.observe_watch(second, [(4, 804.1)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        route, = event['data']['routes']
        self.assertEqual(event['data']['plan_id'], second['buy_watch']['id'])
        self.assertEqual(datetime.fromisoformat(route['hold_since']), START + timedelta(minutes=1))
        self.assertEqual(datetime.fromisoformat(route['signal_expires_at']), original_end)

    def test_one_queued_retest_survives_when_its_queued_sibling_changes(self):
        self.update_quote(0, 799)
        first = self.publish_watch(1, START, 799, retest_price=805)
        self.observe_watch(first, [(0, 799), (1, 805.1), (2, 804), (4, 804.1), (5, 804.2)])
        event, = read(self.conn, 'codex')['pending']
        self.assertEqual(len(event['data']['routes']), 2)

        at = START + timedelta(minutes=6)
        second = self.publish_watch(2, at, 800, retest_price=805, basis='从浙商近一小时数据得出。')
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        queued, = pending_events(self.conn, 'codex', at)
        self.assertEqual(queued['data']['plan_id'], second['buy_watch']['id'])
        route, = queued['data']['routes']
        self.assertEqual(route, next(r for r in event['data']['routes'] if r['condition'] == 'at_or_above'))

    def test_changed_buy_cap_requires_a_new_confirmation(self):
        self.update_quote(0, 799)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 799), (1, 803.5), (4, 804)])
        self.assertEqual(len(read(self.conn, 'codex')['pending']), 1)

        at = START + timedelta(minutes=5)
        second = self.publish_watch(2, at, 799, max_buy_price=805.5)
        self.assertNotEqual(first['buy_watch']['id'], second['buy_watch']['id'])
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.assertEqual(pending_events(self.conn, 'codex', at), [])

    def test_late_quote_cannot_complete_a_carried_pullback_after_the_old_deadline(self):
        self.update_quote(0, 802)
        first = self.publish_watch(1, START, 799, retest_price=805)
        self.observe_watch(first, [(56, 799), (57.5, 803.5), (59, 804)])
        at = START + timedelta(minutes=59, seconds=10)
        second = self.publish_watch(2, at, 799, retest_price=806)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.observe_watch(second, [(60.5, 804.1)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        state = read_watch(self.conn, 'live-buy-watch:codex:zheshang')
        self.assertNotIn('stage', state['routes'][condition_key(second['review_conditions'][0])])

    def test_same_price_with_a_changed_exit_scope_does_not_carry_a_confirmation(self):
        self.update_quote(0, 799)
        first = self.publish_watch(1, START, 799, exit_scope='tranche')
        self.observe_watch(first, [(0, 799), (1, 803.5), (4, 804)])
        self.assertEqual(len(read(self.conn, 'codex')['pending']), 1)

        at = START + timedelta(minutes=5)
        second = self.publish_watch(2, at, 799, exit_scope='whole_position')
        self.assertNotEqual(first['buy_watch']['id'], second['buy_watch']['id'])
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.assertEqual(pending_events(self.conn, 'codex', at), [])

    def test_breakout_progress_is_reset_after_a_quote_gap(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806)])

        at = START + timedelta(minutes=5)
        self.update_quote(5, 806.5)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.observe_watch(second, [(5, 806.5)])
        step = read_watch(self.conn, 'live-buy-watch:codex:zheshang')['routes'][condition_key(second['review_conditions'][-1])]
        self.assertEqual(step['breakout_quote_count'], 1)
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

    def test_breakout_progress_restarts_after_a_recorded_drop(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5)])
        self.update_quote(.5, 804.9)
        self.observe_watch(first, [(1, 806)])

        at = START + timedelta(minutes=2)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        self.observe_watch(second, [(3, 806.5)])
        step = read_watch(self.conn, 'live-buy-watch:codex:zheshang')['routes'][condition_key(second['review_conditions'][-1])]
        self.assertEqual(step['breakout_quote_count'], 2)
        self.assertEqual(datetime.fromisoformat(step['touched_at']), START + timedelta(minutes=1))
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

    def test_switch_replay_uses_the_high_of_all_intervening_quotes(self):
        self.update_quote(0, 805.5)
        first = self.publish_watch(1, START, 799)
        self.observe_watch(first, [(0, 805.5), (1, 806)])
        self.update_quote(2, 807)
        self.update_quote(3, 806.5)

        at = START + timedelta(minutes=3, seconds=10)
        second = self.publish_watch(2, at, 800)
        with self.conn:
            carry_entry_observations(self.conn, 'codex', 2, at)
        step = read_watch(self.conn, 'live-buy-watch:codex:zheshang')['routes'][condition_key(second['review_conditions'][-1])]
        self.assertEqual(step['peak'], 807)
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

        self.observe_watch(second, [(4, 807.1)])
        event, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        self.assertEqual(event['data']['routes'][0]['confirmation_mode'], 'continuation')

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

    def test_same_watch_reviews_a_new_breakout_only_after_a_verified_recross(self):
        from monitor.jd_live_buy_guard import remember_reviewed_confirmations

        self.update_quote(0, 806.4)
        advice = self.publish_watch(1, START, 799)
        self.observe_watch(advice, [(0, 806.4), (1, 805.2), (2, 805.3), (4, 806.5)])
        first, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        self.assertEqual({r['confirmation_mode'] for r in first['data']['routes']}, {'continuation', 'retest'})
        context = {'trigger_events': [first], 'live_readonly_accounts': {'accounts': {
            'zheshang': {'revision': advice['snapshot_revision']}}}}
        with self.conn:
            remember_reviewed_breakouts(self.conn, 'codex', context, [first['id']], 0)
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])

        # A feed gap and renewed upward motion above the old level are still silent.
        self.observe_watch(advice, [(5, 806.6), (9, 806.6), (10, 805.3), (11, 805.4), (13, 806.7)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.observe_watch(advice, [(14, 805), (15, 806.7)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

        self.update_quote(16, 804.95)
        self.observe_watch(advice, [(17, 806.7), (18, 805.2), (19, 805.4), (21, 806.9)])
        again, = pending_events(self.conn, 'codex', START + timedelta(minutes=21))
        self.assertEqual(again['data']['plan_id'], first['data']['plan_id'])
        self.assertNotEqual(again['id'], first['id'])
        self.assertEqual({r['confirmation_mode'] for r in again['data']['routes']}, {'continuation', 'retest'})
        self.assertTrue(all(datetime.fromisoformat(r['touched_at']) == START + timedelta(minutes=17)
                            for r in again['data']['routes']))
        self.observe_watch(advice, [(22, 807.0)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [again])

    def test_same_watch_can_review_a_new_pullback_after_cap_invalidation(self):
        from monitor.jd_live_buy_guard import remember_reviewed_confirmations

        self.update_quote(0, 799.5)
        advice = self.publish_watch(1, START, 800)
        self.observe_watch(advice, [(0, 799.5), (1, 803.5), (4, 804)])
        first, = pending_events(self.conn, 'codex', START + timedelta(minutes=4))
        with self.conn:
            remember_reviewed_confirmations(self.conn, 'codex', [first])
            acknowledge(self.conn, 'codex', [first['id']])

        self.observe_watch(advice, [(8, 807), (9, 803.5), (10, 804)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.update_quote(11, 799.5)
        self.observe_watch(advice, [(12, 803.5), (14, 804)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.observe_watch(advice, [(15, 804.2)])
        again, = pending_events(self.conn, 'codex', START + timedelta(minutes=15))
        again_route, = again['data']['routes']
        self.assertEqual(again['data']['plan_id'], first['data']['plan_id'])
        self.assertNotEqual(again['id'], first['id'])
        self.assertEqual(datetime.fromisoformat(again_route['touched_at']), START + timedelta(minutes=11))

        self.observe_watch(advice, [(16, 796.5), (17, 799.5), (18, 803.5), (21, 804)])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

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

    def test_replaced_buy_threshold_is_rechecked_immediately_before_model_dispatch(self):
        self.update_quote(0, 806)
        revision = accounts(self.conn, START, include_advice=False)[0]['revision']
        condition = {'condition': 'at_or_above', 'quote_side': 'buy', 'price_cny': 805}
        expires = (START + timedelta(minutes=15)).isoformat()
        with self.conn:
            self.conn.execute('INSERT INTO jd_live_advice VALUES(1,?,?,?,?,?,?,?)',
                ('zheshang', 'codex', 1, START.isoformat(), expires, revision,
                 json.dumps({'review_conditions': [condition]})))
        fingerprint = condition_key(condition)
        key = f'live-price:codex:zheshang:1:{fingerprint}'
        enqueue(self.conn, 'codex', '普通买价复评', 0, key, {'bank': 'zheshang', 'advice_id': 1,
                'condition_key': fingerprint, 'quote_side': 'buy', 'quoted_at': START.isoformat()})
        m = Monitor(self.settings, True)
        self.addCleanup(m.store.conn.close)
        analyst = AIAnalyst(self.settings, m.store, 'codex', 'codex')
        analyst.analyze = Mock()
        m.analysts = {'codex': analyst}
        now = START + timedelta(minutes=1)

        def assemble(*_):
            with self.conn:
                self.conn.execute('INSERT INTO jd_live_advice VALUES(2,?,?,?,?,?,?,?)',
                    ('zheshang', 'codex', 2, now.isoformat(), expires, revision, '{"review_conditions":[]}'))
            return {}

        with patch.object(m, 'ai_context', side_effect=assemble), \
                patch('monitor.jd_live_preflight.prepare', return_value={'can_analyze': True, 'status': 'ready'}), \
                patch('monitor.__main__.utc_now', return_value=now), patch('monitor.ai.utc_now', return_value=now):
            m._ai_worker({}, {}, {'codex': '已进入队列'}, automatic={'codex'})
        analyst.analyze.assert_not_called()
        self.assertEqual(read(self.conn, 'codex')['pending'], [])

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
        context, key = self.context([self.route(position_boundary_cny=805.5)])
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

        def backend(model_context, _):
            self.assertNotIn('position_boundary_cny', model_context['trigger_events'][0]['data']['routes'][0])
            self.update_quote(1, 806, sell_price=805.5)
            self.update_quote(2, 806.1)
            return 'notes', [], response

        with patch.object(analyst, '_run_codex', side_effect=backend), patch('monitor.ai.utc_now', return_value=finish), \
                patch('monitor.jd_live_advice.datetime') as clock:
            clock.now.return_value = finish
            view = analyst.analyze(context, '突破条件', run_id='guard-fixture')
        latest = accounts(self.conn, finish, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(latest['action_status'], 'review_required')
        self.assertEqual(latest['checks'], ['分析期间买入触发已到期、失守、超价、触及退出边界或报价中断，须按新行情复评'])
        self.assertEqual(notification_events(self.conn, 'codex', view['_view_id']), [])
        self.assertNotIn(key, read(self.conn, 'codex')['covered'])
        self.assertEqual(read(self.conn, 'codex')['pending'], [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0], 1)

    def save_advice(self, action='buy', condition='at_or_below', orders=(), routes=None, view=1, *, basis=None, now=START,
                    price_events=(), management=()):
        raw = {'action': action, 'grams': 10 if action == 'buy' else None,
               'price_condition': condition, 'price_cny': 806 if condition != 'none' else None,
               'valid_for_minutes': 15, 'reason': '本轮按实际报价评估。', 'risk_note': '先核对实时买价。',
               'suggested_orders': list(orders), 'order_management': list(management), 'entry_trigger_key': basis}
        with self.conn:
            save(self.conn, 'codex', view, {'zheshang': raw}, model_input(self.conn, now), now,
                 entry_routes={'zheshang': routes} if routes else None, submitted_price_events=price_events)

    def test_named_plain_price_touch_must_still_hold_for_a_buy(self):
        self.update_quote(0,806)
        revision=accounts(self.conn,START,include_advice=False)[0]['revision']
        condition={'condition':'at_or_above','quote_side':'buy','price_cny':805}
        fingerprint=condition_key(condition)
        with self.conn:
            self.conn.execute('INSERT INTO jd_live_advice VALUES(?,?,?,?,?,?,?,?)',
                (1,'zheshang','codex',1,START.isoformat(),(START+timedelta(minutes=5)).isoformat(),
                 revision,json.dumps({'action':'hold','review_conditions':[condition]})))
        event={'id':f'live-price:codex:zheshang:1:{fingerprint}','data':{
            'bank':'zheshang','advice_id':1,'condition_key':fingerprint,'quote_side':'buy',
            'threshold_cny':805,'price_cny':806,'quoted_at':START.isoformat()}}

        self.save_advice(view=2,basis=fingerprint,price_events=[event])
        current=accounts(self.conn,START,selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(current['action_status'],'current')
        self.assertTrue(current['entry_trigger_routes'][0]['price_touch'])

        self.update_quote(1,804.9)
        at=START+timedelta(minutes=1)
        current=accounts(self.conn,at,selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(current['action_status'],'review_required')
        self.update_quote(2,806)
        at=START+timedelta(minutes=2)
        self.save_advice(view=3,basis=fingerprint,price_events=[event],now=at)
        current=accounts(self.conn,at,selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(current['action_status'],'review_required')
        self.assertIn('本轮加仓引用的复评分支已失效，须重新确认',current['checks'])
        self.save_advice(view=4,price_events=[event],now=at)
        independent=accounts(self.conn,at,selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(independent['action_status'],'current')

    def test_mixed_confirmations_require_a_still_valid_buy_basis_at_publication(self):
        self.update_quote(0, 806)
        routes = [self.route(805), self.route(803)]
        self.update_quote(1, 804.9)
        now = START + timedelta(minutes=1)
        for view, (basis, expected) in enumerate(((None, 'review_required'), ('805', 'review_required'),
                                                 ('unknown', 'review_required'), ('803', 'current')), start=1):
            with self.subTest(basis=basis):
                self.save_advice(routes=routes, basis=basis, view=view, now=now)
                advice = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
                self.assertEqual(advice['action_status'], expected)
                self.assertEqual(bool(notification_events(self.conn, 'codex', view, now=now)), expected == 'current')
        self.assertEqual(advice['entry_trigger_routes'], [routes[1]])

    def test_display_rechecks_only_the_selected_buy_basis_or_requires_all_unselected_routes(self):
        self.update_quote(0, 806)
        routes = [self.route(805), self.route(803)]
        self.save_advice(routes=routes)
        self.update_quote(1, 804.9)
        now = START + timedelta(minutes=1)
        advice = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(advice['action_status'], 'review_required')
        self.save_advice(routes=routes, basis='803', view=2, now=now)
        advice = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(advice['action_status'], 'current')
        self.update_quote(2, 802.9)
        advice = accounts(self.conn, START + timedelta(minutes=2), selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(advice['action_status'], 'review_required')

    def test_published_buy_is_paused_at_its_trigger_deadline(self):
        self.update_quote(0, 806)
        expires = START + timedelta(minutes=1)
        self.save_advice(routes=[self.route(signal_expires_at=expires.isoformat())])
        self.assertEqual(len(notification_events(self.conn, 'codex', 1, now=START)), 1)

        self.update_quote(1, 806)
        advice = accounts(self.conn, expires, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(advice['action_status'], 'review_required')
        self.assertIn('到期', advice['entry_pause_reason'])
        self.assertEqual(notification_events(self.conn, 'codex', 1, now=expires), [])

    def test_price_limit_is_rechecked_without_rewriting_original_advice(self):
        self.update_quote(0, 806)
        self.save_advice()
        self.update_quote(1, 806.01)
        at = START + timedelta(minutes=1)
        a = accounts(self.conn, at, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(a['action_status'], 'review_required')
        self.assertIn('超过建议上限', a['entry_pause_reason'])
        self.assertEqual(notification_events(self.conn, 'codex', 1, now=at), [])
        self.update_quote(2, 806)
        at = START + timedelta(minutes=2)
        a = accounts(self.conn, at, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(a['action_status'], 'current')
        self.assertEqual(a['checks'], [])
        self.assertEqual(len(notification_events(self.conn, 'codex', 1, now=at)), 1)

    def test_lower_trigger_is_enforced_but_reference_price_is_only_context(self):
        self.update_quote(0, 806)
        self.save_advice(condition='at_or_above')
        self.update_quote(1, 805.99)
        at = START + timedelta(minutes=1)
        a = accounts(self.conn, at, selected_bank='zheshang')[0]['advice'][0]
        self.assertIn('低于建议触发价', a['entry_pause_reason'])
        self.update_quote(0, 806)
        self.save_advice(condition='reference', view=2)
        self.update_quote(2, 806.01)
        a = accounts(self.conn, START + timedelta(minutes=2), selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(a['action_status'], 'current')
        self.assertNotIn('entry_pause_reason', a)

    def test_notification_retry_is_cancelled_if_live_buy_price_is_outside_limit(self):
        self.update_quote(0, 806)
        self.save_advice()
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.side_effect = [False, True]
        notifier.last_receipt = {'status': 'failed'}
        events = notification_events(self.conn, 'codex', 1, now=START)
        self.assertEqual(len(events), 1)
        dispatch(self.store, notifier, events, START)
        self.update_quote(1, 806.01)
        dispatch(self.store, notifier, [], START + timedelta(seconds=61))
        notifier.send.assert_called_once()
        self.assertEqual(tuple(self.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone()), ('cancelled', 1))

    def test_retest_only_buy_without_a_limit_pauses_above_the_input_band(self):
        from monitor.jd_live_review import request_reviews

        rule = {'hold_minutes': 3, 'retest_ceiling_cny': 806}
        retest = self.route(condition_key='retest', bank_breakout=rule, confirmation_mode='retest')
        self.update_quote(0, 805.8)
        self.save_advice(condition='reference', routes=[retest], basis='retest')
        self.assertEqual(len(notification_events(self.conn, 'codex', 1, now=START)), 1)
        self.update_quote(1, 806.1)
        now = START + timedelta(minutes=1)
        view = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(view['action_status'], 'review_required')
        self.assertIn('涨出本轮分析的回踩区', view['entry_pause_reason'])
        waiting, = notification_events(self.conn, 'codex', 1, now=now)
        self.assertTrue(waiting['live_buy_deferred'])
        self.store.set_state('ai_status:codex', {'available': True})
        request_reviews(self.store, now, settings=self.settings)
        request_reviews(self.store, now, settings=self.settings)
        event, = pending_events(self.conn, 'codex', now)
        self.assertEqual(event['id'], 'live-retest-reprice:codex:zheshang:1')

        self.update_quote(2, 806)
        now = START + timedelta(minutes=2)
        view = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(view['action_status'], 'current')
        self.assertEqual(len(notification_events(self.conn, 'codex', 1, now=now)), 1)
        self.assertEqual(pending_events(self.conn, 'codex', now), [])

    def test_retest_pause_defers_an_unsent_notice_and_resumes_inside_the_original_band(self):
        retest = self.route(condition_key='retest', bank_breakout={'hold_minutes': 3, 'retest_ceiling_cny': 806},
                            confirmation_mode='retest')
        self.update_quote(0, 805.8)
        self.save_advice(condition='reference', routes=[retest], basis='retest')
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.return_value = True
        self.update_quote(1, 806.1)
        at = START + timedelta(minutes=1)
        dispatch(self.store, notifier, notification_events(self.conn, 'codex', 1, now=at), at)
        notifier.send.assert_not_called()
        self.assertEqual(tuple(self.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone()), ('pending',0))

        self.update_quote(2, 805.9)
        dispatch(self.store, notifier, [], START + timedelta(minutes=2))
        notifier.send.assert_called_once()
        self.assertIn('买入 10克', notifier.send.call_args.args[1])
        self.assertEqual(tuple(self.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone()), ('accepted',1))

    def test_retest_pause_does_not_consume_retry_budget_or_extend_expiry(self):
        retest = self.route(condition_key='retest', bank_breakout={'hold_minutes': 3, 'retest_ceiling_cny': 806},
                            confirmation_mode='retest')
        self.update_quote(0, 805.8)
        self.save_advice(condition='reference', routes=[retest], basis='retest')
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.return_value = False
        notifier.last_receipt = {'status': 'failed'}
        dispatch(self.store, notifier, notification_events(self.conn, 'codex', 1, now=START), START)
        self.update_quote(1, 806.1)
        dispatch(self.store, notifier, [], START + timedelta(minutes=1))
        notifier.send.assert_called_once()
        status, attempts, expires = self.conn.execute('SELECT status,attempts,expires_at FROM notification_outbox').fetchone()
        self.assertEqual((status,attempts), ('retry',1))
        self.assertEqual(datetime.fromisoformat(expires), START + timedelta(minutes=15))

        for minute in range(2,15):
            self.update_quote(minute, 806.1)
        dispatch(self.store, notifier, [], START + timedelta(minutes=14))
        self.assertEqual(self.conn.execute('SELECT status FROM notification_outbox').fetchone()[0], 'retry')
        self.update_quote(15, 805.9)
        dispatch(self.store, notifier, [], START + timedelta(minutes=15))
        notifier.send.assert_called_once()
        self.assertEqual(tuple(self.conn.execute('SELECT status,attempts FROM notification_outbox').fetchone()), ('cancelled',1))

    def test_deferred_notice_does_not_suppress_a_new_review_at_the_same_terms(self):
        retest = self.route(condition_key='retest', bank_breakout={'hold_minutes': 3, 'retest_ceiling_cny': 806},
                            confirmation_mode='retest')
        self.update_quote(0, 805.8)
        self.save_advice(condition='reference', routes=[retest], basis='retest')
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.return_value = True
        self.update_quote(1, 806.1)
        at = START + timedelta(minutes=1)
        dispatch(self.store, notifier, notification_events(self.conn,'codex',1,now=at), at)
        notifier.send.assert_not_called()

        self.update_quote(2, 806.2)
        at = START + timedelta(minutes=2)
        self.save_advice(condition='reference', routes=[retest], basis='retest', now=at, view=2)
        new, = notification_events(self.conn,'codex',2,now=at)
        dispatch(self.store, notifier, [new], at)
        notifier.send.assert_called_once()
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT event_key,status FROM notification_outbox ORDER BY event_key')],
                         [('live-advice:1','cancelled'),('live-advice:2','accepted')])

    def test_paused_buy_keeps_protection_and_sends_one_buy_only_notice_on_recovery(self):
        from monitor.jd_live_advice import resumed_buy_notice

        until = (START + timedelta(hours=1)).isoformat()
        orders = [{'kind': 'limit_buy', 'grams': 10, 'price_cny': 805, 'expires_at': until, 'reason': '等待回调。'},
                  {'kind': 'take_profit_stop_loss', 'grams': 2, 'price_cny': 820, 'loss_price_cny': 790,
                   'expires_at': until, 'reason': '独立保护现有持仓。'}]
        old = {'order_key': 'b'*24, 'kind': 'limit_buy', 'action': 'buy', 'status': 'pending',
               'price_cny': 799, 'grams': 12, 'filled_grams': 0, 'expires_at': until, 'price_condition': 'at_or_below'}
        management = [{'order_key': old['order_key'], 'action': 'cancel', 'reason': '撤销后重新评估买入40克。'}]
        retest = self.route(condition_key='retest', bank_breakout={'hold_minutes': 3, 'retest_ceiling_cny': 806},
                            confirmation_mode='retest')
        self.update_quote(0, 805.8, pending=[old])
        self.save_advice(condition='reference', orders=orders, routes=[retest], basis='retest', management=management)
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.side_effect = [False, True, True]
        notifier.last_receipt = {'status': 'failed'}
        dispatch(self.store, notifier, notification_events(self.conn, 'codex', 1, now=START), START)
        self.assertIn('买入 10克', notifier.send.call_args.args[1])

        self.update_quote(1, 806.1, pending=[old])
        at = START + timedelta(minutes=1)
        partial, = notification_events(self.conn, 'codex', 1, now=at, include_recorded=True)
        self.assertNotIn('买入建议', partial['title'])
        dispatch(self.store, notifier, [], at)
        self.assertEqual(notifier.send.call_count, 2)
        text = notifier.send.call_args.args[1]
        self.assertIn('即时买入建议已暂停', text)
        self.assertIn('止损 ≤ 790元/克', text)
        self.assertIn('建议撤单', text)
        self.assertNotIn('买入 10克', text)
        self.assertNotIn('买入10克', text)
        self.assertNotIn('买入40克', text)

        dispatch(self.store, notifier, [], at)
        self.assertEqual(notifier.send.call_count, 2)
        self.update_quote(2, 805.9, pending=[old])
        at = START + timedelta(minutes=2)
        dispatch(self.store, notifier, [], at)
        dispatch(self.store, notifier, [], at)
        self.assertEqual(notifier.send.call_count, 3)
        text = notifier.send.call_args.args[1]
        self.assertIn('买入条件恢复', text)
        self.assertIn('买入 10克', text)
        self.assertNotIn('止损 ≤ 790元/克', text)
        self.assertNotIn('建议撤单', text)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT event_key,status FROM notification_outbox ORDER BY event_key')],
                         [('live-advice:1','accepted'), ('live-advice:1:buy-resumed','accepted')])
        self.update_quote(3, 805.9, pending=[old])
        at = START + timedelta(minutes=3)
        self.save_advice(action='hold', condition='none', view=2, now=at)
        self.assertIsNone(resumed_buy_notice(self.conn, 1, at))

    def test_retest_band_does_not_override_an_explicit_limit_or_broader_basis(self):
        rule = {'hold_minutes': 3, 'retest_ceiling_cny': 805.9}
        retest = self.route(condition_key='retest', bank_breakout=rule, confirmation_mode='retest')
        continuation = self.route(condition_key='continuation', bank_breakout=rule, confirmation_mode='continuation')
        self.update_quote(0, 805.8)
        self.save_advice(condition='at_or_below', routes=[retest], basis='retest')
        self.update_quote(1, 805.95)
        now = START + timedelta(minutes=1)
        view = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(view['action_status'], 'current')

        self.save_advice(condition='reference', routes=[retest], basis='retest', view=2, now=now)
        self.update_quote(2, 806.1)
        now = START + timedelta(minutes=2)
        view = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(view['action_status'], 'current')

        self.update_quote(3, 805.8)
        self.save_advice(condition='reference', routes=[retest, continuation], basis='continuation', view=3,
                         now=START + timedelta(minutes=3))
        self.update_quote(4, 806.3)
        view = accounts(self.conn, START + timedelta(minutes=4), selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(view['action_status'], 'current')

    def test_broken_entry_trigger_removes_unpublished_buy_draft_but_keeps_sell_notice(self):
        self.update_quote(0, 806)
        until = (START + timedelta(hours=1)).isoformat()
        orders = [{'kind': 'limit_buy', 'grams': 10, 'price_cny': 805, 'expires_at': until, 'reason': '观察后择价买入。'},
                  {'kind': 'limit_sell', 'grams': 2, 'price_cny': 820, 'expires_at': until, 'reason': '独立减仓。'}]
        route = self.route(805, bank_breakout={'hold_minutes': 3}, position_boundary_cny=805.5)
        self.save_advice('hold', 'none', orders, routes=[route])
        set_subscription(self.conn, 'codex:live_zheshang', True)
        events = notification_events(self.conn, 'codex', 1, now=START)
        self.assertEqual(len(events), 1)
        self.assertIn('买入10克', events[0]['body'])
        self.update_quote(1, 806, sell_price=805.5)
        self.update_quote(2, 806)
        notifier = Mock()
        notifier.send.return_value = True
        dispatch(self.store, notifier, events, START + timedelta(minutes=2))
        notifier.send.assert_called_once()
        text = notifier.send.call_args.args[1]
        self.assertIn('卖出2克', text)
        self.assertNotIn('买入10克', text)
        saved = self.conn.execute('SELECT body,delivered FROM alerts').fetchone()
        self.assertEqual(tuple(saved), (text, 1))

    def test_expired_buy_draft_is_removed_on_retry_without_losing_live_sell_draft(self):
        self.update_quote(0, 806)
        orders = [
            {'kind': 'limit_buy', 'grams': 10, 'price_cny': 805,
             'expires_at': (START + timedelta(minutes=1)).isoformat(), 'reason': '回调后择价买入。'},
            {'kind': 'limit_sell', 'grams': 2, 'price_cny': 820,
             'expires_at': (START + timedelta(hours=1)).isoformat(), 'reason': '反弹后独立减仓。'},
        ]
        self.save_advice('hold', 'none', orders)
        raw = self.conn.execute('SELECT advice_json FROM jd_live_advice').fetchone()[0]
        set_subscription(self.conn, 'codex:live_zheshang', True)
        notifier = Mock()
        notifier.send.side_effect = [False, True]
        notifier.last_receipt = {'status': 'failed'}
        dispatch(self.store, notifier, notification_events(self.conn, 'codex', 1, now=START), START)
        self.assertIn('买入10克', notifier.send.call_args.args[1])

        self.update_quote(1, 806)
        now = START + timedelta(minutes=1)
        live = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertEqual(live['action_status'], 'current')
        self.assertIn('本条预挂建议已到期', live['suggested_orders'][0]['checks'])
        self.assertEqual(live['suggested_orders'][0]['execution_progress']['status'], 'not_observed')
        dispatch(self.store, notifier, [], now)
        self.assertEqual(notifier.send.call_count, 2)
        self.assertNotIn('买入10克', notifier.send.call_args.args[1])
        self.assertIn('卖出2克', notifier.send.call_args.args[1])
        self.assertEqual(self.conn.execute('SELECT advice_json FROM jd_live_advice').fetchone()[0], raw)

    def test_draft_at_trigger_price_is_not_sent_as_future_order(self):
        self.update_quote(0, 806)
        orders = [{'kind': 'limit_buy', 'grams': 10, 'price_cny': 805,
                   'expires_at': (START + timedelta(hours=1)).isoformat(), 'reason': '回调后择价买入。'}]
        self.save_advice('hold', 'none', orders)
        event = notification_events(self.conn, 'codex', 1, now=START)
        self.assertEqual(len(event), 1)
        set_subscription(self.conn, 'codex:live_zheshang', True)
        self.update_quote(1, 805)
        now = START + timedelta(minutes=1)
        current = accounts(self.conn, now, selected_bank='zheshang')[0]['advice'][0]
        self.assertIn('当前已满足买入条件', current['suggested_orders'][0]['checks'][0])
        notifier = Mock()
        dispatch(self.store, notifier, event, now)
        notifier.send.assert_not_called()
