import json
import os
import tempfile
import unittest

from decision_intelligence import (
    _probability_trust,
    build_change_signal,
    build_decision_center,
    build_market_regime,
    build_portfolio_risk,
)


class DecisionIntelligenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        dates = [f"2026010{index}" for index in range(1, 7)]

        def series(values, code="A"):
            rows = []
            for day, value in zip(dates, values):
                rows.append({
                    "date": day,
                    "price_change_pct": value,
                    "breadth": value / 4,
                    "direction_score": value / 5,
                    "activity_pctile": 50 + value * 5,
                    "risk_level": "watch" if value < 0 else "normal",
                })
            return rows

        alpha_series = series([-1.0, -0.6, -0.2, -0.1, -0.1, 1.2])
        beta_series = series([-0.8, -0.4, -0.1, 0.0, 0.1, 1.0])
        self.flow = {
            "trade_date": dates[-1],
            "data_quality": {"coverage": {"covered": 20, "total": 20, "ratio": 1}},
            "market": {
                "date": dates[-1],
                "breadth": 0.55,
                "direction_score": 0.45,
                "activity_pctile": 70,
                "amount_ratio_20": 1.3,
                "price_change_pct": 1.1,
                "series": alpha_series,
            },
            "industries": [
                {
                    "date": dates[-1],
                    "industry": "行业甲",
                    "price_change_pct": 1.2,
                    "breadth": 0.30,
                    "direction_score": 0.24,
                    "activity_pctile": 56,
                    "advance_ratio": 0.70,
                    "internal_top5": 0.45,
                    "amihud_1e10_pctile": 20,
                    "market_impact_score": 18,
                    "top_stock_codes": ["000001", "000002"],
                    "top_stocks": [
                        {"code": "000001", "name": "甲一", "share": 0.22, "return_pct": 3.0},
                        {"code": "000002", "name": "甲二", "share": 0.15, "return_pct": 2.0},
                    ],
                    "series": alpha_series,
                },
                {
                    "date": dates[-1],
                    "industry": "行业乙",
                    "price_change_pct": 1.0,
                    "breadth": 0.25,
                    "direction_score": 0.20,
                    "activity_pctile": 55,
                    "advance_ratio": 0.62,
                    "internal_top5": 0.50,
                    "amihud_1e10_pctile": 25,
                    "market_impact_score": 20,
                    "top_stock_codes": ["000001", "000003"],
                    "top_stocks": [
                        {"code": "000001", "name": "甲一", "share": 0.18, "return_pct": 3.0},
                        {"code": "000003", "name": "乙二", "share": 0.14, "return_pct": 1.8},
                    ],
                    "series": beta_series,
                },
            ],
        }
        self.market_cap = {
            "trade_date": dates[-1],
            "market": {
                "date": dates[-1],
                "market_return_pct": 1.1,
                "stock_breadth_pct": 72,
                "style": {
                    "top100": {"return_pct": 0.8, "breadth_pct": 65, "mcap_share_pct": 45},
                    "next400": {"return_pct": 1.1, "breadth_pct": 70, "mcap_share_pct": 25},
                    "rest": {"return_pct": 1.3, "breadth_pct": 74, "mcap_share_pct": 30},
                },
            },
            "industries": [
                {"industry": name, "date": dates[-1], "relative_1d_pct": 1.0, "stock_breadth_1d_pct": 70, "top1_stock_share_pct": 22}
                for name in ("行业甲", "行业乙")
            ],
        }
        self.crowding = {
            "trade_date": dates[-1],
            "market": {"date": dates[-1], "risk_state": "normal"},
            "industries": [
                {
                    "industry": name,
                    "date": dates[-1],
                    "risk_state": "normal",
                    "crowding_score": 45,
                    "direct_position_score": 62,
                    "external_fragility_score": 30,
                    "spread_bps": 3,
                }
                for name in ("行业甲", "行业乙")
            ],
        }
        self.margin = {
            "latest_date": dates[-1],
            "industries": [
                {"industry": name, "financing_change_pct": 1.2}
                for name in ("行业甲", "行业乙")
            ],
        }
        self.temperature = {
            "updated_at": "2026-01-06",
            "rows": [{"date": dates[-1], "temperature": 68}],
            "indices": {
                "test": {
                    "name": "测试指数",
                    "points": [{"date": day, "close": 100 + index} for index, day in enumerate(dates)],
                }
            },
        }
        self.etf = {
            "date": dates[-1],
            "industries": [
                {
                    "industry": name,
                    "score": 75,
                    "liquid": True,
                    "share_change_pct": 1.0,
                    "etf": {
                        "code": "510001" if name == "行业甲" else "510002",
                        "name": f"{name}ETF",
                        "amount_today": 500_000_000,
                        "avg_amount_20d": 400_000_000,
                        "volatility_20d": 1.5,
                    },
                }
                for name in ("行业甲", "行业乙")
            ],
        }
        for filename, payload in (
            ("capital_flow_v2_sw3.json", self.flow),
            ("market_cap_v2_sw3.json", self.market_cap),
            ("crowding_sw3.json", self.crowding),
            ("margin_financing_sw3.json", self.margin),
            ("market_temperature.json", self.temperature),
            ("etf_recommend_sw3.json", self.etf),
        ):
            with open(os.path.join(self.root, filename), "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_market_regime_is_permission_gate(self):
        quality = {
            "status": "valid",
            "as_of": "20260106",
            "warnings": [],
        }
        regime = build_market_regime(
            self.flow,
            self.market_cap,
            self.crowding,
            self.temperature,
            quality,
        )
        self.assertEqual(regime["state"], "trend_expansion")
        self.assertEqual(regime["permission"], "allowed")
        self.assertIn("strategy_fit", regime)
        self.assertIn("indices", regime)

    def test_change_engine_has_level_delta_and_acceleration(self):
        signal = build_change_signal(self.flow["industries"][0], self.crowding["industries"][0])
        self.assertIn("first_strength", signal["flags"])
        self.assertIn("value", signal["metrics"]["price"])
        self.assertIn("delta", signal["metrics"]["breadth"])
        self.assertIn("acceleration", signal["metrics"]["direction"])

    def test_probability_trust_does_not_invent_probability(self):
        missing = _probability_trust({}, "opportunity")
        self.assertFalse(missing["available"])
        self.assertEqual(missing["level"], "insufficient")
        radar = {
            "bottom": {
                "probability_available": True,
                "samples": 600,
                "independent_dates": 150,
                "ci_t5": [50, 61],
                "horizons": [{"horizon": "T1", "probability": 55.12, "base_probability": 49.91, "lift": 1.104}],
            }
        }
        trusted = _probability_trust(radar, "opportunity")
        self.assertEqual(trusted["level"], "stable")
        self.assertEqual(trusted["horizons"][0]["probability"], 55.1)

    def test_decision_center_covers_nine_items(self):
        result = build_decision_center(self.root, "sw3")
        self.assertEqual(result["quality"]["status"], "valid")
        self.assertEqual(len(result["methodology"]["items"]), 9)
        self.assertTrue(result["battle_cards"])
        card = result["battle_cards"][0]
        for key in (
            "change",
            "drivers",
            "structure",
            "tradability",
            "trade_plan",
            "transmission",
            "probability",
            "alerts",
        ):
            self.assertIn(key, card)
        self.assertEqual(card["drivers"]["event_source"]["status"], "manual_journal")
        self.assertGreater(card["tradability"]["liquidity_score"], 0)
        self.assertGreater(card["tradability"]["estimated_cost_bps"], 0)
        self.assertEqual(card["trade_plan"]["horizon"], "T1–T5短线观察")
        self.assertTrue(card["trade_plan"]["invalidation"])
        self.assertFalse(card["probability"]["regime_conditioning"]["supported"])
        alert_ids = [item["id"] for item in result["alerts"]]
        self.assertEqual(len(alert_ids), len(set(alert_ids)))

    def test_misaligned_core_dates_disable_execution(self):
        payload = dict(self.crowding)
        payload["trade_date"] = "20260105"
        payload["market"] = dict(payload["market"], date="20260105")
        with open(
            os.path.join(self.root, "crowding_sw3.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False)
        result = build_decision_center(self.root, "sw3")
        self.assertEqual(result["quality"]["status"], "degraded")
        self.assertNotEqual(result["regime"]["permission"], "allowed")
        for card in result["battle_cards"]:
            self.assertEqual(card["tradability"]["mode"], "observation_only")

    def test_portfolio_detects_correlation_and_shared_leader(self):
        result = build_portfolio_risk(self.root, ["行业甲", "行业乙"], "sw3")
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["average_correlation"], 0.9)
        self.assertTrue(result["leader_overlaps"])
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
