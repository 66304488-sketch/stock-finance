import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import export_json
import kline_cache
import update_engine
from db import StockDB
from kline_cache import KlineCache


class HeatmapDatabaseTest(unittest.TestCase):
    def test_heatmap_batch_rolls_back_every_slice(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            old = {"date":"20260710","period":"month","scheme":"sw","industry":"银行",
                   "count":7,"total_stocks":10,"is_total":0}
            db.insert_highs_lows([old], "highs")
            duplicate = dict(old, count=1)
            with self.assertRaises(Exception):
                db.replace_heatmap_batch([{
                    "direction":"highs", "period":"month", "scheme":"sw",
                    "dates":["20260710"], "records":[duplicate, duplicate], "detail_records":[],
                }])
            count = db.conn.execute(
                "SELECT count FROM daily_new_highs WHERE date='20260710' AND industry='银行'"
            ).fetchone()[0]
            self.assertEqual(count, 7)
            db.close()
    def test_replace_slice_removes_stale_details_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "test.db"))
            dates = ["20260710"]
            old_detail = [{
                "date": dates[0], "direction": "lows", "period": "month",
                "scheme": "sw", "industry": "银行", "code": "000001",
                "name": "旧记录", "price": 10, "change_pct": -1, "mcap": None,
            }]
            db.replace_heatmap_slice([], old_detail, "lows", "month", "sw", dates)

            records = [{
                "date": dates[0], "period": "month", "scheme": "sw",
                "industry": "银行", "count": 1, "total_stocks": 2, "is_total": 0,
            }]
            new_detail = [{**old_detail[0], "code": "000002", "name": "新记录"}]
            db.replace_heatmap_slice(records, new_detail, "lows", "month", "sw", dates)

            codes = db.conn.execute(
                "SELECT code FROM stock_details WHERE direction='lows'"
            ).fetchall()
            self.assertEqual(codes, [("000002",)])
            db.close()

    def test_reopen_repairs_legacy_capital_flow_scale(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "test.db")
            db = StockDB(path)
            records = []
            for date, total in [
                ("20260708", 3_000_000_000_000),
                ("20260709", 3_200_000_000_000),
                ("20260710", 2_900_000_000_000),
                ("20260713", 275_000_000_000_000),
            ]:
                records.extend([
                    {"date": date, "scheme": "sw", "industry": "银行", "turnover": total,
                     "net_flow": total / 2, "stock_count": 10, "is_total": 0},
                    {"date": date, "scheme": "sw", "industry": "全市场合计", "turnover": total,
                     "net_flow": total / 2, "stock_count": 10, "is_total": 1},
                ])
            db.insert_capital_flow(records)
            db.close()

            repaired = StockDB(path)
            turnover, net_flow = repaired.conn.execute(
                "SELECT turnover, net_flow FROM daily_capital_flow "
                "WHERE date='20260713' AND scheme='sw' AND industry='银行'"
            ).fetchone()
            self.assertEqual(turnover, 2_750_000_000_000)
            self.assertEqual(net_flow, 1_375_000_000_000)
            repaired.close()


class HeatmapEngineTest(unittest.TestCase):
    def test_coverage_rejects_any_sparse_target_date(self):
        full = pd.DataFrame({"date":pd.to_datetime(["2026-07-09","2026-07-10"]),"close":[10,11]})
        sparse = pd.DataFrame({"date":pd.to_datetime(["2026-07-10"]),"close":[20]})
        with self.assertRaisesRegex(RuntimeError, "20260709"):
            update_engine._validate_date_coverage(
                {"000001":full,"000002":sparse}, ["000001","000002"],
                ["20260709","20260710"], min_coverage=0.9,
            )

    def test_intraday_cache_read_does_not_persist(self):
        with tempfile.TemporaryDirectory() as root:
            frame = pd.DataFrame({"date":pd.to_datetime(["2026-07-10"]),"close":[10.0]})
            cache = KlineCache(os.path.join(root, "cache.pkl"))
            cache._cache = {"version":2,"updated_at":"","codes":{"000001"},
                            "data":{"000001":frame},"alltime_high_before":{},
                            "alltime_low_before":{},"volume_scale_checked":True}
            with mock.patch.object(cache, "_update_existing"), mock.patch.object(cache, "_save") as save:
                cache.ensure(["000001"], "20260710", persist=False, update_live=False)
            save.assert_not_called()

    def test_short_history_has_no_alltime_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            frame = pd.DataFrame({"date":pd.date_range("2026-06-01", periods=10),
                                  "close":list(range(10,20))})
            cache = KlineCache(os.path.join(root, "cache.pkl"))
            cache._cache = {"version":2,"updated_at":"","codes":set(),"data":{},
                            "alltime_high_before":{},"alltime_low_before":{},
                            "volume_scale_checked":True}
            with mock.patch("kline_cache.fetch_klines_sina", return_value={"000001":frame}):
                cache._init_full(["000001"])
            self.assertNotIn("000001", cache.alltime_high_before)
            self.assertNotIn("000001", cache.alltime_low_before)
    def test_sina_spot_volume_is_already_in_shares(self):
        fields = [""] * 33
        fields[0] = "测试股"
        fields[1] = "10.0"
        fields[2] = "9.8"
        fields[3] = "10.1"
        fields[4] = "10.2"
        fields[5] = "9.7"
        fields[8] = "123456"
        response = mock.Mock()
        response.text = 'var hq_str_sz000001="' + ",".join(fields) + '";'
        with mock.patch.object(kline_cache.requests, "get", return_value=response):
            result = kline_cache.fetch_spot(["000001"], "20260714")
        self.assertEqual(result["000001"]["volume"], 123456)

    def test_cache_repairs_legacy_volume_scale(self):
        cache = KlineCache("/tmp/not-used.pkl")
        dates = pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-10"])
        cache._cache = {
            "version": 2, "updated_at": "", "codes": {"000001", "000002"},
            "data": {
                "000001": pd.DataFrame({"date": dates, "close": [10, 10, 10], "volume": [100, 10_000, 110]}),
                "000002": pd.DataFrame({"date": dates, "close": [10, 10, 10], "volume": [100, 10_000, 110]}),
            },
            "alltime_high_before": {}, "alltime_low_before": {},
        }
        self.assertEqual(cache._repair_volume_scale_anomalies(), ["20260709"])
        self.assertEqual(cache._cache["data"]["000001"].iloc[1]["volume"], 100)

    def test_custom_window_snapshot_uses_close_definition(self):
        dates = pd.date_range("2026-06-01", periods=30, freq="B")
        frame = pd.DataFrame({
            "date": dates,
            "close": [float(value) for value in range(1, 31)],
        })
        targets = [dates[-2].strftime("%Y%m%d"), dates[-1].strftime("%Y%m%d")]
        fake_cache = mock.Mock()
        fake_cache.ensure_dates.return_value = {"000001": frame}
        with mock.patch.object(update_engine, "get_active_codes", return_value=["000001"]), \
             mock.patch.object(update_engine, "_load_ind_map", return_value={"000001": "银行"}), \
             mock.patch.object(update_engine, "KlineCache", return_value=fake_cache), \
             mock.patch.object(update_engine, "data_path", return_value="/missing/shares.json"), \
             mock.patch.object(update_engine.ak, "stock_info_a_code_name", return_value=pd.DataFrame({
                 "code": ["000001"], "name": ["测试股"],
             })):
            snapshot = update_engine.build_custom_heatmap_snapshot(
                20, scheme="sw", target_dates=targets,
            )
        high_total = next(row for row in snapshot["highs"]["industries"] if row.get("is_total"))
        low_total = next(row for row in snapshot["lows"]["industries"] if row.get("is_total"))
        self.assertEqual(high_total["daily_counts"], [1, 1])
        self.assertEqual(low_total["daily_counts"], [0, 0])
        self.assertEqual(snapshot["highs"]["type_label"], "创20日新高")

    def test_lows_only_update_calculates_lows_and_reuses_one_cache(self):
        dates = pd.date_range("2026-06-01", periods=22, freq="B")
        frame = pd.DataFrame({"date": dates, "close": list(range(30, 8, -1))})
        target = dates[-1].strftime("%Y%m%d")
        fake_db = mock.Mock()
        fake_cache = mock.Mock()
        fake_cache.ensure_dates.return_value = {"000001": frame}
        fake_cache.alltime_high_before = {}
        fake_cache.alltime_low_before = {}

        with mock.patch.object(update_engine, "get_db", return_value=fake_db), \
             mock.patch.object(update_engine, "get_active_codes", return_value=["000001"]), \
             mock.patch.object(update_engine, "_load_ind_map", return_value={"000001": "银行"}), \
             mock.patch.object(update_engine, "KlineCache", return_value=fake_cache), \
             mock.patch.object(update_engine.ak, "stock_info_a_code_name", return_value=pd.DataFrame({
                 "code": ["000001"], "name": ["测试股"],
             })):
            result = update_engine.update_highs_lows(
                [target], schemes=["ths"], periods=["month"], directions=["lows"]
            )

        fake_cache.ensure_dates.assert_called_once()
        fake_db.replace_heatmap_batch.assert_called_once()
        item = fake_db.replace_heatmap_batch.call_args.args[0][0]
        self.assertEqual((item["direction"], item["period"], item["scheme"]), ("lows", "month", "ths"))
        self.assertEqual(item["detail_records"][0]["code"], "000001")
        self.assertNotIn(("ths", "month"), result["highs"])

    def test_cache_backfills_market_wide_date_gap(self):
        with tempfile.TemporaryDirectory() as root:
            cache = KlineCache(os.path.join(root, "cache.pkl"))
            sparse = pd.DataFrame({
                "date": pd.to_datetime(["2026-07-08", "2026-07-10"]),
                "close": [10.0, 11.0],
            })
            complete = pd.DataFrame({
                "date": pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-10"]),
                "close": [10.0, 10.5, 11.0],
            })
            cache._cache = {
                "version": 2, "updated_at": "", "codes": {"000001"},
                "data": {"000001": sparse}, "alltime_high_before": {},
                "alltime_low_before": {},
            }
            with mock.patch("kline_cache.fetch_klines_sina", return_value={"000001": complete}):
                data = cache.ensure_dates(["000001"], ["20260709", "20260710"])
            dates = set(data["000001"]["date"].dt.strftime("%Y%m%d"))
            self.assertIn("20260709", dates)

    def test_cache_replaces_same_day_intraday_row(self):
        with tempfile.TemporaryDirectory() as root:
            cache = KlineCache(os.path.join(root, "cache.pkl"))
            today = pd.Timestamp.now().normalize()
            cache._cache = {
                "version": 2, "updated_at": "", "codes": {"000001"},
                "data": {"000001": pd.DataFrame([{
                    "date": today, "open": 10.0, "high": 10.2, "low": 9.9,
                    "close": 10.0, "volume": 100,
                }])},
                "alltime_high_before": {}, "alltime_low_before": {},
            }
            refreshed = {
                "date": today, "open": 10.0, "high": 11.0, "low": 9.9,
                "close": 10.8, "volume": 200, "prev_close": 10.0,
                "change_pct": 8.0, "name": "测试股",
            }
            with mock.patch("kline_cache.fetch_spot", return_value={"000001": refreshed}):
                cache._update_existing(["000001"], today.strftime("%Y%m%d"))
            frame = cache._cache["data"]["000001"]
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["close"], 10.8)
            self.assertEqual(frame.iloc[0]["high"], 11.0)


class AtomicExportTest(unittest.TestCase):
    def test_atomic_dump_replaces_valid_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "data.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"old": True}, f)
            export_json._atomic_json_dump({"new": True}, path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"new": True})
            self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(root)))

    def test_export_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            target = os.path.join(root, "capital_flow.json")
            with open(target, "w", encoding="utf-8") as handle:
                json.dump({"snapshot": "old"}, handle)
            with mock.patch.object(export_json, "get_db", return_value=db), \
                 mock.patch.object(export_json, "_make_capital_flow", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    export_json.export_all(root)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"snapshot": "old"})
            db.close()


if __name__ == "__main__":
    unittest.main()
