import os
import tempfile
import unittest
from datetime import datetime, timedelta

from db import StockDB
from market_cap_structure import (
    _point_in_time_effects,
    _stock_price_return_pct,
    build_market_cap_structure_payload,
    causal_percentiles,
)


class MarketCapStructureTest(unittest.TestCase):
    def _fixture(self, root, day_count=22):
        db = StockDB(os.path.join(root, "data.db"))
        dates = [
            (datetime(2026, 1, 1) + timedelta(days=index)).strftime(
                "%Y%m%d")
            for index in range(day_count)
        ]
        stocks = [
            ("000001", "甲一", "行业甲", 1000, 10.0, 0.010),
            ("000002", "甲二", "行业甲", 900, 11.0, 0.006),
            ("000003", "甲三", "行业甲", 800, 12.0, 0.002),
            ("000004", "乙一", "行业乙", 700, 13.0, -0.003),
            ("000005", "乙二", "行业乙", 600, 14.0, 0.001),
            ("000006", "乙三", "行业乙", 500, 15.0, -0.006),
        ]
        aggregate = []
        details = []
        total_history = {}
        circulating_history = {}
        previous_prices = {}
        for index, date in enumerate(dates):
            industry_values = {"行业甲": 0.0, "行业乙": 0.0}
            for code, name, industry, shares, base_price, drift in stocks:
                price = base_price * (1 + drift * index)
                previous = previous_prices.get(code)
                change = (
                    (price / previous - 1) * 100 if previous else 0.0)
                previous_prices[code] = price
                mcap = price * shares
                industry_values[industry] += mcap
                details.append({
                    "date": date,
                    "direction": "market_cap",
                    "period": "daily",
                    "scheme": "sw",
                    "industry": industry,
                    "code": code,
                    "name": name,
                    "price": price,
                    "change_pct": change,
                    "mcap": mcap,
                })
                total_history.setdefault(code, {})[date] = shares
                circulating_history.setdefault(code, {})[date] = round(
                    shares * 0.8)
            for industry, value in industry_values.items():
                aggregate.append({
                    "date": date,
                    "scheme": "sw",
                    "industry": industry,
                    "mcap": value,
                    "stock_count": 3,
                    "is_total": 0,
                })
            aggregate.append({
                "date": date,
                "scheme": "sw",
                "industry": "全市场合计",
                "mcap": sum(industry_values.values()),
                "stock_count": len(stocks),
                "is_total": 1,
            })
        db.insert_market_cap(aggregate)
        db.insert_stock_details(details)
        history = {
            "available": True,
            "source": "unit-test-authoritative-share-history",
            "updated_at": "2026-01-22T18:00:00",
            "total_shares": total_history,
            "circulating_a_shares": circulating_history,
            "events": {},
            "circulating_disclaimer":
                "circulating_share_proxy_not_csi_free_float",
        }
        return db, dates, history

    def test_causal_percentile_never_uses_future_observations(self):
        prefix = [1, 2, 3, 4, 5, 3]
        without_future = causal_percentiles(
            prefix, min_history=3, window=5)
        with_future = causal_percentiles(
            prefix + [999, -999], min_history=3, window=5)
        self.assertEqual(without_future, with_future[:len(prefix)])
        self.assertEqual(without_future[-1], 60.0)

    def test_builder_exposes_contribution_breadth_style_and_cw_ew(self):
        with tempfile.TemporaryDirectory() as root:
            db, dates, history = self._fixture(root)
            payload = build_market_cap_structure_payload(
                db,
                scheme="sw",
                n_dates=60,
                min_history=3,
                share_history=history,
            )

            self.assertEqual(payload["dates"][0]["date"], dates[0])
            self.assertEqual(payload["trade_date"], dates[-1])
            latest = payload["market"]["latest"]
            contribution = sum(
                industry["contribution_1d_bp"]
                for industry in payload["industries"]
            )
            self.assertAlmostEqual(
                contribution,
                latest["market_return_pct"] * 100,
                places=1,
            )
            self.assertIsNotNone(latest["cap_weighted_return_pct"])
            self.assertIsNotNone(latest["equal_weight_return_pct"])
            self.assertIsNotNone(latest["stock_breadth_pct"])
            self.assertEqual(
                set(latest["style"]), {"top100", "next400", "rest"})
            self.assertIsNotNone(latest["hhi"])
            self.assertIsNotNone(latest["effective_industries"])
            industry = payload["industries"][0]
            for offset in (1, 5, 20):
                self.assertIn(
                    f"cap_weighted_return_{offset}d_pct", industry)
                self.assertIn(
                    f"equal_weight_return_{offset}d_pct", industry)
                self.assertIn(
                    f"stock_breadth_{offset}d_pct", industry)
                self.assertIn(
                    f"contribution_{offset}d_bp", industry)
            self.assertEqual(
                latest["measure_kind"], "point_in_time_total_shares")
            self.assertIsNotNone(latest["circulating_mcap_proxy"])
            self.assertFalse(
                payload["data_quality"]["free_float"]["available"])
            self.assertEqual(
                payload["data_quality"]["circulating_share_proxy"][
                    "disclaimer"],
                "circulating_share_proxy_not_csi_free_float",
            )
            universe_quality = payload["data_quality"]["effects"]["universe"]
            self.assertEqual(universe_quality["confidence"], "low")
            self.assertEqual(
                universe_quality["bias"],
                "current_universe_backfill_survivorship_bias",
            )
            self.assertIn("退市", universe_quality["disclosure"])
            self.assertTrue(any(
                "幸存者偏差" in warning
                for warning in payload["data_quality"]["warnings"]
            ))
            db.close()

    def test_without_share_history_never_fabricates_supply_or_free_float(self):
        with tempfile.TemporaryDirectory() as root:
            db, _, _ = self._fixture(root)
            payload = build_market_cap_structure_payload(
                db,
                scheme="sw",
                min_history=3,
                share_history={
                    "available": False,
                    "total_shares": {},
                    "circulating_a_shares": {},
                    "events": {},
                    "warning": "not available",
                },
            )
            latest = payload["market"]["latest"]
            self.assertIsNone(latest["supply_effect_bp"])
            self.assertIsNone(latest["circulating_mcap_proxy"])
            self.assertFalse(
                payload["data_quality"]["free_float"]["available"])
            self.assertEqual(
                payload["data_quality"]["effects"]["supply"][
                    "method"],
                "unavailable",
            )
            db.close()

    def test_point_in_time_effects_obey_exact_accounting_identity(self):
        previous = {
            "000001": {
                "code": "000001", "name": "甲", "price": 10, "mcap": 1000,
            },
            "000002": {
                "code": "000002", "name": "乙", "price": 20, "mcap": 2000,
            },
        }
        current = {
            "000001": {
                "code": "000001", "name": "甲", "price": 11, "mcap": 1210,
            },
            "000002": {
                "code": "000002", "name": "乙", "price": 18, "mcap": 1800,
            },
        }
        history = {
            "available": True,
            "total_shares": {
                "000001": {"20260101": 100, "20260102": 110},
                "000002": {"20260101": 100, "20260102": 100},
            },
            "events": {},
        }
        effects = _point_in_time_effects(
            current,
            previous,
            "20260102",
            "20260101",
            history,
            3010,
            3000,
        )
        explained = sum(
            effects[field]
            for field in (
                "price_effect_bp",
                "supply_effect_bp",
                "share_snapshot_effect_bp",
                "universe_effect_bp",
                "residual_effect_bp",
            )
        )
        self.assertAlmostEqual(
            explained,
            effects["point_in_time_market_return_pct"] * 100,
            places=1,
        )
        self.assertEqual(effects["company_action_count"], 0)

    def test_split_like_events_are_neutralized_not_counted_as_supply(self):
        samples = [
            (
                "688059",
                140.71,
                85.19,
                99_986_768,
                139_981_475,
                "2025年度权益分派：资本公积金转增股本",
            ),
            (
                "002594",
                337.0,
                111.42,
                1_000_000,
                round(1_000_000 * 337.0 / 111.42),
                "送转/拆股测试",
            ),
        ]
        for (
            code,
            old_price,
            new_price,
            old_shares,
            new_shares,
            reason,
        ) in samples:
            with self.subTest(code=code):
                previous_total = old_price * old_shares
                current_total = new_price * new_shares
                history = {
                    "available": True,
                    "total_shares": {
                        code: {
                            "20260101": old_shares,
                            "20260102": new_shares,
                        },
                    },
                    "events": {
                        code: [{
                            "date": "20260102",
                            "reason": reason,
                        }],
                    },
                }
                effects = _point_in_time_effects(
                    {
                        code: {
                            "code": code,
                            "name": code,
                            "price": new_price,
                            "mcap": current_total,
                        },
                    },
                    {
                        code: {
                            "code": code,
                            "name": code,
                            "price": old_price,
                            "mcap": previous_total,
                        },
                    },
                    "20260102",
                    "20260101",
                    history,
                    current_total,
                    previous_total,
                )
                self.assertTrue(effects["available"])
                self.assertEqual(effects["company_action_count"], 1)
                self.assertEqual(effects["supply_effect_bp"], 0.0)
                self.assertTrue(
                    effects["company_action_effect_included_in_price"])
                self.assertAlmostEqual(
                    effects["price_effect_bp"],
                    effects["point_in_time_market_return_pct"] * 100,
                    places=1,
                )
                self.assertLess(
                    abs(effects["price_effect_bp"]),
                    abs(effects["raw_price_effect_bp"]),
                )
                self.assertGreater(
                    effects["company_action_effect_bp"], 1000)

    def test_real_share_supply_events_are_never_neutralized(self):
        reasons = [
            "H股首发上市",
            "向特定对象增发股份",
            "实施配股",
            "股票期权行权",
            "可转债转股",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                history = {
                    "available": True,
                    "total_shares": {
                        "000001": {
                            "20260101": 100,
                            "20260102": 200,
                        },
                    },
                    "events": {
                        "000001": [{
                            "date": "20260102",
                            "reason": reason,
                        }],
                    },
                }
                effects = _point_in_time_effects(
                    {
                        "000001": {
                            "code": "000001",
                            "name": "供给事件",
                            "price": 5,
                            "mcap": 1000,
                        },
                    },
                    {
                        "000001": {
                            "code": "000001",
                            "name": "供给事件",
                            "price": 10,
                            "mcap": 1000,
                        },
                    },
                    "20260102",
                    "20260101",
                    history,
                    1000,
                    1000,
                )
                self.assertTrue(effects["available"])
                self.assertEqual(effects["company_action_count"], 0)
                self.assertEqual(effects["company_action_effect_bp"], 0.0)
                self.assertEqual(effects["price_effect_bp"], -5000.0)
                self.assertEqual(effects["raw_price_effect_bp"], -5000.0)
                self.assertEqual(effects["supply_effect_bp"], 5000.0)
                self.assertEqual(
                    _stock_price_return_pct(
                        {
                            "code": "000001",
                            "date": "20260102",
                            "price": 5,
                        },
                        {
                            "code": "000001",
                            "date": "20260101",
                            "price": 10,
                        },
                        history,
                    ),
                    -50.0,
                )

    def test_neutral_action_adjusts_all_stock_windows_style_and_top_stock(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            dates = [
                (
                    datetime(2026, 1, 1) + timedelta(days=index)
                ).strftime("%Y%m%d")
                for index in range(21)
            ]
            aggregate = []
            details = []
            split_shares = {}
            normal_shares = {}
            for index, date in enumerate(dates):
                is_latest = index == len(dates) - 1
                split_price = 5.0 if is_latest else 10.0
                split_total_shares = 200 if is_latest else 100
                normal_price = 11.0 if is_latest else 10.0
                rows = [
                    (
                        "000001", "送转股", split_price,
                        split_total_shares,
                        -50.0 if is_latest else 0.0,
                    ),
                    (
                        "000002", "普通股", normal_price, 100,
                        10.0 if is_latest else 0.0,
                    ),
                ]
                total = 0.0
                for code, name, price, shares, change_pct in rows:
                    mcap = price * shares
                    total += mcap
                    details.append({
                        "date": date,
                        "direction": "market_cap",
                        "period": "daily",
                        "scheme": "sw",
                        "industry": "测试行业",
                        "code": code,
                        "name": name,
                        "price": price,
                        "change_pct": change_pct,
                        "mcap": mcap,
                    })
                split_shares[date] = split_total_shares
                normal_shares[date] = 100
                aggregate.extend([
                    {
                        "date": date,
                        "scheme": "sw",
                        "industry": "测试行业",
                        "mcap": total,
                        "stock_count": 2,
                        "is_total": 0,
                    },
                    {
                        "date": date,
                        "scheme": "sw",
                        "industry": "全市场合计",
                        "mcap": total,
                        "stock_count": 2,
                        "is_total": 1,
                    },
                ])
            db.insert_market_cap(aggregate)
            db.insert_stock_details(details)
            history = {
                "available": True,
                "source": "unit-test-authoritative-share-history",
                "updated_at": "2026-01-21T18:00:00",
                "total_shares": {
                    "000001": split_shares,
                    "000002": normal_shares,
                },
                "circulating_a_shares": {},
                "events": {
                    "000001": [{
                        "date": dates[-1],
                        "reason": "资本公积金转增股本",
                    }],
                },
            }
            payload = build_market_cap_structure_payload(
                db,
                scheme="sw",
                n_dates=21,
                min_history=1,
                share_history=history,
            )
            latest = payload["market"]["latest"]
            self.assertEqual(latest["cap_weighted_return_pct"], 5.0)
            self.assertEqual(latest["equal_weight_return_pct"], 5.0)
            self.assertEqual(latest["stock_breadth_pct"], 50.0)
            self.assertEqual(latest["up_stock_count"], 1)
            self.assertEqual(latest["down_stock_count"], 0)
            self.assertEqual(latest["flat_stock_count"], 1)
            self.assertEqual(
                latest["style"]["top100"]["equal_weight_return_pct"],
                5.0,
            )
            self.assertEqual(
                latest["style"]["top100"]["breadth_pct"],
                50.0,
            )
            industry = payload["industries"][0]
            for offset in (1, 5, 20):
                self.assertEqual(
                    industry[
                        f"cap_weighted_return_{offset}d_pct"],
                    5.0,
                )
                self.assertEqual(
                    industry[
                        f"equal_weight_return_{offset}d_pct"],
                    5.0,
                )
                self.assertEqual(
                    industry[f"stock_breadth_{offset}d_pct"],
                    50.0,
                )
            split_stock = next(
                stock for stock in industry["top_stocks"]
                if stock["code"] == "000001"
            )
            self.assertEqual(split_stock["raw_change_pct"], -50.0)
            self.assertEqual(split_stock["change_pct"], 0.0)
            db.close()


if __name__ == "__main__":
    unittest.main()
