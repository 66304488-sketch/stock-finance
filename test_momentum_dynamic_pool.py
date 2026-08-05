import unittest
from datetime import date, datetime, timedelta

import numpy as np

from momentum_etf import (
    RANK_HISTORY_METHOD_REPLAY,
    _elapsed_trade_minutes,
    _etf_metrics,
    _extract_dynamic_pool,
    _historical_weak_flags,
    _load_dynamic_pool,
    _merge_rank_history,
    _momentum_score,
    _rank_history_snapshot,
    _replay_rank_history,
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

    def test_rank_history_migrates_legacy_output_and_replaces_same_day(self):
        def payload(date, code, score, updated_at):
            return {
                "date": date,
                "updated_at": updated_at,
                "mode": "close",
                "variants": {
                    "combined": {
                        "top10": [{
                            "code": code,
                            "name": "测试ETF",
                            "score": score,
                            "annualized": 0.12,
                            "r_squared": 0.8,
                        }]
                    }
                },
            }

        previous = payload("20260730", "510001", 1.0, "2026-07-30T15:10:00")
        current = payload("20260731", "510002", 2.0, "2026-07-31T15:10:00")
        history = _merge_rank_history(previous, current)
        self.assertEqual([row["date"] for row in history], ["20260730", "20260731"])
        self.assertEqual(history[-1]["variants"]["combined"][0]["rank"], 1)

        same_day = payload("20260731", "510003", 3.0, "2026-07-31T15:20:00")
        current_with_history = dict(current, rank_history=history)
        replaced = _merge_rank_history(current_with_history, same_day)
        self.assertEqual([row["date"] for row in replaced], ["20260730", "20260731"])
        self.assertEqual(replaced[-1]["variants"]["combined"][0]["code"], "510003")

        replayed = _rank_history_snapshot(dict(
            payload("20260731", "510099", 9.0, None),
            history_method=RANK_HISTORY_METHOD_REPLAY,
        ))
        observed_wins = _merge_rank_history(previous, same_day, replayed=[replayed])
        self.assertEqual(observed_wins[-1]["variants"]["combined"][0]["code"], "510003")
        self.assertEqual(observed_wins[-1]["method"], "observed")

    def test_rank_history_snapshot_keeps_only_six_digit_ranked_etfs(self):
        snapshot = _rank_history_snapshot({
            "date": "20260731",
            "variants": {
                "combined": {
                    "top10": [
                        {"code": "510001", "name": "有效", "score": 1},
                        {"code": "bad", "name": "无效", "score": 2},
                    ]
                }
            },
        })
        self.assertEqual(snapshot["variants"]["combined"], [{
            "rank": 1,
            "code": "510001",
            "name": "有效",
            "score": 1,
            "annualized": None,
            "r_squared": None,
        }])

    def test_metrics_expose_five_ten_and_twenty_day_returns_and_filter_reasons(self):
        rows = [
            {
                "date": f"2026-07-{index + 1:02d}",
                "open": 100 + index,
                "high": 101 + index,
                "low": 99 + index,
                "close": 100 + index,
                "volume": 1_000_000,
            }
            for index in range(30)
        ]
        params = {
            "lookback_days": 25,
            "score_range": [0, 0.01],
            "r2_threshold": 0.4,
            "ma_lookback": 10,
            "ma_threshold": 1.0,
            "volume_lookback": 5,
            "volume_threshold": 1.8,
            "loss": 0.97,
        }
        metric = _etf_metrics("510001", "测试ETF", rows, params, False)
        self.assertAlmostEqual(metric["return_5d"], round(129 / 124 - 1, 4))
        self.assertAlmostEqual(metric["return_10d"], round(129 / 119 - 1, 4))
        self.assertAlmostEqual(metric["return_20d"], round(129 / 109 - 1, 4))
        self.assertIn("得分阈值外", metric["filter_reasons"])

    def test_rank_history_replays_previous_dates_from_current_universe(self):
        start = date(2026, 5, 1)
        klines = {}
        names = {"510001": "甲ETF", "510002": "乙ETF"}
        for code, daily_gain in (("510001", 0.002), ("510002", 0.001)):
            rows = []
            price = 1.0
            for index in range(60):
                price *= 1 + daily_gain
                rows.append({
                    "date": (start + timedelta(days=index)).isoformat(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 100_000_000,
                })
            klines[code] = rows
        params = {
            "lookback_days": 25,
            "score_range": [-10, 10],
            "score_threshold_ratio": 0.9,
            "r2_threshold": 0.4,
            "ma_lookback": 10,
            "ma_threshold": 1.0,
            "volume_lookback": 5,
            "volume_threshold": 1.8,
            "loss": 0.97,
        }
        pools = {
            "china": names,
            "dynamic": {},
            "combined": names,
            "global": {},
            "strategy": names,
        }
        history = _replay_rank_history(
            klines, names, pools, params, 1, {}, limit=5
        )
        self.assertEqual(len(history), 5)
        self.assertTrue(all(row["method"] == RANK_HISTORY_METHOD_REPLAY for row in history))
        self.assertEqual(history[-1]["variants"]["combined"][0]["code"], "510001")
        self.assertEqual(history[-1]["universe_note"], "按当前池成员与当前参数逐日历史回算")

    def test_historical_weak_flags_do_not_use_future_index_closes(self):
        start = date(2026, 5, 1)
        base = {}
        for number in range(4):
            rows = []
            for index in range(25):
                rows.append({
                    "date": (start + timedelta(days=index)).isoformat(),
                    "close": 100 - index,
                })
            base[f"idx{number}"] = rows
        params = {"weak_ma_lookback": 5, "max_weak_days": 20}
        before = _historical_weak_flags(base, params)
        target = (start + timedelta(days=10)).strftime("%Y%m%d")

        extended = {
            code: rows + [{"date": "2026-07-01", "close": 1000}]
            for code, rows in base.items()
        }
        after = _historical_weak_flags(extended, params)
        self.assertEqual(before[target], after[target])

    def test_weekend_refresh_is_not_labeled_intraday(self):
        self.assertEqual(_elapsed_trade_minutes(datetime(2026, 8, 1, 10, 30)), 240)


if __name__ == "__main__":
    unittest.main()
