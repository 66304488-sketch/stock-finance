import unittest

import numpy as np

from momentum_etf import (
    _extract_dynamic_pool,
    _load_dynamic_pool,
    _momentum_score,
    _variant_result,
)


class MomentumDynamicPoolTest(unittest.TestCase):
    def test_extracts_unique_etf_recommendation_top10_with_source_metadata(self):
        rows = []
        for index in range(12):
            code = f"51{index:04d}"
            rows.append({
                "industry": f"行业{index}",
                "score": 100 - index,
                "etf": {
                    "code": code,
                    "name": f"ETF{index}",
                    "match_level": "sw3",
                    "match_label": "申万三级",
                },
            })
        rows.insert(2, rows[0])

        entries = _extract_dynamic_pool({"top": rows})

        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0]["rank"], 1)
        self.assertEqual(entries[-1]["rank"], 10)
        self.assertEqual(entries[0]["industry"], "行业0")
        self.assertEqual(len({entry["code"] for entry in entries}), 10)

    def test_dynamic_variant_keeps_recommendation_rank_on_momentum_rows(self):
        metric = {
            "code": "510001",
            "name": "测试ETF",
            "score": 1.2,
            "passed_all": True,
        }
        result = _variant_result(
            {"510001": metric},
            {"510001": "测试ETF"},
            {"score_threshold_ratio": 0.9},
            False,
            {"510001": {"rank": 3, "industry": "测试行业"}},
        )

        self.assertEqual(result["top10"][0]["recommendation"]["rank"], 3)
        self.assertNotIn("recommendation", metric)

    def test_v3_intentional_empty_does_not_restore_stale_candidates(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as root:
            recommendation = Path(root, "etf_recommend_sw3.json")
            recommendation.write_text(json.dumps({
                "model_version": "etf-hotspot-v3",
                "date": "20260724",
                "decision_status": "no_signal",
                "top": [],
            }), encoding="utf-8")
            Path(root, "momentum_dynamic_pool.json").write_text(json.dumps({
                "source": "etf_recommend_sw3",
                "entries": [{"code": "510001", "name": "旧信号"}],
            }), encoding="utf-8")

            with mock.patch(
                "momentum_etf.data_path",
                side_effect=lambda name: str(Path(root, name)),
            ), mock.patch(
                "momentum_etf.resource_path",
                side_effect=lambda name: str(Path(root, name)),
            ):
                names, metadata, stats = _load_dynamic_pool()

            self.assertEqual(names, {})
            self.assertEqual(metadata, {})
            self.assertEqual(stats["dynamic_count"], 0)
            self.assertTrue(stats["intentional_empty"])

    def test_momentum_r_squared_uses_the_regression_weighting(self):
        closes = [1.0, 1.02, 1.01, 1.08, 1.1, 1.18]
        _, _, actual = _momentum_score(closes, 5)

        y = np.log(np.array(closes))
        x = np.arange(len(y))
        weights = np.linspace(1, 2, len(y)) ** 2
        x_bar = np.sum(weights * x) / np.sum(weights)
        y_bar = np.sum(weights * y) / np.sum(weights)
        slope = np.sum(weights * (x - x_bar) * (y - y_bar)) / np.sum(weights * (x - x_bar) ** 2)
        predicted = slope * x + (y_bar - slope * x_bar)
        expected = 1 - np.sum(weights * (y - predicted) ** 2) / np.sum(weights * (y - y_bar) ** 2)

        self.assertAlmostEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
