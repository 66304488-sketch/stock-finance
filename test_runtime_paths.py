import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class RuntimePathsTest(unittest.TestCase):
    def _load(self, resource_dir, data_dir, user_dir):
        env = {
            "STOCK_FINANCE_RESOURCE_DIR": resource_dir,
            "STOCK_FINANCE_DATA_DIR": data_dir,
            "STOCK_FINANCE_USER_DATA_DIR": user_dir,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            sys.modules.pop("runtime_paths", None)
            return importlib.import_module("runtime_paths")

    def test_initialization_copies_only_missing_data_files(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            legacy_dir = Path(root, "legacy")
            resource_dir.mkdir()
            data_dir.mkdir()
            legacy_dir.mkdir()
            (legacy_dir / "config.json").write_text("legacy-config", encoding="utf-8")
            (resource_dir / "new_highs_data_month.json").write_text("seed", encoding="utf-8")
            (resource_dir / "capital_flow.json").write_text("capital", encoding="utf-8")
            (resource_dir / "app.html").write_text("page", encoding="utf-8")
            (resource_dir / "industry_map_ths.json").write_text("map", encoding="utf-8")
            (data_dir / "new_highs_data_month.json").write_text("user", encoding="utf-8")

            paths = self._load(str(resource_dir), str(data_dir), str(user_dir))
            paths.LEGACY_USER_DATA_DIR = str(legacy_dir)
            copied = paths.initialize_data_dir()

            self.assertEqual((user_dir / "config.json").read_text(), "legacy-config")
            self.assertEqual((data_dir / "new_highs_data_month.json").read_text(), "user")
            self.assertEqual((data_dir / "capital_flow.json").read_text(), "capital")
            self.assertFalse((data_dir / "app.html").exists())
            self.assertFalse((data_dir / "industry_map_ths.json").exists())
            self.assertEqual(copied, ["capital_flow.json"])
            (data_dir / "capital_flow.json").unlink()
            self.assertEqual(paths.initialize_data_dir(), [])
            self.assertFalse((data_dir / "capital_flow.json").exists())

    def test_runtime_file_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            paths = self._load(root, root, root)
            self.assertTrue(paths.is_runtime_data_file("new_lows_data_month.json"))
            self.assertTrue(paths.is_runtime_data_file("industry-heatmap-standalone.html"))
            self.assertTrue(paths.is_runtime_data_file("etf_prediction_log.jsonl"))
            self.assertFalse(paths.is_runtime_data_file("config.json"))
            self.assertFalse(paths.is_runtime_data_file("app.html"))
            self.assertFalse(paths.is_runtime_data_file("../config.json"))

    def test_etf_model_upgrade_refreshes_only_derived_cache_after_initialization(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            resource_dir.mkdir()
            data_dir.mkdir()
            (data_dir / ".stock-finance-initialized").touch()
            (data_dir / "etf_recommend_sw3.json").write_text(
                '{"model_version":"legacy"}', encoding="utf-8")
            (data_dir / "etf_snapshot.json").write_text("old snapshot", encoding="utf-8")
            (data_dir / "momentum_etf_pool.json").write_text("user pool", encoding="utf-8")
            (resource_dir / "etf_recommend_sw3.json").write_text(
                '{"model_version":"ignition-v2"}', encoding="utf-8")
            (resource_dir / "etf_snapshot.json").write_text("new snapshot", encoding="utf-8")

            paths = self._load(str(resource_dir), str(data_dir), str(user_dir))
            copied = paths.initialize_data_dir()

            self.assertEqual(set(copied), {"etf_recommend_sw3.json", "etf_snapshot.json"})
            self.assertIn("ignition-v2", (data_dir / "etf_recommend_sw3.json").read_text())
            self.assertEqual((data_dir / "etf_snapshot.json").read_text(), "new snapshot")
            self.assertEqual((data_dir / "momentum_etf_pool.json").read_text(), "user pool")

    def test_existing_install_receives_new_crowding_scheme_seeds(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            resource_dir.mkdir()
            data_dir.mkdir()
            (data_dir / ".stock-finance-initialized").touch()
            for filename in (
                "crowding_ths.json",
                "crowding_sw3.json",
                "crowding_detail_ths.json",
                "crowding_detail_sw3.json",
            ):
                (resource_dir / filename).write_text(
                    f'{{"name":"{filename}"}}', encoding="utf-8")

            paths = self._load(
                str(resource_dir), str(data_dir), str(user_dir))
            copied = paths.initialize_data_dir()

            self.assertEqual(
                set(copied), set(paths.CROWDING_SCHEME_SEED_FILES))
            for filename in paths.CROWDING_SCHEME_SEED_FILES:
                self.assertTrue((data_dir / filename).exists())

    def test_existing_install_receives_capital_flow_v2_seeds(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            resource_dir.mkdir()
            data_dir.mkdir()
            (data_dir / ".stock-finance-initialized").touch()
            for filename in (
                "capital_flow_v2.json",
                "capital_flow_v2_ths.json",
                "capital_flow_v2_sw3.json",
            ):
                (resource_dir / filename).write_text(
                    f'{{"model_version":"turnover-momentum-v2",'
                    f'"name":"{filename}"}}',
                    encoding="utf-8",
                )
            # Existing user data must never be replaced.
            (data_dir / "capital_flow_v2.json").write_text(
                '{"model_version":"user"}', encoding="utf-8")

            paths = self._load(
                str(resource_dir), str(data_dir), str(user_dir))
            copied = paths.initialize_data_dir()

            self.assertEqual(
                set(copied),
                {"capital_flow_v2_ths.json", "capital_flow_v2_sw3.json"},
            )
            self.assertIn(
                '"user"', (data_dir / "capital_flow_v2.json").read_text())
            for filename in (
                "capital_flow_v2_ths.json",
                "capital_flow_v2_sw3.json",
            ):
                self.assertTrue((data_dir / filename).exists())

    def test_existing_install_receives_missing_market_cap_v2_seeds(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            resource_dir.mkdir()
            data_dir.mkdir()
            (data_dir / ".stock-finance-initialized").touch()
            for filename in (
                "market_cap_v2.json",
                "market_cap_v2_ths.json",
                "market_cap_v2_sw3.json",
                "market_cap_share_history_cninfo.json",
                "market_cap_point_in_time_shares.json",
            ):
                (resource_dir / filename).write_text(
                    f'{{"name":"{filename}"}}', encoding="utf-8")
            (data_dir / "market_cap_v2.json").write_text(
                '{"model_version":"user"}', encoding="utf-8")

            paths = self._load(
                str(resource_dir), str(data_dir), str(user_dir))
            copied = paths.initialize_data_dir()

            self.assertEqual(
                set(copied),
                set(paths.MARKET_CAP_V2_SEED_FILES)
                - {"market_cap_v2.json"},
            )
            self.assertIn(
                '"user"', (data_dir / "market_cap_v2.json").read_text())

    def test_new_feature_data_is_seeded_and_managed(self):
        with tempfile.TemporaryDirectory() as root:
            paths = self._load(root, root, root)
            for filename in (
                "intraday_temperature_history.json",
                "market_temperature.json",
                "crowding.json",
                "crowding_detail.json",
                "crowding_ths.json",
                "crowding_sw3.json",
                "crowding_detail_ths.json",
                "crowding_detail_sw3.json",
                "crowding_external.json",
                "capital_flow_v2.json",
                "capital_flow_v2_ths.json",
                "capital_flow_v2_sw3.json",
                "market_cap_v2.json",
                "market_cap_v2_ths.json",
                "market_cap_v2_sw3.json",
                "market_cap_share_history_cninfo.json",
                "market_cap_point_in_time_shares.json",
                "etf_backtest.json",
                "momentum_state.json",
                "index_constituents_cache.json",
            ):
                self.assertTrue(paths.is_runtime_data_file(filename), filename)


if __name__ == "__main__":
    unittest.main()
