import unittest
from datetime import datetime
from unittest.mock import patch
from monitor.trading_hours import trading_session
from monitor.execution import execution_quote


class TradingHoursTests(unittest.TestCase):
    def test_boundaries_and_continuous_nights(self):
        cases={'2026-10-26T08:59:59+08:00':False,'2026-10-26T09:00:00+08:00':True,
               '2026-10-27T03:00:00+08:00':True,'2026-10-30T23:59:59+08:00':True,
               '2026-10-31T02:29:59+08:00':True,'2026-10-31T02:30:00+08:00':False,
               '2026-11-01T12:00:00+08:00':False,'2026-11-02T08:59:59+08:00':False}
        for stamp,want in cases.items():
            with self.subTest(stamp=stamp):self.assertEqual(trading_session(datetime.fromisoformat(stamp))['is_open'],want)
        self.assertEqual(trading_session(datetime.fromisoformat('2026-10-31T02:30:00+08:00'))['next_open'],'2026-11-02T09:00:00+08:00')

    def test_fresh_quote_cannot_execute_outside_session(self):
        for stamp,want in [('2026-10-31T02:30:00+08:00',False),('2026-10-26T09:00:00+08:00',True)]:
            now=datetime.fromisoformat(stamp)
            quote={'source':'jd_latest_price_api','price':855,'quote_at':stamp,'fetched_at':stamp}
            with patch('monitor.execution.utc_now',return_value=now):
                self.assertEqual(execution_quote({'execution_quote':quote}) is not None,want)
