import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitor.jd_desktop import (
    CASH_SOURCE, SELL_COUPON_POPUP, DesktopReader, empty_waiting_orders, parse_cash_panel, parse_product,
    process_review, request_review, review_state, sync_desktop, write_review,
)
from monitor.jd_live_readonly import accounts, ensure_schema, record_success, set_enabled_banks
from monitor.jd_desktop_prefill import arm, poll, trade_plan
from monitor.store import Store


class DesktopSnapshotTests(unittest.TestCase):
    def test_zero_balance_requires_bank_and_gold_electronic_account(self):
        labels = ["买入金额", "付款账户", "浙商银行", "(余额", "¥0.00", ")", "买金专户，交易无限额", "立即买入"]
        self.assertEqual(parse_cash_panel(labels, "zheshang"), 0)
        self.assertEqual(parse_cash_panel(["买入克重", *labels[1:]], "zheshang"), 0)
        self.assertIsNone(parse_cash_panel(labels, "minsheng"))
        labels[-2] = "绑定银行卡"
        self.assertIsNone(parse_cash_panel(labels, "zheshang"))

    def test_orders_ending_or_loading_does_not_count_as_empty(self):
        labels = ["限价买入", "限价卖出", "止盈止损", "等待中",
                  "还没有限价订单哦", "查看交易单历史记录"]
        self.assertTrue(empty_waiting_orders(labels))
        self.assertFalse(empty_waiting_orders(labels[:-2] + ["加载中"]))
        self.assertFalse(empty_waiting_orders(labels + ["撤销"]))

    def test_passive_poll_preserves_component_timestamp_but_not_web_data(self):
        with TemporaryDirectory() as root:
            store = Store(Path(root) / "monitor.db")
            try:
                ensure_schema(store.conn)
                set_enabled_banks(store.conn, ["zheshang"])
                reader = Mock()
                reader.read.side_effect = lambda banks, at: {
                    "zheshang": parse_product(
                        ["持仓克重", "1.0023", "成本均价(元/克)", "600", "浙商实时金价(元/克)", "610"],
                        "zheshang", at,
                    ),
                }
                captured = datetime.now(timezone.utc).isoformat()
                reader.refresh_panels.return_value = {"cash": (0.0, captured), "orders": captured}
                self.assertEqual(sync_desktop(store, reader, refresh_panels=True), "completed")
                sync_desktop(store, reader)
                row = accounts(store.conn, include_advice=False)[0]
                self.assertEqual(row["component_times"]["cash_balance"], captured)
                self.assertEqual(row["cash_source"], CASH_SOURCE)
                self.assertTrue(row["pending_orders"]["complete"])
                self.assertIsNone(row.get("quote"))
                self.assertEqual(row["display_price"]["price_cny"], 610)
                record_success(store.conn, "zheshang", {"cash_cny": 1000, "cash_source": "jd_electronic_account_api"})
                sync_desktop(store, reader)
                row = accounts(store.conn, include_advice=False)[0]
                self.assertIsNone(row.get("cash_cny"))
            finally:
                store.conn.close()


class DesktopReviewTests(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store = Store(Path(root.name) / "monitor.db")
        self.conn = self.store.conn
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        set_enabled_banks(self.conn, ["zheshang"])

    def test_one_request_is_opened_once_and_only_for_an_enabled_bank(self):
        with self.assertRaises(ValueError):
            request_review(self.conn, "minsheng", "market_sell")
        with self.assertRaises(ValueError):
            request_review(self.conn, "zheshang", "submit")
        review = request_review(self.conn, "zheshang", "market_sell")
        with self.assertRaises(RuntimeError):
            request_review(self.conn, "zheshang", "limit_sell")
        reader = Mock()
        reader.open_review.return_value = "applied"
        self.assertTrue(process_review(self.conn, reader))
        self.assertFalse(process_review(self.conn, reader))
        reader.open_review.assert_called_once_with("zheshang", "market_sell")
        self.assertEqual(review_state(self.conn)["id"], review["id"])
        self.assertEqual(review_state(self.conn)["status"], "opened")
        self.assertEqual(review_state(self.conn)["coupon"], "applied")

    def test_stale_navigation_requests_expire_and_unverified_pages_are_reported(self):
        review = request_review(self.conn, "zheshang", "pending_orders")
        now = datetime.now(timezone.utc)
        with self.conn:
            write_review(self.conn, {**review, "requested_at": (now - timedelta(minutes=1)).isoformat()}, now)
        reader = Mock()
        self.assertFalse(process_review(self.conn, reader))
        self.assertEqual(review_state(self.conn)["status"], "expired")
        reader.open_review.assert_not_called()
        request_review(self.conn, "zheshang", "limit_buy")
        reader.open_review.side_effect = ValueError("form not visible")
        self.assertTrue(process_review(self.conn, reader))
        self.assertEqual(review_state(self.conn)["status"], "unverified")

    def test_sell_coupon_uses_one_verified_choice_and_checks_the_result(self):
        prefix = SELL_COUPON_POPUP
        candidate = SimpleNamespace(CurrentClassName=f"{prefix}__item")
        unused = SimpleNamespace(CurrentClassName=f"{prefix}__item {prefix}__item--selected {prefix}__unused")
        reader = object.__new__(DesktopReader)
        reader.visible_controls = Mock(return_value=[candidate, unused])
        reader.invoke = Mock()
        row = object()
        reader.sell_coupon_row = Mock(return_value=row)
        reader.labels = Mock(side_effect=lambda item: ["7折", "手续费优惠", "有效期至 2026.12.31"]
                             if item is candidate else ["优惠", "手续费7折"])
        picker = object()
        self.assertEqual(reader.apply_sell_coupon("zheshang", picker), "applied")
        reader.invoke.assert_called_once_with(candidate)
        reader.invoke.reset_mock()
        reader.visible_controls.return_value = [candidate, candidate, unused]
        self.assertEqual(reader.apply_sell_coupon("zheshang", picker), "needs_review")
        reader.invoke.assert_not_called()

    def test_auto_prefill_uses_only_current_new_immediate_advice(self):
        state = arm(self.store)
        now = datetime.now(timezone.utc)
        account = {"source": "desktop", "connected": True, "fetched_at": now.isoformat(),
                   "revision": "current", "pending_orders": {"status": "fresh", "complete": True}}
        advice = {"id": 2, "action": "buy", "action_status": "current", "validation_version": 1,
                  "snapshot_revision": "current", "expires_at": (now + timedelta(minutes=10)).isoformat(),
                  "grams": 1.0025, "price_condition": "at_or_below", "price_cny": 650}
        account["advice"] = [advice]
        reader = Mock()
        reader.prepare_market_order.return_value = {"status": "prepared"}

        def save(view, stamp, action):
            with self.conn:
                return self.conn.execute(
                    "INSERT INTO jd_live_advice(bank,analyst,view_id,created_at,expires_at,snapshot_revision,advice_json) "
                    "VALUES('zheshang','codex',?,?,?,?,?)",
                    (view, stamp, advice["expires_at"], "current", json.dumps({"action": action})),
                ).lastrowid

        save(1, (now - timedelta(minutes=5)).isoformat(), "sell")
        with patch("monitor.jd_desktop_prefill.trading_session", return_value={"is_open": True}), \
                patch("monitor.jd_desktop_prefill.accounts", return_value=[account]), \
                patch("monitor.jd_desktop_prefill.review_context_current", return_value=True):
            poll(self.store, reader, state)
            reader.prepare_market_order.assert_not_called()
            self.assertEqual(save(2, now.isoformat(), "buy"), 2)
            poll(self.store, reader, state)
            poll(self.store, reader, state)
            reader.prepare_market_order.assert_called_once_with("zheshang", {
                "action": "buy", "grams": "1.0025", "price_condition": "at_or_below", "price_cny": 650,
            })
            self.assertEqual(state["banks"]["zheshang"]["status"], "prepared")
            save(3, now.isoformat(), "hold")
            poll(self.store, reader, state)
            self.assertEqual(state["banks"]["zheshang"]["status"], "no_trade")
            self.assertEqual(reader.prepare_market_order.call_count, 1)
            self.assertEqual(trade_plan(account, {**advice, "grams": 1.00001}, now)[1], "quantity_unavailable")
            self.assertEqual(trade_plan(account, {**advice, "action_status": "review_required"}, now)[1], "advice_unavailable")

    def test_prepare_buy_writes_only_the_number_and_reads_back_each_digit(self):
        reader = object.__new__(DesktopReader)
        dialog, field = object(), object()
        reader.landing_page = Mock()
        reader.open_review = Mock(return_value="grams")
        reader.sell_coupon_picker = Mock(return_value=None)
        reader.form_price_matches = Mock(return_value=True)
        reader.gram_field = Mock(return_value=(dialog, field))
        reader.labels = Mock(side_effect=lambda value: ["1克起购，0.0001克递增"] if value is field else [
            "买入克重", "浙商实时金价(元/克)", "610", "付款账户", "浙商银行", "(余额", "¥1000",
            ")", "买金专户，交易无限额", "立即买入",
        ])
        reader.numeric_key = Mock(side_effect=lambda bank, action, character: character)
        reader.invoke = Mock()
        reader.close_market_panel = Mock()
        reader.gram_input_labels = Mock(side_effect=[["1"], ["1."], ["1.2"]])
        reader.buy_payable = Mock(return_value=Decimal("732"))
        plan = {"action": "buy", "grams": "1.2", "price_condition": "at_or_below", "price_cny": 615}
        self.assertEqual(reader.prepare_market_order("zheshang", plan), {"status": "prepared"})
        self.assertEqual([call.args[0] for call in reader.invoke.call_args_list], [field, "1", ".", "2"])
        reader.close_market_panel.assert_not_called()
