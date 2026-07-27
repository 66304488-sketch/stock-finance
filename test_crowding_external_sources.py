import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import crowding_external as external


class CrowdingExternalSourceTest(unittest.TestCase):
    def test_etf_mapping_follows_selected_industry_scheme(self):
        self.assertEqual(
            external.load_etf_industry_map(
                ["互联网电商"], scheme="ths").get("159550"),
            "互联网电商",
        )
        self.assertEqual(
            external.load_etf_industry_map(
                ["IT服务Ⅲ"], scheme="sw3").get("516510"),
            "IT服务Ⅲ",
        )
        self.assertEqual(
            external.load_etf_industry_map(
                ["计算机"], scheme="sw").get("516510"),
            "计算机",
        )

    def test_cni_free_float_is_market_level_and_date_validated(self):
        fake_ak = SimpleNamespace(
            index_all_cni=lambda: pd.DataFrame([{
                "指数代码": "399317",
                "指数简称": "国证A指",
                "收盘点位": 6649.8994,
                "成交额": 19157.9762522507,
                "总市值": 1034175.856414723,
                "自由流通市值": 484745.0762654029,
            }]),
            index_hist_cni=lambda **kwargs: pd.DataFrame([{
                "日期": "2026-07-24",
                "收盘价": 6649.8994,
                "成交额": 19157.98,
            }]),
        )

        with patch.dict(sys.modules, {"akshare": fake_ak}):
            market, source = external._fetch_market_free_float("20260726")

        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["as_of"], "20260724")
        self.assertEqual(market["source_date"], "20260724")
        self.assertAlmostEqual(
            market["free_float_mcap"],
            484745.0762654029 * 100_000_000,
        )
        self.assertAlmostEqual(
            market["free_float_turnover_rate"],
            19157.9762522507 / 484745.0762654029 * 100,
        )

    def test_cni_free_float_rejects_an_unmatched_snapshot_date(self):
        fake_ak = SimpleNamespace(
            index_all_cni=lambda: pd.DataFrame([{
                "指数代码": "399317",
                "指数简称": "国证A指",
                "收盘点位": 6649.8994,
                "成交额": 19157.9762522507,
                "总市值": 1034175.856414723,
                "自由流通市值": 484745.0762654029,
            }]),
            index_hist_cni=lambda **kwargs: pd.DataFrame([{
                "日期": "2026-07-24",
                "收盘价": 6500.0,
                "成交额": 18000.0,
            }]),
        )

        with patch.dict(sys.modules, {"akshare": fake_ak}):
            with self.assertRaisesRegex(RuntimeError, "日期校验"):
                external._fetch_market_free_float("20260726")

    def test_margin_falls_back_per_exchange_and_keeps_source_dates(self):
        calls = {"SSE": [], "SZSE": []}

        def sse(date):
            calls["SSE"].append(date)
            rows = {
                "20260724": [{
                    "标的证券代码": "600000",
                    "融资余额": 100.0,
                    "融券余量": 8.0,
                }],
                "20260723": [{
                    "标的证券代码": "600000",
                    "融资余额": 90.0,
                    "融券余量": 7.0,
                }],
            }
            return pd.DataFrame(rows.get(date, []))

        def szse(date):
            calls["SZSE"].append(date)
            if date == "20260724":
                raise ValueError("not published")
            rows = {
                "20260723": [{
                    "证券代码": "000001",
                    "融资余额": 50.0,
                    "融券余额": 4.0,
                    "融券余量": 3.0,
                }],
                "20260722": [{
                    "证券代码": "000001",
                    "融资余额": 45.0,
                    "融券余额": 3.0,
                    "融券余量": 2.0,
                }],
            }
            return pd.DataFrame(rows.get(date, []))

        fake_ak = SimpleNamespace(
            stock_margin_detail_sse=sse,
            stock_margin_detail_szse=szse,
        )
        with patch.dict(sys.modules, {"akshare": fake_ak}):
            records, source = external._fetch_margin(
                {"600000", "000001"},
                "20260724",
                "20260723",
            )

        self.assertEqual(source["status"], "ok")
        self.assertEqual(
            source["source_dates"],
            {"SSE": "20260724", "SZSE": "20260723"},
        )
        self.assertEqual(
            source["previous_source_dates"],
            {"SSE": "20260723", "SZSE": "20260722"},
        )
        self.assertEqual(records["600000"]["margin_change"], 10.0)
        self.assertEqual(records["000001"]["margin_change"], 5.0)
        self.assertEqual(records["000001"]["margin_source_date"], "20260723")
        self.assertEqual(calls["SZSE"][:3], ["20260724", "20260723", "20260722"])

    def test_etf_uses_date_stamped_sse_and_szse_sources(self):
        def sse(date):
            shares = {"20260724": 100.0, "20260723": 90.0}[date]
            return pd.DataFrame([{
                "基金代码": "510300",
                "基金简称": "沪深300ETF",
                "统计日期": date,
                "基金份额": shares,
            }])

        def szse(start_date, end_date, symbol):
            self.assertEqual(start_date, end_date)
            self.assertEqual(symbol, "ETF")
            shares = {"20260724": 200.0, "20260723": 250.0}[start_date]
            return pd.DataFrame([{
                "基金代码": "159919",
                "基金简称": "沪深300ETF",
                "日期": start_date,
                "基金份额": shares,
            }])

        fake_ak = SimpleNamespace(
            fund_etf_scale_sse=sse,
            fund_scale_daily_szse=szse,
        )
        with patch.dict(sys.modules, {"akshare": fake_ak}):
            records, source = external._fetch_etf_shares(
                "20260724", "20260723", {}
            )

        self.assertEqual(source["status"], "ok")
        self.assertEqual(
            source["source_dates"],
            {"SSE": "20260724", "SZSE": "20260724"},
        )
        self.assertEqual(records["510300"]["share_change"], 10.0)
        self.assertAlmostEqual(records["510300"]["share_change_pct"], 100 / 9)
        self.assertEqual(records["159919"]["share_change"], -50.0)
        self.assertAlmostEqual(records["159919"]["share_change_pct"], -20.0)

    def test_same_day_etf_cache_is_not_used_as_a_previous_day(self):
        fake_ak = SimpleNamespace(
            fund_etf_scale_sse=lambda date: pd.DataFrame([{
                "基金代码": "510300",
                "基金简称": "沪深300ETF",
                "统计日期": date,
                "基金份额": 100.0,
            }]),
            fund_scale_daily_szse=lambda **kwargs: pd.DataFrame([{
                "基金代码": "159919",
                "基金简称": "沪深300ETF",
                "日期": kwargs["start_date"],
                "基金份额": 200.0,
            }]),
        )
        cached = {
            "trade_date": "20260724",
            "etfs": {
                "510300": {"shares": 80.0},
                "159919": {"shares": 180.0},
            },
        }
        with patch.dict(sys.modules, {"akshare": fake_ak}):
            records, _ = external._fetch_etf_shares(
                "20260724", None, cached
            )

        self.assertIsNone(records["510300"]["share_change"])
        self.assertIsNone(records["159919"]["share_change"])

    def test_cninfo_quarter_fallback_converts_ten_thousand_yuan_to_yuan(self):
        calls = []

        def fund_report(date):
            calls.append(date)
            if date == "20260630":
                raise KeyError("not complete")
            if date == "20260331":
                return pd.DataFrame([{
                    "股票代码": "300750",
                    "基金覆盖家数": 2335,
                    "持股总数": 399291123,
                    "持股总市值": 16037690.22,
                } for _ in range(20)])
            return pd.DataFrame()

        fake_ak = SimpleNamespace(fund_report_stock_cninfo=fund_report)
        with patch.dict(sys.modules, {"akshare": fake_ak}):
            records, source = external._fetch_fund_holdings(
                {"300750"}, "20260724"
            )

        self.assertEqual(calls[:2], ["20260630", "20260331"])
        self.assertEqual(source["as_of"], "20260331")
        self.assertEqual(records["300750"]["fund_count"], 2335.0)
        self.assertAlmostEqual(
            records["300750"]["fund_hold_mcap"],
            16037690.22 * 10_000,
        )

    def test_northbound_is_explicitly_unsupported_without_a_network_call(self):
        records, source = external._fetch_northbound({"600000"})
        self.assertEqual(records, {})
        self.assertEqual(source["status"], "unsupported")

    def test_order_book_network_failure_degrades_to_unavailable(self):
        with patch("requests.get", side_effect=RuntimeError("offline")):
            records, source = external._fetch_order_book(["600000"])
        self.assertEqual(records, {})
        self.assertEqual(source["status"], "unavailable")

    def test_refresh_keeps_market_data_separate_and_keys_cache_by_universe(self):
        ok = {"status": "ok", "rows": 1}
        skipped = {"status": "skipped", "rows": 0}
        unsupported = {"status": "unsupported", "rows": 0}
        market_loader = Mock(return_value=(
            {"free_float_mcap": 10.0, "free_float_turnover_rate": 2.0},
            ok,
        ))
        margin_loader = Mock(return_value=(
            {"600000": {"margin_balance": 3.0}},
            ok,
        ))
        fund_loader = Mock(return_value=(
            {"600000": {"fund_hold_mcap": 4.0}},
            ok,
        ))
        north_loader = Mock(return_value=({}, unsupported))
        etf_loader = Mock(return_value=(
            {"510300": {"shares": 5.0}},
            ok,
        ))
        book_loader = Mock(return_value=({}, skipped))

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/external.json"
            with (
                patch.object(external, "_fetch_market_free_float", market_loader),
                patch.object(external, "_fetch_margin", margin_loader),
                patch.object(external, "_fetch_fund_holdings", fund_loader),
                patch.object(external, "_fetch_northbound", north_loader),
                patch.object(external, "_fetch_etf_shares", etf_loader),
                patch.object(external, "_fetch_order_book", book_loader),
            ):
                first = external.refresh_external_snapshot(
                    ["600000"],
                    "20260724",
                    previous_trade_date="20260723",
                    force=True,
                    path=path,
                )
                second = external.refresh_external_snapshot(
                    ["600000"],
                    "20260724",
                    previous_trade_date="20260723",
                    path=path,
                )
                external.refresh_external_snapshot(
                    ["600000", "000001"],
                    "20260724",
                    previous_trade_date="20260723",
                    path=path,
                )

        self.assertEqual(first, second)
        self.assertEqual(first["market"]["free_float_mcap"], 10.0)
        self.assertNotIn("free_float_mcap", first["stocks"]["600000"])
        self.assertEqual(first["stocks"]["600000"]["margin_balance"], 3.0)
        self.assertEqual(first["sources"]["northbound"]["status"], "unsupported")
        self.assertEqual(market_loader.call_count, 2)
        self.assertEqual(margin_loader.call_count, 2)

    def test_missing_values_and_legacy_northbound_never_create_scores(self):
        snapshot = {
            "trade_date": "20260724",
            "fetched_at": "2026-07-24T17:00:00",
            "sources": {
                "free_float": {"status": "ok"},
                "northbound": {"status": "ok"},
            },
            "market": {"free_float_turnover_rate": 3.95},
            "stocks": {
                "600000": {
                    "float_mcap": 1_000.0,
                    "north_hold_mcap": 900.0,
                },
                "000001": {
                    "float_mcap": 1_000.0,
                    "north_hold_mcap": 1.0,
                },
            },
            "etfs": {
                "510300": {"shares": 100.0, "share_change": None},
            },
        }
        with patch.object(
            external, "load_etf_industry_map", return_value={"510300": "银行"}
        ):
            rows, summary = external.aggregate_external_by_industry(
                snapshot,
                {"600000": "银行", "000001": "电子"},
                ["银行", "电子"],
            )

        self.assertIsNone(rows["银行"]["fund_float_pct"])
        self.assertIsNone(rows["银行"]["margin_change_pct"])
        self.assertIsNone(rows["银行"]["etf_share_change"])
        self.assertIsNone(rows["银行"]["north_pctile"])
        self.assertIsNone(rows["电子"]["north_pctile"])
        self.assertIsNone(rows["银行"]["direct_position_score"])
        self.assertIsNone(rows["电子"]["direct_position_score"])
        self.assertEqual(summary["market_free_float_turnover_rate"], 3.95)
        self.assertNotIn("northbound", summary["available_sources"])
        self.assertIn("northbound", summary["unsupported_sources"])


if __name__ == "__main__":
    unittest.main()
