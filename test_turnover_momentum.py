import json
import math
import unittest

import pandas as pd

from turnover_momentum import (
    build_turnover_momentum_payload,
    causal_median_ratio,
    causal_percentile,
    classify_momentum_risk,
    effective_participant_count,
    prepare_stock_momentum_daily,
)


class TurnoverMomentumCausalityTest(unittest.TestCase):
    def test_percentile_and_baseline_exclude_current_and_future(self):
        original = pd.Series([10.0, 20.0, 15.0, 40.0, 50.0])
        changed_future = pd.Series([10.0, 20.0, 15.0, -999.0, 999.0])

        percentile = causal_percentile(original, window=3, min_periods=2)
        changed = causal_percentile(
            changed_future, window=3, min_periods=2)
        self.assertEqual(percentile.iloc[2], 50.0)
        pd.testing.assert_series_equal(
            percentile.iloc[:3], changed.iloc[:3])

        ratio = causal_median_ratio(
            pd.Series([10.0, 20.0, 30.0]), window=2, min_periods=2)
        self.assertTrue(math.isnan(ratio.iloc[0]))
        self.assertTrue(math.isnan(ratio.iloc[1]))
        self.assertEqual(ratio.iloc[2], 2.0)

    def test_stock_rvol_uses_prior_days_only(self):
        dates = pd.date_range("2026-01-01", periods=4, freq="B")
        stocks = pd.DataFrame({
            "date": dates,
            "code": ["1"] * 4,
            "amount": [10.0, 20.0, 100.0, 40.0],
            "return": [0.01] * 4,
        })
        prepared = prepare_stock_momentum_daily(
            stocks, window=2, min_history=2)

        # Day 3 is compared with the median of [10, 20], not a baseline that
        # already contains the current 100.
        self.assertAlmostEqual(
            prepared.iloc[2]["stock_rvol_20"], 100 / 15)
        self.assertEqual(prepared.iloc[0]["code"], "000001")

    def test_effective_count_detects_one_leader_dominating_many_names(self):
        equal = effective_participant_count([10.0, 10.0, 10.0])
        dominated = effective_participant_count([100.0, 1.0, 1.0])

        self.assertAlmostEqual(equal, 3.0)
        self.assertLess(dominated, 1.1)
        self.assertLess(dominated, 3.0)


class TurnoverMomentumRiskTest(unittest.TestCase):
    def test_single_extreme_never_becomes_warning(self):
        risk = classify_momentum_risk({
            "price_change_pct": -2.0,
            "activity_pctile": 99.0,
        })
        self.assertEqual(risk["risk_level"], "normal")
        self.assertLess(risk["risk_evidence_count"], 2)

    def test_three_independent_domains_warn_then_persistence_confirms_danger(self):
        row = {
            "price_change_pct": -2.0,
            "price_result_pctile": 5.0,
            "activity_pctile": 96.0,
            "breadth": -0.45,
            "acceleration": 0.1,
            "acceleration_pctile": 50.0,
        }
        first = classify_momentum_risk(row)
        second = classify_momentum_risk(
            row,
            previous_pattern=first["risk_pattern"],
            previous_level=first["risk_level"],
        )

        self.assertEqual(first["risk_level"], "warning")
        self.assertEqual(first["risk_pattern"], "selloff")
        self.assertEqual(
            set(first["risk_domains"]), {"price", "effort", "participation"})
        self.assertEqual(second["risk_level"], "danger")
        self.assertIn("连续出现", second["risk_reasons"][-1])

    def test_four_domains_are_immediately_dangerous(self):
        risk = classify_momentum_risk({
            "price_change_pct": -2.0,
            "price_result_pctile": 5.0,
            "activity_pctile": 96.0,
            "breadth": -0.45,
            "acceleration": -2.0,
            "acceleration_pctile": 4.0,
        })
        self.assertEqual(risk["risk_level"], "danger")
        self.assertEqual(risk["risk_evidence_count"], 4)


class TurnoverMomentumPayloadTest(unittest.TestCase):
    def _frames(self):
        dates = pd.date_range("2025-01-01", periods=90, freq="B")
        stocks = []
        market = []
        industry = []
        for index, date in enumerate(dates):
            date_string = date.strftime("%Y%m%d")
            for code, amount, stock_return, rvol in (
                ("000001", 120 + index, 0.008, 1.4),
                ("000002", 80 + index, -0.002, 0.8),
            ):
                stocks.append({
                    "date": date,
                    "code": code,
                    "amount": amount,
                    "return": stock_return,
                    "stock_rvol_20": rvol,
                })
            total = 200 + index * 2
            market.append({
                "date": date_string,
                "total_amount": total,
                "direction_score": 0.20,
                "price_change_pct": 0.30,
                "breadth": 0.0,
                "internal_top5": 0.50,
                "stocks": 2,
                "expected_stocks": 2,
                "coverage": 1.0,
            })
            industry.append({
                "date": date_string,
                "industry": "电子",
                "amount": total,
                "share": 0.18 + index / 10000,
                "direction_score": 0.20,
                "price_change_pct": 0.55,
                "breadth": 0.25,
                "internal_top5": 0.60,
                "internal_top5_pctile": 75.0,
                "eligible_stocks": 2,
                "top_stocks": [{
                    "code": "000001",
                    "name": "样本股",
                    "amount": 120 + index,
                    "return_pct": 0.8,
                }],
            })
        return (
            pd.DataFrame(stocks),
            pd.DataFrame(market),
            pd.DataFrame(industry),
        )

    def test_payload_has_flat_metrics_and_twenty_day_causal_series(self):
        stocks, market, industry = self._frames()
        payload = build_turnover_momentum_payload(
            stocks,
            market,
            industry,
            industry_map={"000001": "电子", "000002": "电子"},
            scheme="ths",
            scheme_label="同花顺",
            classification={"total": 2, "direct": 2},
            percentile_window=30,
            min_history=5,
        )
        json.dumps(payload, ensure_ascii=False, allow_nan=False)

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["model_version"], "turnover-momentum-v2")
        self.assertEqual(payload["scheme"], "ths")
        self.assertEqual(payload["scheme_label"], "同花顺")
        self.assertEqual(len(payload["dates"]), 20)
        self.assertEqual(
            payload["data_quality"]["classification"]["direct"], 2)
        self.assertIn("不是资金净流入", payload["data_quality"]["proxy_notice"])

        row = payload["industries"][0]
        required = {
            "activity_pctile", "share_ratio_60", "direction_score",
            "direction_5d", "breadth", "active_breadth",
            "active_participants", "effective_participants",
            "effective_participation_ratio", "eligible_stocks", "internal_top5",
            "price_change_pct", "price_response_pctile", "persistence",
            "coherence", "acceleration", "efficiency_gap", "state",
            "risk_level", "risk_reasons", "top_stocks", "series",
        }
        self.assertTrue(required.issubset(row))
        self.assertEqual(len(row["series"]), 20)
        self.assertAlmostEqual(row["active_breadth"], 0.5)
        self.assertEqual(row["active_participants"], 1)
        self.assertEqual(row["effective_participants"], 1)
        self.assertAlmostEqual(row["effective_participation_ratio"], 0.5)
        self.assertEqual(row["top_stocks"][0]["rvol_20"], 1.4)
        self.assertIn("state_distribution", payload["market"])
        self.assertIn("risk_distribution", payload["market"])


if __name__ == "__main__":
    unittest.main()
