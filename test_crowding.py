import json
import math
import unittest

import pandas as pd

from crowding import (
    apply_external_evidence,
    assess_exit_risk,
    build_crowding_payload,
    causal_rolling_percentile,
)


class CrowdingCausalityTest(unittest.TestCase):
    def test_percentile_excludes_current_value_and_future_values(self):
        original = pd.Series([1.0, 2.0, 1.5, 4.0, 5.0])
        changed_future = pd.Series([1.0, 2.0, 1.5, -999.0, 999.0])

        result = causal_rolling_percentile(
            original, window=3, min_periods=2)
        changed_result = causal_rolling_percentile(
            changed_future, window=3, min_periods=2)

        self.assertTrue(math.isnan(result.iloc[0]))
        self.assertTrue(math.isnan(result.iloc[1]))
        # 1.5 is compared only with the two prior values [1, 2].
        self.assertEqual(result.iloc[2], 50.0)
        pd.testing.assert_series_equal(
            result.iloc[:3], changed_result.iloc[:3])


class CrowdingExitRiskTest(unittest.TestCase):
    def test_concentration_without_independent_confirmation_is_watch_only(self):
        result = assess_exit_risk(
            pd.Series({"share_pctile": 92.0, "persistence_days": 8}),
            "industry",
        )

        self.assertEqual(result["state"], "watch")
        self.assertEqual(result["domains"], {})

    def test_two_independent_confirmation_domains_are_fragile(self):
        result = assess_exit_risk(
            pd.Series({
                "share_pctile": 92.0,
                "persistence_days": 2,
                "leader_overlap_5d": 0.65,
                "price_extension_pctile": 88.0,
            }),
            "industry",
        )

        self.assertEqual(result["state"], "fragile")
        self.assertEqual(
            set(result["domains"]), {"synchrony", "extension"})

    def test_market_top5_does_not_double_count_stock_concentration(self):
        result = assess_exit_risk(
            pd.Series({
                "concentration_pctile": 94.0,
                "internal_top5_pctile": 99.0,
                "amihud_1e10_pctile": 85.0,
            }),
            "market",
        )

        self.assertEqual(result["state"], "watch")
        self.assertNotIn("internal", result["domains"])
        self.assertEqual(set(result["domains"]), {"liquidity"})

    def test_persistent_concentration_with_active_downside_damage_is_unwind(self):
        result = assess_exit_risk(
            pd.Series({
                "share_pctile": 94.0,
                "persistence_days": 3,
                "direction_score": -0.25,
                "breadth": -0.30,
                "downside_impact_pctile": 91.0,
                "price_change_pct": -1.6,
            }),
            "industry",
        )

        self.assertEqual(result["state"], "unwind")
        self.assertIn("breadth", result["domains"])
        self.assertIn("downside", result["domains"])


class CrowdingPayloadTest(unittest.TestCase):
    def test_payload_is_strict_json_and_contains_frontend_contract(self):
        market = pd.DataFrame([
            {
                "date": "20260723",
                "concentration_pctile": float("nan"),
                "risk_state": "unknown",
                "risk_label": "证据不足",
                "risk_reasons": [],
                "stocks": 998,
                "expected_stocks": 1000,
                "coverage": 0.998,
            },
            {
                "date": "20260724",
                "concentration_pctile": 91.0,
                "risk_state": "watch",
                "risk_label": "集中观察",
                "risk_reasons": ["尚无独立退出危险证据"],
                "stocks": 999,
                "expected_stocks": 1000,
                "coverage": 0.999,
            },
        ])
        industry = pd.DataFrame([
            {
                "date": "20260723",
                "industry": "电子",
                "amount": 90.0,
                "share": 0.09,
                "share_pctile": float("nan"),
            },
            {
                "date": "20260724",
                "industry": "电子",
                "amount": 120.0,
                "share": 0.12,
                "share_pctile": 94.0,
                "market_impact_score": 100.0,
                "hhi_contribution_pct": 18.0,
                "risk_state": "watch",
                "risk_label": "集中观察",
                "risk_reasons": ["成交集中处于 94 分位"],
                "top_stocks": [{
                    "code": "000001",
                    "name": "样本股",
                    "amount": 30.0,
                    "return_pct": -1.2,
                }],
            },
        ])

        payload = build_crowding_payload(
            market,
            industry,
            scheme="ths",
            scheme_label="同花顺",
            classification={"total": 1000, "direct": 800, "fallback": 200},
            external_summary={
                "available_count": 2,
                "requested_count": 4,
                "confidence": "medium",
            },
        )

        # Disallow JavaScript-only NaN/Infinity tokens and non-JSON pandas types.
        json.dumps(payload, ensure_ascii=False, allow_nan=False)

        required_top_level = {
            "schema_version",
            "model_version",
            "trade_date",
            "dates",
            "market",
            "industries",
            "concentration_state",
            "exit_risk_state",
            "conclusion",
            "coverage",
            "confidence",
            "data_quality",
        }
        self.assertTrue(required_top_level.issubset(payload))
        self.assertEqual(payload["trade_date"], "20260724")
        self.assertEqual(payload["scheme"], "ths")
        self.assertEqual(payload["scheme_label"], "同花顺")
        self.assertEqual(
            payload["data_quality"]["classification"]["fallback"], 200)
        self.assertEqual(payload["concentration_state"]["state"], "watch")
        self.assertEqual(payload["exit_risk_state"]["state"], "watch")
        self.assertEqual(payload["coverage"]["ratio"], 0.999)

        electronic = payload["industries"][0]
        self.assertTrue({
            "daily_shares",
            "daily_amounts",
            "pctile",
            "risk_state",
            "top_stocks",
        }.issubset(electronic))
        self.assertEqual(electronic["daily_shares"], [0.09, 0.12])
        self.assertEqual(electronic["top_stocks"][0]["share"], 0.25)
        self.assertEqual(
            electronic["top_stocks"][0]["change_pct"], -1.2)


class CrowdingExternalCapacityTest(unittest.TestCase):
    def test_positions_use_industry_turnover_capacity_without_fake_float_cap(self):
        industries = [f"行业{i}" for i in range(5)]
        codes = [f"{index:06d}" for index in range(1, 6)]
        market = pd.DataFrame([{
            "date": "20260724",
            "concentration_pctile": 50.0,
            "risk_state": "normal",
            "risk_label": "普通",
            "risk_reasons": [],
            "risk_domains": {},
            "share": 1.0,
        }])
        industry = pd.DataFrame([{
            "date": "20260724",
            "industry": name,
            "amount": 100.0,
            "share": 0.2,
            "share_pctile": 50.0,
            "eligible_stocks": 1,
            "risk_state": "normal",
            "risk_label": "普通",
            "risk_reasons": [],
            "risk_domains": {},
        } for name in industries])
        snapshot = {
            "trade_date": "20260724",
            "sources": {
                "margin": {"status": "ok"},
                "fund_holdings": {"status": "ok"},
            },
            "stocks": {
                code: {
                    # Deliberately omit float_mcap: market-level free float must
                    # not be fabricated as an industry denominator.
                    "margin_balance": float(index * 100),
                    "margin_change": float(index * 5),
                    "fund_hold_mcap": float(index * 200),
                }
                for index, code in enumerate(codes, start=1)
            },
            "etfs": {},
        }

        updated_market, updated_industry, summary = apply_external_evidence(
            market,
            industry,
            snapshot,
            dict(zip(codes, industries)),
        )

        largest = updated_industry[
            updated_industry["industry"].eq("行业4")].iloc[0]
        self.assertEqual(largest["margin_turnover_days"], 5.0)
        self.assertEqual(largest["fund_turnover_days"], 10.0)
        self.assertEqual(largest["margin_turnover_pctile"], 100.0)
        self.assertEqual(largest["fund_turnover_pctile"], 100.0)
        self.assertIn("融资余额", largest["direct_position_domains"])
        self.assertIn("基金披露持仓", largest["direct_position_domains"])
        self.assertEqual(summary["industry_direct_evidence_count"], 5)
        self.assertTrue(pd.notna(
            updated_market.iloc[-1]["direct_position_score"]))


if __name__ == "__main__":
    unittest.main()
