import unittest
from unittest import mock

from etf_backtest import (
    FORWARD_DAYS,
    INTRADAY_DISABLED_REASON,
    SHORT_HORIZONS,
    _adjust_ohlc_corporate_actions,
    _annotate_forward_outcomes,
    _calibrate_scores,
    _clustered_evaluation,
    _forward_perf,
    _intraday_proxy_trade,
    _intraday_summary,
    _join_universe_outcomes,
    _normalise_candidate,
    _summary,
    _weak_series,
)


def rows(*items):
    return [
        {"date": date, "open": open_, "high": high, "low": low,
         "close": close, "volume": 1}
        for date, open_, high, low, close in items
    ]


class StrictForwardBacktestTest(unittest.TestCase):
    def setUp(self):
        self.benchmark = rows(
            ("20260701", 200, 200, 200, 200),
            ("20260702", 200, 201, 199, 200),
            ("20260703", 200, 202, 199, 201),
            ("20260704", 201, 203, 200, 202),
            ("20260705", 202, 204, 201, 203),
            ("20260706", 203, 205, 202, 204),
            ("20260707", 204, 206, 203, 205),
            ("20260708", 205, 207, 204, 206),
            ("20260709", 206, 208, 205, 207),
            ("20260710", 207, 209, 206, 208),
            ("20260711", 208, 210, 207, 209),
        )
        self.etf = rows(
            ("20260701", 100, 100, 100, 100),
            ("20260702", 100, 102, 99, 101),
            ("20260703", 101, 104, 100, 103),
            ("20260704", 103, 106, 102, 105),
            ("20260705", 105, 109, 104, 108),
            ("20260706", 108, 111, 107, 110),
            ("20260707", 110, 112, 109, 111),
            ("20260708", 111, 113, 110, 112),
            ("20260709", 112, 114, 111, 113),
            ("20260710", 113, 115, 112, 114),
            ("20260711", 114, 116, 113, 115),
        )

    def test_cost_benchmark_and_excess_are_applied_to_t5(self):
        result = _forward_perf(
            self.etf, "20260701", self.benchmark, round_trip_cost_bps=20
        )
        self.assertEqual(result["entry_date"], "20260702")
        self.assertAlmostEqual(result["gross_ret_t5"], 10.0, places=4)
        self.assertAlmostEqual(result["net_ret_t5"], 9.8, places=4)
        self.assertAlmostEqual(result["benchmark_ret_t5"], 2.0, places=4)
        self.assertAlmostEqual(result["excess_ret_t5"], 7.8, places=4)
        self.assertEqual(result["ret_t5"], result["net_ret_t5"])

    def test_short_horizons_cover_every_day_from_t1_through_t5(self):
        result = _forward_perf(
            self.etf, "20260701", self.benchmark, round_trip_cost_bps=20
        )
        self.assertEqual(SHORT_HORIZONS, (1, 2, 3, 4, 5))
        self.assertEqual(FORWARD_DAYS, 5)
        self.assertEqual(result["fwd_days"], 5)
        self.assertAlmostEqual(result["net_ret_t2"], 2.8, places=4)
        self.assertAlmostEqual(result["excess_ret_t2"], 2.3, places=4)
        self.assertAlmostEqual(result["net_ret_t4"], 7.8, places=4)
        self.assertAlmostEqual(result["excess_ret_t4"], 6.3, places=4)
        for day in SHORT_HORIZONS:
            self.assertIn(f"net_ret_t{day}", result)
        self.assertNotIn("net_ret_t10", result)

    def test_missing_market_t_plus_one_is_not_shifted_to_a_later_entry(self):
        without_t1 = [row for row in self.etf if row["date"] != "20260702"]
        result = _forward_perf(without_t1, "20260701", self.benchmark)
        self.assertIsNone(result["entry"])
        self.assertEqual(result["entry_date"], "20260702")

    def test_etf_share_split_adjusts_all_ohlc_and_volume(self):
        source = rows(
            ("20260101", 3.8, 4.1, 3.7, 4.0),
            ("20260102", 4.0, 4.2, 3.9, 4.0),
            ("20260105", 1.0, 1.1, 0.98, 1.05),
        )
        source[0]["volume"], source[1]["volume"], source[2]["volume"] = 100, 120, 480

        adjusted = _adjust_ohlc_corporate_actions(source)

        ratio = 1.05 / 4.0
        self.assertAlmostEqual(adjusted[1]["open"], 4.0 * ratio)
        self.assertAlmostEqual(adjusted[1]["high"], 4.2 * ratio)
        self.assertAlmostEqual(adjusted[1]["low"], 3.9 * ratio)
        self.assertAlmostEqual(adjusted[1]["volume"], 120.0 / ratio)
        self.assertEqual(adjusted[2], source[2])
        self.assertEqual(source[1]["open"], 4.0)


class HotspotLabelAndClusterTest(unittest.TestCase):
    def test_v3_candidate_fields_are_normalised(self):
        candidate = _normalise_candidate({
            "code": "510001",
            "name": "测试ETF",
            "opportunity_score": 67.5,
            "related_industries": [{"industry": "半导体"}, {"industry": "设备"}],
        }, 2)
        self.assertEqual(candidate["score"], 67.5)
        self.assertEqual(candidate["industries"][0]["industry"], "半导体")
        self.assertEqual(candidate["label"], "测试ETF")

    def test_hotspot_requires_positive_net_excess_and_top_twenty_percent(self):
        picks = []
        for index, net in enumerate([5.0, 4.0, 3.0, 2.0, 1.0], 1):
            picks.append({
                "date": "20260701",
                "rank": index,
                "score": 100 - index,
                "net_ret_t5": net,
                "benchmark_ret_t5": 0.5,
                "excess_ret_t5": net - 0.5,
            })
        _annotate_forward_outcomes(picks)
        self.assertEqual(picks[0]["forward_percentile_t5"], 100.0)
        self.assertTrue(picks[0]["hotspot_hit_t5"])
        self.assertEqual(picks[1]["forward_percentile_t5"], 75.0)
        self.assertFalse(picks[1]["hotspot_hit_t5"])

        picks[0]["excess_ret_t5"] = -0.1
        _annotate_forward_outcomes(picks)
        self.assertFalse(picks[0]["hotspot_hit_t5"])

    def test_models_join_the_same_full_universe_percentile(self):
        universe = [
            {
                "date": "20260701", "code": f"ETF{index}",
                "net_ret_t5": float(net), "benchmark_ret_t5": 0.0,
                "excess_ret_t5": float(net),
            }
            for index, net in enumerate([10, 8, 6, 4, 2])
        ]
        _annotate_forward_outcomes(universe)
        model = [{"date": "20260701", "code": "ETF2", "score": 99}]
        baseline = [{"date": "20260701", "code": "ETF2", "score": 3}]
        _join_universe_outcomes(model, universe)
        _join_universe_outcomes(baseline, universe)
        self.assertEqual(model[0]["forward_percentile_t5"], 50.0)
        self.assertEqual(
            model[0]["forward_percentile_t5"],
            baseline[0]["forward_percentile_t5"],
        )
        self.assertFalse(model[0]["hotspot_hit_t5"])

    def test_statistics_cluster_by_prediction_date(self):
        picks = [
            {
                "date": "20260701", "rank": 1, "score": 100,
                "net_ret_t5": 5.0, "excess_ret_t5": 4.0,
                "hotspot_hit_t5": True,
            },
            {
                "date": "20260701", "rank": 2, "score": 90,
                "net_ret_t5": -5.0, "excess_ret_t5": -6.0,
                "hotspot_hit_t5": False,
            },
            {
                "date": "20260702", "rank": 1, "score": 100,
                "net_ret_t5": -1.0, "excess_ret_t5": -2.0,
                "hotspot_hit_t5": False,
            },
        ]
        evaluation = _clustered_evaluation(
            picks, 5, k=1,
            expected_dates=["20260701", "20260702", "20260703", "20260704"],
        )
        self.assertEqual(evaluation["eligible_dates"], 2)
        self.assertEqual(evaluation["eligible_samples"], 3)
        self.assertEqual(evaluation["precision_at_k"], 50.0)
        self.assertAlmostEqual(evaluation["avg_net_return"], 2.0, places=4)
        self.assertEqual(evaluation["coverage"], 50.0)

        summary = _summary(picks)
        self.assertEqual(summary["t5"]["n"], 2)
        self.assertEqual(summary["t5"]["samples"], 3)


class CalibrationAndIntradayTest(unittest.TestCase):
    def test_calibration_is_empty_below_date_or_sample_threshold(self):
        fewer_dates = [
            {
                "date": "placeholder",
                "score": float(index % 10),
                "hotspot_hit_t5": index % 5 == 0,
            }
            for index in range(590)
        ]
        # 强制只有 59 个独立日期，同时样本数达到 500。
        fewer_dates = [
            {**row, "date": f"D{index // 10:02d}"}
            for index, row in enumerate(fewer_dates)
        ]
        result = _calibrate_scores(fewer_dates, 5)
        self.assertEqual(result["independent_dates"], 59)
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["model"])
        self.assertEqual(result["score_knots"], [])
        self.assertEqual(result["probabilities"], [])

        fewer_samples = [
            {
                "date": f"D{date:02d}",
                "score": float(rank),
                "hotspot_hit_t5": rank == 0,
            }
            for date in range(60)
            for rank in range(8)
        ]
        result = _calibrate_scores(fewer_samples, 5)
        self.assertEqual(result["sample_size"], 480)
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["model"])

    def test_calibration_fits_early_dates_and_scores_later_dates_only(self):
        picks = [
            {
                "date": f"D{date:03d}",
                "score": float(rank),
                # 验证段故意反转关系，若偷用验证标签拟合会得到另一套映射。
                "hotspot_hit_t5": rank >= 8 if date < 56 else rank < 2,
            }
            for date in range(80)
            for rank in range(10)
        ]
        result = _calibrate_scores(picks, 5)
        self.assertEqual(result["status"], "ready")
        self.assertIsNotNone(result["model"])
        self.assertTrue(result["score_knots"])
        self.assertEqual(
            result["model"]["score_knots"], result["score_knots"]
        )
        self.assertLess(
            result["training"]["last_date"],
            result["validation"]["first_date"],
        )
        self.assertEqual(result["validation"]["sample_size"], 240)
        self.assertIsNotNone(result["validation"]["brier"])
        self.assertTrue(result["validation"]["reliability"])

    def test_1450_module_is_disabled_instead_of_proxying_daily_ohlc(self):
        trade = _intraday_proxy_trade(rows(
            ("20260701", 1, 1.00, 1.00, 1.00),
            ("20260702", 1, 1.03, 0.96, 1.01),
        ), "20260701")
        self.assertEqual(trade["status"], "disabled")
        self.assertIsNone(trade["return_pct"])
        self.assertIn("无法无穿越复现", trade["reason"])

        summary = _intraday_summary([trade])
        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(summary["valid_sessions"], 0)
        self.assertIsNone(summary["total_return"])
        self.assertEqual(summary["reason"], INTRADAY_DISABLED_REASON)

    def test_weak_period_exits_after_configured_maximum(self):
        dates = [f"202607{day:02d}" for day in range(1, 8)]
        rows_ = [
            {"date": date, "close": 10 - index}
            for index, date in enumerate(dates)
        ]
        with mock.patch("momentum_etf._sina_kline", return_value=rows_):
            series = _weak_series({"weak_ma_lookback": 2, "max_weak_days": 3})

        self.assertTrue(series["20260704"])
        self.assertFalse(series["20260705"])


if __name__ == "__main__":
    unittest.main()
