import json
import pickle
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from sentiment_radar import (
    _condition_probability,
    build_sentiment_radar,
    build_sentiment_radar_stocks,
)


def _series(side, industry, members, days=36):
    rows = []
    for index in range(days):
        top = side == "top"
        excess = -0.2 if top else 0.2
        price = -0.2 if top else 0.2
        breadth = -0.02 if top else 0.02
        direction = -0.02 if top else 0.02
        activity = 45
        efficiency = 0
        if index >= days - 5:
            excess = 1.0 if top else -1.0
        if index == days - 2:
            if top:
                price, breadth, direction, activity = 1.2, 0.35, 0.30, 90
            else:
                price, breadth, direction, activity = -1.2, -0.40, -0.30, 90
        if index == days - 1:
            if top:
                price, breadth, direction = -0.8, -0.30, -0.25
            else:
                price, breadth, direction = 0.8, 0.30, 0.25
        rows.append({
            "date": f"202601{index + 1:02d}",
            "industry": industry,
            "eligible_stocks": members,
            "excess_return_pct": excess,
            "price_change_pct": price,
            "breadth": breadth,
            "direction_score": direction,
            "active_direction_breadth": 0.12 if not top else -0.12,
            "activity_pctile": activity,
            "efficiency_gap": efficiency,
            "price_extension": 0.10 if top else -0.10,
            "price_extension_pctile": 90 if top else 10,
            "internal_top5_pctile": 50,
        })
    return rows


def _industry(side, industry, members):
    series = _series(side, industry, members)
    current = dict(series[-1])
    current["series"] = series
    current["traded_stocks"] = members
    return current


def _breadth(industry, total=20):
    return {
        "industry": industry,
        "total": total,
        "daily_counts": [2] * 40,
        "is_total": False,
    }


class SentimentRadarTest(unittest.TestCase):
    def _write_payloads(self, root, scheme="sw", members=20):
        suffix = "" if scheme == "sw" else f"_{scheme}"
        flow = {
            "trade_date": "20260205",
            "scheme": scheme,
            "scheme_label": scheme,
            "data_quality": {
                "history_days": 80,
                "coverage": {"covered": 40, "total": 40, "ratio": 1},
                "classification": {"total": 40, "direct": 40, "fallback": 0},
            },
            "industries": [
                _industry("bottom", "底部行业", members),
                _industry("top", "顶部行业", members),
            ],
        }
        highs = {"dates": [{}] * 40, "industries": [_breadth("底部行业"), _breadth("顶部行业")]}
        lows = {"dates": [{}] * 40, "industries": [_breadth("底部行业"), _breadth("顶部行业")]}
        Path(root, f"capital_flow_v2{suffix}.json").write_text(
            json.dumps(flow, ensure_ascii=False), encoding="utf-8")
        Path(root, f"new_highs_data_month{suffix}.json").write_text(
            json.dumps(highs, ensure_ascii=False), encoding="utf-8")
        Path(root, f"new_lows_data_month{suffix}.json").write_text(
            json.dumps(lows, ensure_ascii=False), encoding="utf-8")

    def test_bottom_and_top_are_separate_confirmed_events(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_payloads(root)
            payload = build_sentiment_radar(root, "sw")
        rows = {row["industry"]: row for row in payload["industries"]}
        bottom = rows["底部行业"]
        top = rows["顶部行业"]
        self.assertTrue(bottom["bottom"]["eligible"])
        self.assertEqual(bottom["bottom"]["stage"], "confirmed")
        self.assertFalse(bottom["top"]["eligible"])
        self.assertTrue(top["top"]["eligible"])
        self.assertEqual(top["top"]["stage"], "confirmed")
        self.assertFalse(top["bottom"]["eligible"])
        self.assertEqual(len(bottom["bottom"]["horizons"]), 5)
        self.assertTrue(all(item["probability"] is not None for item in bottom["bottom"]["horizons"]))

    def test_small_industry_suppresses_probability(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_payloads(root, members=3)
            payload = build_sentiment_radar(root, "sw")
        bottom = next(row for row in payload["industries"] if row["industry"] == "底部行业")
        self.assertTrue(bottom["bottom"]["eligible"])
        self.assertFalse(bottom["bottom"]["probability_available"])
        self.assertTrue(all(item["probability"] is None for item in bottom["bottom"]["horizons"]))

    def test_sw3_frequency_is_shrunk_by_member_count(self):
        result = _condition_probability("sw3", "bottom", ["triple_confirm"], members=5)
        self.assertEqual(result["probability_kind"], "hierarchical_research_frequency")
        self.assertAlmostEqual(result["shrink_weight"], 0.2)
        local = 33.5
        parent = 38.3
        self.assertAlmostEqual(result["horizons"][4]["probability"], round(0.2 * local + 0.8 * parent, 1))

    def test_invalid_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            build_sentiment_radar("/tmp", "bad")

    def test_stock_detail_uses_selected_taxonomy_and_stops_at_signal_date(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            pd.DataFrame([
                {"股票代码": "000001", "行业名称": "测试一级"},
                {"股票代码": "000002", "行业名称": "测试一级"},
            ]).to_excel(
                root_path / "industry_stock_map.xlsx",
                sheet_name="个股行业映射",
                index=False,
            )
            (root_path / "industry_map_ths.json").write_text(
                json.dumps({"000001": "测试同花顺"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (root_path / "industry_taxonomy.json").write_text(
                json.dumps({
                    "stocks": {
                        "000001": {
                            "sw_level1": "测试一级",
                            "sw_level2": "测试二级",
                            "sw_level3": "测试三级",
                        },
                        "000002": {
                            "sw_level1": "测试一级",
                            "sw_level2": "回退二级",
                        },
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (root_path / "market_cap_sw3.json").write_text(
                json.dumps({
                    "dates": [{"full_label": "2026年1月7日"}],
                    "industries": [{
                        "industry": "测试三级",
                        "stocks": [
                            {"code": "000001", "name": "甲公司", "mcap": 1000},
                            {"code": "000002", "name": "乙公司", "mcap": 800},
                        ],
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            dates = pd.date_range("2026-01-01", periods=8, freq="D")
            frames = {
                "000001": pd.DataFrame({
                    "date": dates,
                    "close": [10, 10, 10, 10, 10, 10, 11, 99],
                    "volume": [100, 100, 100, 100, 100, 100, 300, 9999],
                }),
                "000002": pd.DataFrame({
                    "date": dates,
                    "close": [20, 20, 20, 20, 20, 20, 19, 1],
                    "volume": [100, 100, 100, 100, 100, 100, 300, 9999],
                }),
            }
            with open(root_path / "kline_cache.pkl", "wb") as handle:
                pickle.dump({
                    "version": 2,
                    "updated_at": "2026-01-08T18:00:00",
                    "data": frames,
                }, handle)

            sw = build_sentiment_radar_stocks(
                root, root, "sw", "测试一级", "20260107")
            ths = build_sentiment_radar_stocks(
                root, root, "ths", "测试同花顺", "20260107")
            sw3 = build_sentiment_radar_stocks(
                root, root, "sw3", "测试三级", "20260107")
            sw3_fallback = build_sentiment_radar_stocks(
                root, root, "sw3", "回退二级", "20260107")

        self.assertEqual(sw["member_count"], 2)
        self.assertEqual(sw["quoted_count"], 2)
        self.assertEqual(ths["member_count"], 1)
        self.assertEqual(sw3["member_count"], 1)
        self.assertEqual(sw3_fallback["stocks"][0]["code"], "000002")
        stocks = {row["code"]: row for row in sw["stocks"]}
        self.assertEqual(stocks["000001"]["name"], "甲公司")
        self.assertEqual(stocks["000001"]["close"], 11)
        self.assertEqual(stocks["000001"]["data_date"], "20260107")
        self.assertEqual(stocks["000001"]["role"], "放量领涨")
        self.assertEqual(stocks["000002"]["role"], "放量领跌")
        self.assertAlmostEqual(
            sum(row["amount_share_pct"] for row in sw["stocks"]), 100)


if __name__ == "__main__":
    unittest.main()
