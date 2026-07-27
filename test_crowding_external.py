import unittest

from crowding_external import (
    _percentile,
    _quarter_candidates,
    aggregate_external_by_industry,
)


class CrowdingExternalTest(unittest.TestCase):
    def test_reporting_quarters_never_use_a_future_period(self):
        self.assertEqual(
            _quarter_candidates("20260724")[:3],
            ["20260630", "20260331", "20251231"],
        )

    def test_missing_values_do_not_become_zero_percentiles(self):
        self.assertIsNone(_percentile([None, None], None))
        self.assertEqual(_percentile([1.0, 2.0, 3.0], 2.0), 66.7)

    def test_industry_aggregation_keeps_direct_and_fragility_domains_separate(self):
        snapshot = {
            "trade_date": "20260724",
            "fetched_at": "2026-07-24T17:00:00",
            "sources": {
                "free_float": {"status": "ok"},
                "margin": {"status": "ok"},
                "order_book": {"status": "ok"},
            },
            "stocks": {
                "000001": {
                    "float_mcap": 1_000.0,
                    "margin_balance": 100.0,
                    "official_amount": 200.0,
                    "bid_depth_amount": 10.0,
                    "spread_bps": 4.0,
                },
                "000002": {
                    "float_mcap": 2_000.0,
                    "margin_balance": 40.0,
                    "official_amount": 400.0,
                    "bid_depth_amount": 80.0,
                    "spread_bps": 1.0,
                },
            },
            "etfs": {},
        }

        rows, summary = aggregate_external_by_industry(
            snapshot,
            {"000001": "电子", "000002": "银行"},
            ["电子", "银行", "煤炭"],
        )

        self.assertAlmostEqual(rows["电子"]["margin_float_pct"], 10.0)
        self.assertGreater(
            rows["电子"]["direct_position_score"],
            rows["银行"]["direct_position_score"],
        )
        self.assertGreater(
            rows["电子"]["external_fragility_score"],
            rows["银行"]["external_fragility_score"],
        )
        self.assertIsNone(rows["煤炭"]["margin_float_pct"])
        self.assertIsNone(rows["煤炭"]["direct_position_score"])
        self.assertEqual(summary["confidence"], "medium")


if __name__ == "__main__":
    unittest.main()
