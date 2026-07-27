import unittest

from build_etf_map import _build_mapping
from etf_recommend import (
    TOP_N,
    V3_TOP_N,
    _aggregate_etf_candidates,
    _choose_candidate,
    _etf_stats,
    _pick_top,
    _prediction_for,
    _score_components,
    _select_etf_candidates,
    _shrink_small_sample,
)


class Sw3EtfMappingTest(unittest.TestCase):
    def test_recommendation_exports_top10_for_momentum_dynamic_pool(self):
        self.assertEqual(TOP_N, 10)

    def test_exact_sw3_match_has_priority_over_parent_fallback(self):
        industries = ["证券Ⅲ"]
        etfs = [
            {"code": "512000", "name": "券商ETF华宝", "turnover": 200},
            {"code": "510230", "name": "金融ETF国泰", "turnover": 500},
        ]
        parents = {"证券Ⅲ": {"sw2": "证券Ⅱ", "sw1": "非银金融"}}
        aliases = {"证券": ["券商"], "非银金融": ["金融"]}

        mapping, unmatched = _build_mapping(industries, etfs, parents, aliases, {})

        self.assertFalse(unmatched)
        self.assertEqual(mapping["证券Ⅲ"][0]["code"], "512000")
        self.assertEqual(mapping["证券Ⅲ"][0]["match_level"], "sw3")
        self.assertEqual(mapping["证券Ⅲ"][0]["matched_industry"], "证券Ⅲ")

    def test_sw3_without_product_falls_back_to_sw2_before_sw1(self):
        industries = ["数字芯片设计"]
        etfs = [
            {"code": "512480", "name": "半导体ETF国联安", "turnover": 100},
            {"code": "159997", "name": "电子ETF天弘", "turnover": 500},
        ]
        parents = {"数字芯片设计": {"sw2": "半导体", "sw1": "电子"}}
        aliases = {}

        mapping, unmatched = _build_mapping(industries, etfs, parents, aliases, {})

        self.assertFalse(unmatched)
        self.assertEqual(mapping["数字芯片设计"][0]["code"], "512480")
        self.assertEqual(mapping["数字芯片设计"][0]["match_level"], "sw2")
        self.assertEqual(mapping["数字芯片设计"][1]["match_level"], "sw1")

    def test_manual_override_is_labeled(self):
        overrides = {"住宅开发": [{"code": "512200", "name": "房地产ETF南方"}]}
        mapping, unmatched = _build_mapping(["住宅开发"], [], {}, {}, overrides)

        self.assertFalse(unmatched)
        self.assertEqual(mapping["住宅开发"][0]["match_level"], "override")
        self.assertEqual(mapping["住宅开发"][0]["matched_industry"], "住宅开发")

    @staticmethod
    def _metrics(**overrides):
        values = {
            "h5_pct": 3.0, "breadth_accel_pp": 2.0, "net_breadth_2d": 3.0,
            "flow_ratio": 0.08, "flow_persistence": 0.8, "flow_accel": 0.06,
            "vol_ratio": 1.15, "lows_2d_pct": 0.0, "lows_rising": False,
        }
        values.update(overrides)
        return values

    @staticmethod
    def _etf(**overrides):
        values = {
            "ret_2d": 1.5, "ret_5d": 4.0, "ret_20d": 7.0,
            "ma20_distance": 3.0, "amount_ratio_5_20": 1.25,
            "volatility_20d": 2.0, "positive_days_5d": 3,
            "avg_amount_20d": 3e8,
        }
        values.update(overrides)
        return values

    def test_breadth_acceleration_beats_static_high_level(self):
        ranks = {key: 50 for key in ("h5_pct", "breadth_accel_pp", "net_breadth_2d",
                                      "flow_ratio", "flow_persistence", "flow_accel",
                                      "vol_ratio", "ret_2d", "ret_5d", "ma20_distance")}
        accelerating = _score_components(self._metrics(), self._etf(), ranks)
        static = _score_components(
            self._metrics(h5_pct=8.0, breadth_accel_pp=0.0, net_breadth_2d=6.0),
            self._etf(), ranks)
        self.assertGreater(accelerating["score"], static["score"])

    def test_overheated_etf_receives_real_score_penalty(self):
        ranks = {key: 50 for key in ("h5_pct", "breadth_accel_pp", "net_breadth_2d",
                                      "flow_ratio", "flow_persistence", "flow_accel",
                                      "vol_ratio", "ret_2d", "ret_5d", "ma20_distance")}
        normal = _score_components(self._metrics(), self._etf(), ranks)
        hot = _score_components(self._metrics(), self._etf(
            ret_5d=20.0, ret_20d=30.0, ma20_distance=18.0,
            amount_ratio_5_20=3.2, volatility_20d=6.0), ranks)
        self.assertGreater(hot["penalty"], 15)
        self.assertLess(hot["score"], normal["score"])

    def test_weak_absolute_signals_cannot_score_near_full_marks(self):
        ranks = {key: 100 for key in ("h5_pct", "breadth_accel_pp", "net_breadth_2d",
                                       "flow_ratio", "flow_persistence", "flow_accel",
                                       "vol_ratio", "ret_2d", "ret_5d", "ma20_distance")}
        weak = _score_components(
            self._metrics(h5_pct=0, breadth_accel_pp=0, net_breadth_2d=0,
                          flow_ratio=0, flow_persistence=0, flow_accel=0, vol_ratio=0.8),
            self._etf(ret_2d=0, ret_5d=0, ret_20d=0, ma20_distance=0,
                      amount_ratio_5_20=1, positive_days_5d=1), ranks)
        self.assertLess(weak["score"], 50)

    def test_sustained_flow_beats_one_day_pulse(self):
        ranks = {key: 50 for key in ("h5_pct", "breadth_accel_pp", "net_breadth_2d",
                                      "flow_ratio", "flow_persistence", "flow_accel",
                                      "vol_ratio", "ret_2d", "ret_5d", "ma20_distance")}
        sustained = _score_components(self._metrics(flow_ratio=0.12, flow_persistence=0.8), self._etf(), ranks)
        pulse = _score_components(self._metrics(flow_ratio=0.12, flow_persistence=0.2), self._etf(), ranks)
        self.assertGreater(sustained["score"], pulse["score"])
        self.assertTrue(any("方向参与脉冲" in reason for reason in pulse["penalty_reasons"]))

    def test_extreme_current_heat_consumes_future_room(self):
        neutral_ranks = {key: 50 for key in ("h5_pct", "breadth_accel_pp", "net_breadth_2d",
                                              "flow_ratio", "flow_persistence", "flow_accel",
                                              "vol_ratio", "ret_2d", "ret_5d", "ma20_distance")}
        hot_ranks = dict(neutral_ranks, h5_pct=100, flow_ratio=100, vol_ratio=100, ret_5d=100)
        normal = _score_components(self._metrics(), self._etf(), neutral_ranks)
        crowded = _score_components(
            self._metrics(h5_pct=12, flow_ratio=0.28, vol_ratio=1.7),
            self._etf(ret_5d=8), hot_ranks)
        self.assertGreater(crowded["heat_score"], normal["heat_score"])
        self.assertTrue(any("热度拥挤" in reason for reason in crowded["penalty_reasons"]))

    def test_candidate_selection_prefers_exact_then_best_carrier(self):
        candidates = [
            {"code": "510001", "name": "一级ETF", "match_level": "sw1"},
            {"code": "510002", "name": "三级过热ETF", "match_level": "sw3"},
            {"code": "510003", "name": "三级稳健ETF", "match_level": "sw3"},
        ]
        snapshots = {
            "510001": self._etf(avg_amount_20d=2e9),
            "510002": self._etf(ret_5d=22, ma20_distance=18, volatility_20d=6),
            "510003": self._etf(avg_amount_20d=5e8),
        }
        chosen = _choose_candidate(candidates, snapshots)
        self.assertEqual(chosen["candidate"]["code"], "510003")

    def test_next_hotspot_top_prefers_early_stage_over_current_hotspot(self):
        rows = [
            {"industry": "已扩散", "score": 85, "stage": "扩散", "liquid": True,
             "etf": {"code": "510001"}},
            {"industry": "刚启动", "score": 55, "stage": "启动", "liquid": True,
             "etf": {"code": "510002"}},
            {"industry": "在潜伏", "score": 48, "stage": "潜伏", "liquid": True,
             "etf": {"code": "510003"}},
        ]
        top = _pick_top(rows)
        self.assertEqual(top[-1]["industry"], "已扩散")
        self.assertEqual({top[0]["industry"], top[1]["industry"]}, {"在潜伏", "刚启动"})

    def test_etf_share_conversion_is_not_treated_as_price_crash(self):
        closes = [3.0] * 20 + [1.5] * 6
        volumes = [1e8] * 20 + [2e8] * 6
        stats = _etf_stats(closes, volumes, "20260710")
        self.assertTrue(stats["corporate_action_adjusted"])
        self.assertAlmostEqual(stats["ret_20d"], 0.0, places=1)
        self.assertAlmostEqual(stats["ma20_distance"], 0.0, places=1)

    @classmethod
    def _v3_industry_row(cls, code="512000", industry="证券Ⅲ", sample_size=20):
        etf = {
            "code": code, "name": "测试ETF", **cls._etf(),
        }
        return {
            "industry": industry,
            "metrics": {"stock_count": sample_size},
            "signals": {
                "breadth": 75.0,
                "directional_participation_proxy": 65.0,
            },
            "_etf_candidates": [{
                "etf": etf,
                "match_level": "sw3",
                "match_weight": 1.0,
            }],
        }

    @staticmethod
    def _v3_context():
        return {
            "regime": {
                "score": 78.0, "state": "supportive",
                "permission": "allowed", "status": "fresh",
                "temperature": 60.0,
            },
            "benchmark": {"ret_5d": 0.0, "ret_20d": 0.0},
        }

    def test_small_industry_signal_is_shrunk_toward_neutral(self):
        tiny = _shrink_small_sample(100, 2)
        broad = _shrink_small_sample(100, 50)
        self.assertAlmostEqual(tiny, 58.333, places=2)
        self.assertGreater(broad, tiny)
        self.assertLess(broad, 100)

    def test_exchange_etf_share_growth_adds_real_demand_score(self):
        context = self._v3_context()
        row = self._v3_industry_row()
        without = _aggregate_etf_candidates(
            [row], external={}, crowding={}, **context)[0]
        with_growth = _aggregate_etf_candidates(
            [row],
            external={"512000": {"share_change_pct": 4.0}},
            crowding={},
            **context,
        )[0]
        self.assertGreater(
            with_growth["signals"]["demand"], without["signals"]["demand"])
        self.assertGreater(with_growth["score"], without["score"])
        self.assertEqual(with_growth["share_change_pct"], 4.0)

    def test_industry_crowding_reduces_opportunity_and_raises_risk(self):
        context = self._v3_context()
        row = self._v3_industry_row()
        safe = _aggregate_etf_candidates(
            [row],
            crowding={"证券Ⅲ": {
                "risk_state": "normal", "crowding_score": 20,
            }},
            external={"512000": {"share_change_pct": 2.0}},
            **context,
        )[0]
        unwind = _aggregate_etf_candidates(
            [row],
            crowding={"证券Ⅲ": {
                "risk_state": "unwind", "crowding_score": 90,
                "risk_reasons": ["去拥挤正在发生"],
            }},
            external={"512000": {"share_change_pct": 2.0}},
            **context,
        )[0]
        self.assertGreater(unwind["risk"]["score"], safe["risk"]["score"])
        self.assertLess(unwind["score"], safe["score"])
        self.assertIn("去拥挤正在发生", unwind["risk"]["reasons"])

    def test_selective_prediction_can_return_empty(self):
        watch = {
            "code": "512000", "etf": {"code": "512000"}, "score": 57,
            "stage": "watch", "liquid": True,
            "data_quality": {"score": 90}, "risk": {"score": 20},
        }
        self.assertEqual(_select_etf_candidates([watch]), [])

    def test_same_etf_is_aggregated_and_selected_only_once(self):
        context = self._v3_context()
        rows = [
            self._v3_industry_row(industry="证券Ⅲ"),
            self._v3_industry_row(industry="金融科技", sample_size=30),
        ]
        aggregated = _aggregate_etf_candidates(
            rows,
            external={"512000": {"share_change_pct": 4.0}},
            crowding={
                "证券Ⅲ": {"risk_state": "normal", "crowding_score": 20},
                "金融科技": {"risk_state": "normal", "crowding_score": 20},
            },
            **context,
        )
        self.assertEqual(len(aggregated), 1)
        self.assertEqual(len(aggregated[0]["related_industries"]), 2)
        selected = _select_etf_candidates(aggregated + aggregated)
        self.assertLessEqual(len(selected), 1)
        self.assertLessEqual(len(selected), V3_TOP_N)

    def test_probability_is_null_without_v3_calibration(self):
        prediction = _prediction_for(
            75.0, {"status": "insufficient", "reason": "样本不足"})
        self.assertIsNone(prediction["probability"])
        self.assertEqual(prediction["status"], "insufficient")

    def test_ready_isotonic_calibration_maps_probability_to_percent(self):
        prediction = _prediction_for(65.0, {
            "status": "ready",
            "method": "pav_isotonic",
            "score_knots": [55.0, 70.0, 90.0],
            "probabilities": [0.2, 0.45, 0.7],
        })
        self.assertEqual(prediction["status"], "calibrated")
        self.assertEqual(prediction["probability"], 45.0)


if __name__ == "__main__":
    unittest.main()
