import json
import os
import tempfile
import unittest
from unittest import mock
from collections import deque
from datetime import datetime, timedelta

import pandas as pd

import index_constituents as monitor


def frame(closes):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
        "open": closes,
        "high": [value * 1.01 for value in closes],
        "low": [value * 0.99 for value in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


def official_payload(weights, weight_date="20260301"):
    return {
        "weights": weights,
        "weight_date": weight_date,
        "fetched_at": "2026-07-21T08:00:00",
        "cache_state": "cached",
        "warning": None,
    }


class RecentReturnTests(unittest.TestCase):
    def test_current_close_uses_n_sessions_ago(self):
        prices = [float(value) for value in range(100, 165)]
        result = monitor._recent_returns(frame(prices), prices[-1])
        self.assertEqual(result["return_5d"], round((164 / 159 - 1) * 100, 2))
        self.assertEqual(result["return_60d"], round((164 / 104 - 1) * 100, 2))

    def test_live_price_newer_than_cache_uses_previous_sessions(self):
        prices = [float(value) for value in range(100, 165)]
        result = monitor._recent_returns(frame(prices), 170.0)
        self.assertEqual(result["return_5d"], round((170 / 160 - 1) * 100, 2))

    def test_stale_history_does_not_emit_mislabeled_recent_returns(self):
        prices = [float(value) for value in range(100, 165)]
        result = monitor._recent_returns(frame(prices), 170.0, minimum_date="20260415")
        self.assertIsNone(result["return_5d"])
        self.assertIsNone(result["return_60d"])

    def test_large_kline_cache_is_reused_until_file_signature_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "kline.pkl")
            with open(path, "wb") as handle:
                handle.write(b"signature")
            fake_cache = mock.Mock(cache_file=path)
            fake_cache._cache = {"data": {"000001": frame([10.0, 11.0])}}
            monitor._history_frames_cache = None
            monitor._history_frames_signature = None
            with mock.patch.object(monitor, "KlineCache", return_value=fake_cache):
                first = monitor._load_history_frames(["000001"])
                second = monitor._load_history_frames(["000001"])
            self.assertIn("000001", first)
            self.assertIn("000001", second)
            fake_cache._load.assert_called_once()
            monitor._history_frames_cache = None
            monitor._history_frames_signature = None


class ConstituentCacheTests(unittest.TestCase):
    def test_stale_cache_is_kept_when_refresh_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "constituents.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "version": 1,
                    "indexes": {
                        "000300": {
                            "fetched_at": "2020-01-01T00:00:00",
                            "stocks": [{"code": "000001", "name": "平安银行"}],
                        }
                    },
                }, handle, ensure_ascii=False)
            with mock.patch.object(monitor, "CONSTITUENT_CACHE_FILE", path), \
                 mock.patch.object(monitor, "_fetch_constituents", side_effect=RuntimeError("timeout")):
                rows, fetched_at, warning = monitor._get_constituents("000300")
            self.assertEqual(rows[0]["code"], "000001")
            self.assertEqual(fetched_at, "2020-01-01T00:00:00")
            self.assertIn("继续使用", warning)


class QuoteQualityTests(unittest.TestCase):
    def test_quote_freshness_requires_same_trade_date_and_recent_timestamp(self):
        now = datetime(2026, 7, 21, 13, 23, 0)
        live = monitor._quote_quality("20260721132200", now)
        stale = monitor._quote_quality("20260720150000", now)
        unknown = monitor._quote_quality(None, now)
        self.assertTrue(live["is_live"])
        self.assertEqual(live["age_seconds"], 60)
        self.assertEqual(live["quote_trade_date"], "20260721")
        self.assertEqual(stale["quote_state"], "stale")
        self.assertFalse(stale["is_live"])
        self.assertEqual(unknown["quote_state"], "unknown")

    def test_quote_freshness_never_marks_preopen_lunch_or_after_close_live(self):
        cases = (
            (datetime(2026, 7, 21, 9, 12, 0), "20260721091200"),
            (datetime(2026, 7, 21, 12, 0, 0), "20260721115959"),
            (datetime(2026, 7, 21, 15, 8, 0), "20260721150800"),
        )
        for now, quote_time in cases:
            with self.subTest(now=now):
                quality = monitor._quote_quality(quote_time, now)
                self.assertEqual(quality["quote_state"], "outside_session")
                self.assertFalse(quality["is_live"])

    def test_preopen_mixed_base_is_reconciled_to_timestamp_aligned_return(self):
        change, previous = monitor._reconciled_return(98.0, 98.0, -2.0)
        self.assertEqual(change, -2.0)
        self.assertEqual(previous, 100.0)
        self.assertEqual(
            monitor._index_previous_close(
                {"price": 980.0, "prev_close": 980.0, "change_pct": -2.0}
            ),
            1000.0,
        )

    def test_preopen_quote_uses_latest_completed_history_bar(self):
        history = frame([98.0, 100.0])
        quote = {
            "name": "平安银行",
            "close": 100.0,
            "prev_close": 100.0,
            "change_pct": 0.0,
            "quote_time": "20260727091400",
        }
        completed, used = monitor._prefer_completed_preopen_quote(
            quote,
            history,
            "平安银行",
            datetime(2026, 7, 27, 9, 14, 30),
        )
        self.assertTrue(used)
        self.assertEqual(completed["close"], 100.0)
        self.assertEqual(completed["prev_close"], 98.0)
        self.assertEqual(completed["change_pct"], 2.04)
        self.assertTrue(completed["quote_time"].endswith("150000"))

    def test_completed_index_daily_quote_uses_two_closed_bars(self):
        response = mock.Mock()
        response.text = "kline_dayqfq=" + json.dumps({
            "data": {
                "sh000300": {
                    "day": [
                        ["2026-07-23", "101", "100", "102", "99"],
                        ["2026-07-24", "99", "98", "100", "97"],
                    ]
                }
            }
        })
        with mock.patch.object(monitor.requests, "get", return_value=response):
            quote = monitor._fetch_tencent_index_daily_quote("000300")
        response.raise_for_status.assert_called_once()
        self.assertEqual(quote["price"], 98.0)
        self.assertEqual(quote["prev_close"], 100.0)
        self.assertEqual(quote["change_pct"], -2.0)
        self.assertEqual(quote["quote_time"], "20260724150000")
        self.assertEqual(quote["quote_source"], "tencent_completed_daily")


class WeightModelTests(unittest.TestCase):
    def setUp(self):
        monitor._intraday_history.clear()

    def test_free_float_proxy_uses_transparent_tier_rules(self):
        self.assertEqual(monitor._free_float_tier_pct(0.101), 11.0)
        self.assertEqual(monitor._free_float_tier_pct(0.16), 20.0)
        self.assertEqual(monitor._free_float_tier_pct(0.25), 30.0)
        self.assertEqual(monitor._free_float_tier_pct(0.81), 100.0)

    def test_official_weight_cache_avoids_repeat_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "weights.json")
            fetched = {
                "weight_date": "20260630",
                "weights": {"000001": 100.0},
                "source_url": "https://example.invalid/weights.xls",
            }
            with mock.patch.object(monitor, "INDEX_WEIGHT_CACHE_FILE", path), \
                 mock.patch.object(
                     monitor, "_fetch_csindex_closeweights", return_value=fetched
                 ) as fetcher:
                first = monitor._get_official_weights("000300")
                second = monitor._get_official_weights("000300")
            self.assertEqual(first["cache_state"], "fresh")
            self.assertEqual(second["cache_state"], "cached")
            self.assertEqual(second["weight_date"], "20260630")
            fetcher.assert_called_once_with("000300")

    def test_official_weight_sheet_parser_reads_latest_date_and_weight_column(self):
        sheet = pd.DataFrame({
            "日期Date": ["20260629", "20260630", "20260630"],
            "指数代码 Index Code": ["000300"] * 3,
            "指数名称 Index Name": ["沪深300"] * 3,
            "指数英文名称Index Name(Eng)": ["CSI 300"] * 3,
            "成份券代码Constituent Code": ["000003", "000001", "000002"],
            "成份券名称Constituent Name": ["丙", "甲", "乙"],
            "成份券英文名称Constituent Name(Eng)": ["C", "A", "B"],
            "交易所Exchange": ["深圳证券交易所"] * 3,
            "交易所英文名称Exchange(Eng)": ["Shenzhen Stock Exchange"] * 3,
            "权重(%)weight": ["100", "60", "40"],
        })
        response = mock.Mock(content=b"xls")
        response.raise_for_status.return_value = None
        with mock.patch.object(monitor.requests, "get", return_value=response), \
             mock.patch.object(monitor.pd, "read_excel", return_value=sheet), \
             mock.patch.dict(
                 monitor.INDEXES,
                 {"000300": {"name": "沪深300", "expected_count": 2}},
             ):
            parsed = monitor._fetch_csindex_closeweights("000300")
        self.assertEqual(parsed["weight_date"], "20260630")
        self.assertEqual(parsed["weights"], {"000001": 60.0, "000002": 40.0})

    def test_official_weights_are_drifted_from_weight_date_to_previous_close(self):
        stocks = [
            {
                "code": "000001", "price": 112.0, "prev_close": 110.0,
                "market_cap": 500.0, "total_market_cap": 1000.0,
            },
            {
                "code": "000002", "price": 89.0, "prev_close": 90.0,
                "market_cap": 500.0, "total_market_cap": 1000.0,
            },
        ]
        histories = {
            code: pd.DataFrame({
                "date": [pd.Timestamp("2026-01-02")],
                "close": [100.0],
            })
            for code in ("000001", "000002")
        }
        model = monitor._assign_index_weights(
            stocks,
            histories,
            official_payload({"000001": 60.0, "000002": 40.0}, "20260102"),
        )
        self.assertEqual(model["weight_source"], "csindex_closeweight_drifted")
        self.assertTrue(model["is_official_source"])
        self.assertAlmostEqual(stocks[0]["weight_pct"], 66 / 102 * 100, places=5)
        self.assertAlmostEqual(stocks[1]["weight_pct"], 36 / 102 * 100, places=5)
        self.assertAlmostEqual(sum(stock["dynamic_weight_pct"] for stock in stocks), 100, places=5)
        self.assertEqual(model["drift_coverage_pct"], 100.0)

    def test_proxy_fallback_uses_total_cap_and_circulation_tier(self):
        stocks = [
            {
                "code": "000001", "price": 10.0, "prev_close": 10.0,
                "market_cap": 100.0, "total_market_cap": 1000.0,
            },
            {
                "code": "000002", "price": 10.0, "prev_close": 10.0,
                "market_cap": 250.0, "total_market_cap": 1000.0,
            },
        ]
        model = monitor._assign_index_weights(
            stocks, {}, {"weights": {}, "weight_date": None, "cache_state": "unavailable"}
        )
        self.assertEqual(model["weight_source"], "free_float_tier_proxy")
        self.assertFalse(model["is_official_source"])
        self.assertEqual(stocks[0]["free_float_tier_pct"], 10.0)
        self.assertEqual(stocks[1]["free_float_tier_pct"], 30.0)
        self.assertAlmostEqual(stocks[0]["weight_pct"], 25.0)
        self.assertAlmostEqual(stocks[1]["weight_pct"], 75.0)
        self.assertIn("不是指数公司官方权重", model["disclaimer"])

    def test_metrics_compute_contribution_breadth_and_concentration(self):
        stocks = [
            {
                "code": "000001", "name": "甲", "industry": "银行",
                "industry_ths": "银行", "industry_sw": "银行",
                "industry_sw_detail": "股份制银行", "industry_citic": "银行",
                "price": 101.0, "open": 100.0, "vwap": 100.5,
                "_return_pct": 1.0, "weight_pct": 60.0,
                "dynamic_weight_pct": 60.2, "quote_state": "live", "is_live": True,
            },
            {
                "code": "000002", "name": "乙", "industry": "电子",
                "industry_ths": "半导体", "industry_sw": "电子",
                "industry_sw_detail": "数字芯片设计", "industry_citic": "电子",
                "price": 99.0, "open": 100.0, "vwap": 99.5,
                "_return_pct": -1.0, "weight_pct": 40.0,
                "dynamic_weight_pct": 39.8, "quote_state": "live", "is_live": True,
            },
        ]
        metrics = monitor._compute_index_metrics(
            stocks,
            {"price": 1002.0, "prev_close": 1000.0, "change_pct": 0.2},
            {"is_official_source": True},
        )
        self.assertEqual(stocks[0]["contribution_bp"], 60.0)
        self.assertEqual(stocks[1]["contribution_bp"], -40.0)
        self.assertEqual(metrics["replication"]["replicated_return_pct"], 0.2)
        self.assertEqual(metrics["replication"]["replication_residual_bp"], 0.0)
        self.assertEqual(metrics["breadth"]["weighted_advance_pct"], 60.0)
        self.assertEqual(metrics["breadth"]["weighted_above_open_pct"], 60.0)
        self.assertAlmostEqual(
            metrics["driver_concentration"]["absolute_contribution_hhi"], 0.52
        )
        self.assertEqual(
            metrics["industry_contributions"]["sw"][0]["industry"], "银行"
        )


class IntradayHistoryTests(unittest.TestCase):
    def setUp(self):
        monitor._intraday_history.clear()

    @staticmethod
    def metrics(replicated, official, residual, advance):
        return {
            "replication": {
                "replicated_return_pct": replicated,
                "official_index_return_pct": official,
                "replication_residual_bp": residual,
                "effective_live_weight_pct": 100.0,
            },
            "breadth": {"weighted_advance_pct": advance},
        }

    def test_one_minute_stock_and_summary_impulse(self):
        first = [{"code": "000001", "price": 100.0, "contribution_bp": 0.0}]
        second = [{"code": "000001", "price": 101.0, "contribution_bp": 10.0}]
        start = datetime(2026, 7, 21, 10, 0, 0)
        monitor._attach_intraday_history(
            "000300", first, self.metrics(0.0, 0.0, 0.0, 50.0), start, record=True
        )
        result = monitor._attach_intraday_history(
            "000300",
            second,
            self.metrics(0.1, 0.12, 2.0, 60.0),
            start + timedelta(minutes=1),
            record=True,
        )
        self.assertEqual(second[0]["return_1m_pct"], 1.0)
        self.assertEqual(second[0]["contribution_change_1m_bp"], 10.0)
        self.assertEqual(second[0]["contribution_delta_1m_bp"], 10.0)
        self.assertEqual(result["windows"]["1m"]["contribution_change_bp"], 10.0)
        self.assertEqual(result["windows"]["1m"]["replicated_return_change_bp"], 10.0)
        self.assertEqual(result["windows"]["1m"]["weighted_advance_change_pct"], 10.0)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(len(result["series"]), 2)


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        monitor._quote_cache.clear()
        monitor._index_locks.clear()

    def test_refresh_failure_returns_last_good_snapshot(self):
        good = {"index": {"code": "000300"}, "warning": None, "cache_state": "fresh"}
        with mock.patch.object(
            monitor, "build_index_snapshot", side_effect=[good, RuntimeError("timeout")]
        ):
            first = monitor.get_index_snapshot("000300", force=True)
            second = monitor.get_index_snapshot("000300", force=True)
        self.assertIs(first, good)
        self.assertEqual(second["cache_state"], "stale_last_good")
        self.assertEqual(second["stale_reason"], "timeout")
        self.assertIn("继续使用", second["warning"])

    def test_indexes_use_distinct_build_locks(self):
        with mock.patch.object(
            monitor,
            "build_index_snapshot",
            side_effect=lambda code: {"index": {"code": code}, "cache_state": "fresh"},
        ):
            monitor.get_index_snapshot("000300", force=True)
            monitor.get_index_snapshot("000905", force=True)
        self.assertIsNot(
            monitor._index_locks["000300"], monitor._index_locks["000905"]
        )


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        monitor._intraday_history.clear()

    def test_supported_indexes_include_sse_50_and_star_50(self):
        self.assertEqual(monitor.INDEXES["000016"]["name"], "上证50")
        self.assertEqual(monitor.INDEXES["000016"]["expected_count"], 50)
        self.assertEqual(monitor.INDEX_QUOTE_SYMBOLS["000016"], "sh000016")
        self.assertEqual(monitor.INDEXES["000688"]["name"], "科创50")
        self.assertEqual(monitor.INDEXES["000688"]["expected_count"], 50)
        self.assertEqual(monitor.INDEX_QUOTE_SYMBOLS["000688"], "sh000688")

    def test_supported_indexes_include_csi_100_csi_2000_and_bse_50(self):
        self.assertEqual(monitor.INDEXES["000903"], {"name": "中证100", "expected_count": 100})
        self.assertEqual(monitor.INDEXES["932000"], {"name": "中证2000", "expected_count": 2000})
        self.assertEqual(monitor.INDEXES["899050"], {"name": "北证50", "expected_count": 50})
        self.assertEqual(monitor.INDEX_QUOTE_SYMBOLS["000903"], "sh000903")
        self.assertEqual(monitor.INDEX_QUOTE_SYMBOLS["899050"], "bj899050")

    def test_bse_stocks_use_beijing_quote_symbol(self):
        self.assertEqual(monitor._stock_quote_symbol("920100"), "bj920100")
        self.assertEqual(monitor._stock_quote_symbol("600519"), "sh600519")
        self.assertEqual(monitor._stock_quote_symbol("000001"), "sz000001")

    def test_constituents_fall_back_to_sina(self):
        expected = [{"code": "000001", "name": "平安银行"}]
        with mock.patch.object(monitor, "_fetch_csindex_constituents", side_effect=RuntimeError("timeout")), \
             mock.patch.object(monitor, "_fetch_sina_constituents", return_value=expected) as fallback:
            self.assertEqual(monitor._fetch_constituents("932000"), expected)
        fallback.assert_called_once_with("932000")

    def test_csi_2000_quote_uses_csindex_fallback(self):
        expected = {"code": "932000", "price": 2786.28, "change_pct": -1.26}
        with mock.patch.object(monitor, "_fetch_csindex_index_quote", return_value=expected) as fallback:
            self.assertEqual(monitor._fetch_tencent_index_quote("932000"), expected)
        fallback.assert_called_once_with("932000")

    def test_snapshot_combines_live_quote_returns_and_ths_industry(self):
        prices = [float(value) for value in range(100, 165)]
        fake_cache = mock.Mock()
        fake_cache._cache = {"data": {"000001": frame(prices)}}
        fake_cache._load.return_value = None
        quote = {
            "000001": {
                "name": "平安银行", "close": 170.0, "prev_close": 168.0,
                "open": 169.0, "high": 171.0, "low": 167.0, "change_pct": 1.19,
                "quote_time": "20260721132233",
            }
        }
        valuation = {
            "000001": {
                **quote["000001"],
                "market_cap": 1000.0,
                "total_market_cap": 2000.0,
                "volume_lots": 100.0,
                "amount_10k": 170.0,
                "vwap": 170.0,
            }
        }
        with mock.patch.object(
            monitor, "_get_constituents",
            return_value=([{"code": "000001", "name": "平安银行"}], "2026-07-20T09:00:00", None),
        ), mock.patch.object(monitor, "_load_ths_industries", return_value={"000001": "银行"}), \
             mock.patch.object(monitor, "load_industry_map", return_value={"000001": "银行"}), \
             mock.patch.object(monitor, "_load_industry_taxonomy", return_value={"000001": {
                 "sw_level1": "银行", "sw_level2": "城商行Ⅱ", "sw_level3": "城商行Ⅲ",
             }}), \
             mock.patch.object(monitor, "_required_history_date", return_value="20260301"), \
             mock.patch.object(monitor, "_fetch_tencent_quotes", return_value=valuation), \
             mock.patch.object(monitor, "_fetch_tencent_index_quote", return_value={"price": 4000, "change_pct": 1.0}), \
             mock.patch.object(monitor, "_get_official_weights", return_value=official_payload({"000001": 100.0})), \
             mock.patch.object(monitor, "KlineCache", return_value=fake_cache), \
             mock.patch.object(monitor, "fetch_spot", return_value=quote):
            result = monitor.build_index_snapshot(
                "000300", now=datetime(2026, 7, 21, 13, 23, 0)
            )
        stock = result["stocks"][0]
        self.assertEqual(stock["industry"], "银行")
        self.assertEqual(stock["industry_source"], "ths")
        self.assertEqual(stock["industry_sw"], "银行")
        self.assertEqual(stock["industry_citic"], "银行")
        self.assertEqual(stock["sw_level3"], "城商行Ⅲ")
        self.assertEqual(stock["return_5d"], round((170 / 160 - 1) * 100, 2))
        self.assertEqual(stock["amplitude_pct"], round(4 / 168 * 100, 2))
        self.assertEqual(result["coverage"]["live_quotes"], 1)
        self.assertIn("history", result["coverage"])
        self.assertEqual(result["coverage"]["sw_details"], 1)
        self.assertEqual(stock["quote_state"], "live")
        self.assertEqual(stock["weight_source"], "csindex_closeweight_drifted")
        self.assertEqual(stock["weight_pct"], 100.0)
        self.assertAlmostEqual(stock["contribution_bp"], 119.0476, places=4)
        self.assertIn("replication_residual_bp", result["replication"])
        self.assertEqual(result["intraday"]["sample_count"], 1)

    def test_citic_mapping_prefers_ths_detail_and_falls_back_to_sw(self):
        self.assertEqual(monitor._citic_industry("半导体", "电子"), "电子")
        self.assertEqual(monitor._citic_industry(None, "电力设备"), "电力设备及新能源")
        self.assertEqual(monitor._citic_industry(None, None), "其他")

    def test_tencent_quote_parser_reads_valuation_fields(self):
        fields = [""] * 88
        fields[1] = "平安银行"
        fields[2] = "000001"
        fields[3] = "10.89"
        fields[4] = "10.98"
        fields[5] = "10.90"
        fields[6] = "1000"
        fields[32] = "-0.82"
        fields[33] = "11.13"
        fields[34] = "10.83"
        fields[37] = "108.9"
        fields[38] = "0.71"
        fields[39] = "4.91"
        fields[44] = "2113.30"
        fields[45] = "2300.50"
        fields[46] = "0.46"
        fields[30] = "20260721132233"
        parsed = monitor._parse_tencent_line('v_sz000001="' + "~".join(fields) + '";')
        self.assertEqual(parsed["code"], "000001")
        self.assertEqual(parsed["pe"], 4.91)
        self.assertEqual(parsed["pb"], 0.46)
        self.assertEqual(parsed["market_cap"], 2113.30)
        self.assertEqual(parsed["total_market_cap"], 2300.50)
        self.assertEqual(parsed["open"], 10.90)
        self.assertEqual(parsed["volume_lots"], 1000.0)
        self.assertEqual(parsed["amount_10k"], 108.9)
        self.assertAlmostEqual(parsed["vwap"], 10.89)


class PageContractTests(unittest.TestCase):
    def test_page_and_navigation_contract(self):
        root = os.path.dirname(__file__)
        with open(os.path.join(root, "static", "index-constituents.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(root, "static", "app.html"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertIn("/api/index-constituents", page)
        self.assertIn("上证50", page)
        self.assertIn("科创50", page)
        self.assertIn("中证100", page)
        self.assertIn("中证2000", page)
        self.assertIn("北证50", page)
        self.assertIn('data-view="treemap"', page)
        self.assertIn("binaryTreemap", page)
        self.assertIn("市值树图", page)
        self.assertIn("行业", page)
        self.assertIn("data-sort=\"pb\"", page)
        self.assertIn("stockpage.10jqka.com.cn", page)
        self.assertIn("行业分类", page)
        self.assertIn('data-scheme="sw3"', page)
        self.assertIn("sw_level3", page)
        self.assertNotIn("同花顺分类", page)
        self.assertLess(page.index('data-sort="pb"'), page.index('data-sort="return_5d"'))
        self.assertIn("switchTab('constituents')", app)
        self.assertIn('/index-constituents.html', app)


if __name__ == "__main__":
    unittest.main()
