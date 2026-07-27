import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

import scan_intraday


class IntradayScanTests(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range(end=pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=1), periods=30)
        self.ohlcv = {
            "000001": pd.DataFrame({
                "date": dates, "open": 9.0, "high": 10.0,
                "low": 8.0, "close": 9.0, "volume": 100,
            }),
            "000002": pd.DataFrame({
                "date": dates, "open": 9.0, "high": 10.0,
                "low": 8.0, "close": 9.0, "volume": 100,
            }),
        }
        self.spots = {
            "000001": {
                "name": "触及回落", "close": 8.8, "high": 11.0, "low": 8.5,
                "prev_close": 9.0, "change_pct": -2.22,
            },
            "000002": {
                "name": "收盘候选", "close": 9.5, "high": 9.7, "low": 8.5,
                "prev_close": 9.0, "change_pct": 5.56,
            },
        }

    def test_custom_window_validation(self):
        self.assertEqual(scan_intraday.parse_window("120d"), 120)
        self.assertEqual(scan_intraday.parse_window("37"), 37)
        with self.assertRaises(ValueError):
            scan_intraday.parse_window(4)
        with self.assertRaises(ValueError):
            scan_intraday.parse_window(251)

    def test_touch_and_close_candidate_are_distinct(self):
        signals, market = scan_intraday._collect_signals(
            ["000001", "000002"], self.spots, self.ohlcv, [20],
            {"000001": 1000, "000002": 2000}, "2026-07-14T10:00:00",
        )
        pulled = signals[20]["highs"]["000001"]
        candidate = signals[20]["highs"]["000002"]
        self.assertTrue(pulled["touched"])
        self.assertFalse(pulled["standing"])
        self.assertFalse(pulled["retained"])
        self.assertEqual(pulled["status"], "touched_pulled_back")
        self.assertFalse(candidate["touched"])
        self.assertTrue(candidate["standing"])
        self.assertFalse(candidate["retained"])
        self.assertEqual(candidate["status"], "close_candidate")
        self.assertEqual(candidate["mcap"], 19000)
        self.assertEqual(market["spot_count"], 2)
        self.assertEqual(market["mcap_count"], 2)

    def test_output_keeps_zero_signal_industries_and_full_denominator(self):
        signals, _ = scan_intraday._collect_signals(
            ["000001", "000002"], self.spots, self.ohlcv, [20],
            {"000001": 1000, "000002": 2000}, "2026-07-14T10:00:00",
        )
        output = scan_intraday._build_output(
            "highs", 20, "sw",
            {"000001": "行业甲", "000002": "行业甲", "000003": "行业乙"},
            signals[20]["highs"], self.spots,
            {"000001": 1000, "000002": 2000}, "2026-07-14T10:00:00",
            {"000001": "2026-07-14T09:35:00"},
        )
        rows = {row["industry"]: row for row in output["industries"]}
        self.assertIn("行业乙", rows)
        self.assertEqual(rows["行业乙"]["daily_counts"], [0])
        self.assertEqual(rows["全市场合计"]["total"], 3)
        self.assertEqual(rows["全市场合计"]["touched_count"], 1)
        self.assertEqual(rows["全市场合计"]["standing_count"], 1)
        self.assertEqual(rows["全市场合计"]["retained_count"], 0)
        first = rows["行业甲"]["daily_details"][output["dates"][0]["label"]]
        self.assertEqual(
            next(stock for stock in first if stock["code"] == "000001")["first_seen_at"],
            "2026-07-14T09:35:00",
        )

    def test_load_shares_supports_v3_nested_contract(self):
        payload = {
            "version": 3,
            "total_shares": {"000001": 1000, "000002": "2000"},
            "circulating_shares": {"000001": 800},
        }
        with mock.patch.object(scan_intraday, "_load_json", return_value=payload):
            self.assertEqual(
                scan_intraday._load_shares(),
                {"000001": 1000, "000002": 2000},
            )

    def test_load_shares_keeps_legacy_flat_contract(self):
        with mock.patch.object(
            scan_intraday,
            "_load_json",
            return_value={"000001": 1000, "updated_at": "ignore"},
        ):
            self.assertEqual(scan_intraday._load_shares(), {"000001": 1000})


if __name__ == "__main__":
    unittest.main()
