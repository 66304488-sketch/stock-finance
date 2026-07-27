import unittest
from datetime import datetime

import server


class MarketSessionTest(unittest.TestCase):
    def phase(self, hour, minute):
        return server._session_phase(datetime(2026, 7, 15, hour, minute))

    def test_refresh_window_matches_market_sessions(self):
        self.assertEqual(self.phase(9, 24), "preopen")
        self.assertEqual(self.phase(9, 25), "trading")
        self.assertEqual(self.phase(11, 30), "trading")
        self.assertEqual(self.phase(11, 31), "lunch")
        self.assertEqual(self.phase(12, 59), "lunch")
        self.assertEqual(self.phase(13, 0), "trading")
        self.assertEqual(self.phase(15, 5), "trading")
        self.assertEqual(self.phase(15, 6), "awaiting_close")
        self.assertEqual(self.phase(17, 31), "closed")

    def test_weekend_is_closed(self):
        self.assertEqual(server._session_phase(datetime(2026, 7, 18, 10, 0)), "closed")

    def test_continuous_trading_excludes_call_auction_and_close_buffer(self):
        self.assertFalse(server._is_continuous_trading(datetime(2026, 7, 15, 9, 25)))
        self.assertTrue(server._is_continuous_trading(datetime(2026, 7, 15, 9, 30)))
        self.assertTrue(server._is_continuous_trading(datetime(2026, 7, 15, 14, 59)))
        self.assertFalse(server._is_continuous_trading(datetime(2026, 7, 15, 15, 1)))


if __name__ == "__main__":
    unittest.main()
