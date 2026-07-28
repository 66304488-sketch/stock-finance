import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export_json
import pandas as pd
import update_engine
from db import StockDB


APP_HTML = Path(__file__).with_name("static") / "app.html"


class UpdateRangeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_HTML.read_text(encoding="utf-8")

    def _function_source(self, name):
        match = re.search(
            rf"async function {name}\(\) \{{(.*?)(?=\n(?:async )?function |\nvar _pollRetry)",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, f"missing JavaScript function {name}")
        return match.group(1)

    def test_market_cap_uses_selected_range(self):
        source = self._function_source("runMarketCap")
        self.assertIn("var days = getRefreshDays();", source)
        self.assertRegex(source, r"days\s*:\s*days")

    def test_capital_flow_uses_selected_range(self):
        source = self._function_source("runCapitalFlow")
        self.assertIn("var days = getRefreshDays();", source)
        self.assertRegex(source, r"days\s*:\s*days")

    def test_market_cap_export_is_oldest_to_newest(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            db.insert_market_cap([
                {"date": "20260709", "scheme": "sw", "industry": "银行", "mcap": 100,
                 "stock_count": 1, "is_total": 0},
                {"date": "20260710", "scheme": "sw", "industry": "银行", "mcap": 110,
                 "stock_count": 1, "is_total": 0},
            ])
            with mock.patch.object(export_json, "STATIC", root):
                export_json._make_market_cap(db, "sw", "")
            with open(os.path.join(root, "market_cap.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual([d["label"] for d in data["dates"]], ["7月9日", "7月10日"])
            bank = next(row for row in data["industries"] if row["industry"] == "银行")
            self.assertEqual(bank["mcap"], 110)
            self.assertEqual(bank["change_pct"], 10.0)
            db.close()

    def test_capital_flow_cumulative_runs_forward_in_time(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            db.insert_capital_flow([
                {"date":"20260709","scheme":"sw","industry":"银行","turnover":100,
                 "net_flow":20,"stock_count":1,"is_total":0},
                {"date":"20260710","scheme":"sw","industry":"银行","turnover":110,
                 "net_flow":10,"stock_count":1,"is_total":0},
            ])
            export_json._make_capital_flow(db, "sw", "", root)
            data = json.loads(Path(root, "capital_flow.json").read_text(encoding="utf-8"))
            bank = next(row for row in data["industries"] if row["industry"] == "银行")
            self.assertEqual([d["label"] for d in data["dates"]], ["7月9日", "7月10日"])
            self.assertEqual(bank["cumulative_flow"], [20, 30])
            self.assertEqual(data["flow_method"], "signed_turnover_proxy")
            db.close()

    def test_legacy_share_cache_is_refreshed_and_upgraded(self):
        fields = [""] * 88
        fields[72] = "19405600653"
        fields[73] = "19405918198"
        response = mock.Mock()
        response.text = 'v_sz000001="' + "~".join(fields) + '";'
        with tempfile.TemporaryDirectory() as root:
            cache_path = os.path.join(root, "stock_shares.json")
            Path(cache_path).write_text('{"000001":1}', encoding="utf-8")
            with mock.patch.object(update_engine, "data_path", return_value=cache_path), \
                 mock.patch("requests.get", return_value=response):
                snapshot = update_engine._load_share_snapshot(["000001"])
            self.assertEqual(
                snapshot["total_shares"]["000001"], 19405918198)
            self.assertEqual(
                snapshot["circulating_shares"]["000001"], 19405600653)
            payload = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 3)
            self.assertEqual(payload["source"]["total_shares_field"], 73)
            self.assertEqual(
                payload["source"]["circulating_shares_field"], 72)
            self.assertIn("updated_at", payload)

    def test_tencent_share_fields_are_not_reversed_for_byd(self):
        fields = [""] * 88
        fields[72] = "3486613500"
        fields[73] = "9117197565"
        response = mock.Mock()
        response.text = 'v_sz002594="' + "~".join(fields) + '";'
        with tempfile.TemporaryDirectory() as root:
            cache_path = os.path.join(root, "stock_shares.json")
            with mock.patch.object(
                update_engine, "data_path", return_value=cache_path
            ), mock.patch("requests.get", return_value=response):
                snapshot = update_engine._load_share_snapshot(["002594"])

        self.assertEqual(
            snapshot["total_shares"]["002594"], 9117197565)
        self.assertEqual(
            snapshot["circulating_shares"]["002594"], 3486613500)

    def test_legacy_cache_and_offline_tencent_fail_before_db_replace(self):
        with tempfile.TemporaryDirectory() as root:
            cache_path = os.path.join(root, "stock_shares.json")
            export_path = Path(root, "market_cap.json")
            Path(cache_path).write_text(
                '{"version":2,"shares":{"000001":19405600653}}',
                encoding="utf-8",
            )
            export_path.write_text('{"sentinel":"keep"}', encoding="utf-8")
            db = mock.Mock()
            with (
                mock.patch.object(
                    update_engine, "data_path", return_value=cache_path),
                mock.patch.object(
                    update_engine, "_get_trade_dates",
                    return_value=["20260723"]),
                mock.patch.object(
                    update_engine, "get_active_codes",
                    return_value=["000001"]),
                mock.patch.object(update_engine, "get_db", return_value=db),
                mock.patch(
                    "requests.get", side_effect=OSError("offline")),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Total-share coverage too low"
                ):
                    update_engine.update_market_cap(
                        ["20260723"], schemes=["sw"])

            db.replace_market_cap_batch.assert_not_called()
            self.assertEqual(
                json.loads(export_path.read_text(encoding="utf-8")),
                {"sentinel": "keep"},
            )

    def test_suspended_stock_close_is_forward_filled(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            frame = pd.DataFrame({
                "date": pd.to_datetime(["2026-07-20", "2026-07-22"]),
                "close": [10.0, 11.0],
            })
            cache = mock.Mock()
            cache.ensure_dates.return_value = {"000001": frame}
            snapshot = {
                "total_shares": {"000001": 1000},
                "circulating_shares": {"000001": 800},
            }
            with (
                mock.patch.object(
                    update_engine, "_get_trade_dates",
                    return_value=["20260720", "20260721", "20260722"]),
                mock.patch.object(
                    update_engine, "get_active_codes",
                    return_value=["000001"]),
                mock.patch.object(update_engine, "get_db", return_value=db),
                mock.patch.object(
                    update_engine, "_load_share_snapshot",
                    return_value=snapshot),
                mock.patch.object(
                    update_engine, "_load_ind_map",
                    return_value={"000001": "银行"}),
                mock.patch.object(
                    update_engine, "KlineCache", return_value=cache),
                mock.patch.object(
                    update_engine.ak,
                    "stock_info_a_code_name",
                    return_value=pd.DataFrame({
                        "code": ["000001"],
                        "name": ["平安银行"],
                    })),
                mock.patch(
                    "share_history_cninfo."
                    "refresh_point_in_time_share_history",
                    return_value={
                        "total_shares": {},
                        "circulating_a_shares": {},
                        "events": {},
                    },
                ),
            ):
                update_engine.update_market_cap(
                    ["20260720", "20260721", "20260722"],
                    schemes=["sw"],
                    min_coverage=0,
                )

            row = db.conn.execute(
                "SELECT price,change_pct,mcap FROM stock_details "
                "WHERE scheme='sw' AND date='20260721' AND code='000001'"
            ).fetchone()
            self.assertEqual(row, (10.0, 0.0, 10000.0))
            next_row = db.conn.execute(
                "SELECT price,change_pct FROM stock_details "
                "WHERE scheme='sw' AND date='20260722' AND code='000001'"
            ).fetchone()
            self.assertEqual(next_row, (11.0, 10.0))
            db.close()

    def test_market_cap_keeps_unmapped_new_stock_in_other(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            frame = pd.DataFrame({
                "date": pd.to_datetime(["2026-07-24"]),
                "close": [20.0],
            })
            cache = mock.Mock()
            cache.ensure_dates.return_value = {"688806": frame}
            snapshot = {
                "total_shares": {"688806": 1000},
                "circulating_shares": {"688806": 500},
            }
            with (
                mock.patch.object(
                    update_engine, "_get_trade_dates",
                    return_value=["20260724"]),
                mock.patch.object(
                    update_engine, "get_active_codes",
                    return_value=["688806"]),
                mock.patch.object(update_engine, "get_db", return_value=db),
                mock.patch.object(
                    update_engine, "_load_share_snapshot",
                    return_value=snapshot),
                mock.patch.object(
                    update_engine, "_load_ind_map", return_value={}),
                mock.patch.object(
                    update_engine, "KlineCache", return_value=cache),
                mock.patch.object(
                    update_engine.ak,
                    "stock_info_a_code_name",
                    return_value=pd.DataFrame({
                        "code": ["688806"],
                        "name": ["C泰诺"],
                    })),
                mock.patch(
                    "share_history_cninfo."
                    "refresh_point_in_time_share_history",
                    return_value={
                        "total_shares": {},
                        "circulating_a_shares": {},
                        "events": {},
                    },
                ),
            ):
                update_engine.update_market_cap(
                    ["20260724"], schemes=["sw"], min_coverage=0)

            row = db.conn.execute(
                "SELECT industry,mcap FROM stock_details "
                "WHERE scheme='sw' AND date='20260724' AND code='688806'"
            ).fetchone()
            self.assertEqual(row, ("其他", 20000.0))
            db.close()

    def test_aggregate_only_refresh_preserves_bundled_v2_seed(self):
        with tempfile.TemporaryDirectory() as root:
            db = StockDB(os.path.join(root, "data.db"))
            db.insert_market_cap([{
                "date": "20260723",
                "scheme": "sw",
                "industry": "全市场合计",
                "mcap": 100,
                "stock_count": 1,
                "is_total": 1,
            }])
            output = Path(root, "market_cap_v2.json")
            output.write_text(
                '{"model_version":"bundled-seed"}', encoding="utf-8")

            export_json._make_market_cap_v2(db, "sw", "", root)

            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"model_version": "bundled-seed"},
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
