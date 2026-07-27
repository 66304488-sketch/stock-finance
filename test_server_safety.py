import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

import server


class ServerSafetyTest(unittest.TestCase):
    @staticmethod
    def _index_monitor_snapshot(
        *,
        index_code="000300",
        signal_ready=True,
        live_weight=99.0,
    ):
        return {
            "index": {"code": index_code, "name": "测试指数"},
            "index_quote": {
                "price": 4000.0,
                "prev_close": 3980.0,
                "change_pct": 0.5,
                "quote_time": "20260727094500",
            },
            "replication": {
                "index_prev_close": 3980.0,
                "replication_residual_bp": 1.2,
                "effective_live_weight_pct": live_weight,
                "confidence": "high",
                "signal_ready": signal_ready,
                "quality_gate": {"passed": signal_ready},
            },
            "breadth": {
                "weighted_advance_pct": 68.0,
                "equal_weight_advance_pct": 61.0,
                "weighted_above_vwap_pct": 64.0,
            },
            "driver_concentration": {
                "top5_abs_contribution_share_pct": 42.0,
                "effective_driver_count": 12.5,
            },
            "intraday": {
                "windows": {
                    "1m": {"replicated_return_change_bp": 3.0},
                    "5m": {
                        "replicated_return_change_bp": 12.0,
                        "official_return_change_bp": 11.0,
                    },
                }
            },
        }

    def test_index_monitor_requires_quality_gate_and_keeps_basis_descriptive(self):
        snapshot = self._index_monitor_snapshot(
            signal_ready=False,
            live_weight=72.0,
        )
        futures = {
            "quote": {
                "contract": "IF2608",
                "basis": -12.5,
                "stale": True,
                "history": {
                    "5m": {
                        "futures_return_pct": 0.16,
                        "basis_change": 2.3,
                    }
                },
            }
        }

        monitor = server._build_index_futures_monitor(snapshot, futures)

        self.assertEqual(monitor["state_label"], "证据不足·仅供观察")
        self.assertFalse(monitor["quality"]["signal_ready"])
        self.assertEqual(monitor["lead_lag_label"], "期货领先同向")
        self.assertNotIn("direction", monitor)
        self.assertTrue(any("禁止输出方向结论" in item for item in monitor["risks"]))
        self.assertTrue(any("期指行情已过期" in item for item in monitor["risks"]))

    def test_index_constituents_route_attaches_selected_futures_and_monitor(self):
        snapshot = self._index_monitor_snapshot()
        futures = {
            "quote": {
                "contract": "IF2608",
                "mark": 4012.0,
                "basis": 12.0,
                "history": {
                    "5m": {
                        "futures_return_pct": 0.12,
                        "basis_change": 1.5,
                    }
                },
            }
        }
        with (
            mock.patch(
                "index_constituents.get_index_snapshot",
                return_value=snapshot,
            ),
            mock.patch(
                "index_futures.get_product_overview",
                return_value=futures,
            ),
        ):
            result = asyncio.run(
                server.index_constituents(index="000300", refresh=True)
            )

        self.assertEqual(result["futures"]["contract"], "IF2608")
        self.assertEqual(result["monitor"]["state_label"], "上行增强·期现确认")
        self.assertEqual(result["monitor"]["impulse_5m"], 4.776)
        self.assertEqual(result["quality"]["confidence"], 95)

    def test_tencent_quote_maps_total_and_circulating_market_cap(self):
        fields = [""] * 88
        fields[1] = "比亚迪"
        fields[3] = "91.85"
        fields[44] = "3203.85"
        fields[45] = "8377.79"
        response = SimpleNamespace(
            text='v_sz002594="' + "~".join(fields) + '";')

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url):
                return response

        with mock.patch.object(
            server.httpx, "AsyncClient", return_value=FakeClient()
        ):
            quote = asyncio.run(server.fetch_quote("002594"))

        self.assertEqual(quote["marketCap"], 8377.79)
        self.assertEqual(quote["circMarketCap"], 3203.85)

    def test_backup_creates_versioned_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            data_dir = root_path / "data"
            user_dir = root_path / "user"
            backup_dir = root_path / "backups"
            data_dir.mkdir()
            user_dir.mkdir()
            (data_dir / "capital_flow.json").write_text('{"ok":true}', encoding="utf-8")
            (user_dir / "config.json").write_text('{"ai_provider":"anthropic"}', encoding="utf-8")

            with (
                mock.patch.object(server, "data_dir", str(data_dir)),
                mock.patch.object(server, "USER_DATA_DIR", str(user_dir)),
                mock.patch.object(server, "_get_backup_dir", return_value=str(backup_dir)),
            ):
                result = server._backup_data_sync()

            snapshot = Path(result["snapshot"])
            self.assertEqual(snapshot.parent, backup_dir)
            self.assertTrue((snapshot / "capital_flow.json").exists())
            self.assertTrue((snapshot / "config.json").exists())
            self.assertFalse((backup_dir / "capital_flow.json").exists())

    def test_restore_ignores_unrecognized_files(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            data_dir = root_path / "data"
            user_dir = root_path / "user"
            backup_dir = root_path / "backups"
            snapshot = backup_dir / "20260725-120000"
            data_dir.mkdir()
            user_dir.mkdir()
            snapshot.mkdir(parents=True)
            (snapshot / "capital_flow.json").write_text('{"restored":true}', encoding="utf-8")
            (snapshot / "unrelated.txt").write_text("do not restore", encoding="utf-8")

            with (
                mock.patch.object(server, "data_dir", str(data_dir)),
                mock.patch.object(server, "USER_DATA_DIR", str(user_dir)),
                mock.patch.object(server, "_get_backup_dir", return_value=str(backup_dir)),
            ):
                result = server._restore_data_sync()

            self.assertEqual(result["files"], ["capital_flow.json"])
            self.assertTrue((data_dir / "capital_flow.json").exists())
            self.assertFalse((data_dir / "unrelated.txt").exists())

    def test_clear_preserves_strategy_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            data_dir.mkdir()
            user_dir.mkdir()
            (data_dir / "capital_flow.json").write_text("{}", encoding="utf-8")
            (data_dir / "momentum_etf_pool.json").write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(server, "data_dir", str(data_dir)),
                mock.patch.object(server, "USER_DATA_DIR", str(user_dir)),
                mock.patch.object(server, "reset_db"),
            ):
                deleted = server._clear_data_sync()

            self.assertIn("capital_flow.json", deleted)
            self.assertTrue((data_dir / "momentum_etf_pool.json").exists())

    def test_momentum_parameters_reject_invalid_ranges(self):
        with self.assertRaises(HTTPException):
            server._validate_momentum_params({"lookback_days": 0}, {})
        with self.assertRaises(HTTPException):
            server._validate_momentum_params(
                {"score_range_min": 2, "score_range_max": 1},
                {},
            )

        validated = server._validate_momentum_params(
            {"lookback_days": 25, "r2_threshold": 0.4},
            {"score_range": [0, 5]},
        )
        self.assertEqual(validated["lookback_days"], 25)
        self.assertEqual(validated["score_range"], [0.0, 5.0])

    def test_analysis_subprocess_failure_is_an_http_error(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="analysis failed")
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(server.settings_run_analysis())
        self.assertEqual(caught.exception.status_code, 500)
        self.assertIn("analysis failed", caught.exception.detail)

    def test_missing_info_reuses_one_trade_calendar_result(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            payload = '{"dates":[{"full_label":"2026年7月1日"}]}'
            for filename in (
                "new_highs_data_month.json",
                "new_lows_data_month.json",
                "capital_flow.json",
                "market_cap.json",
            ):
                (data_dir / filename).write_text(payload, encoding="utf-8")
            dates = ",".join(f"202607{day:02d}" for day in range(1, 21))

            server._invalidate_missing_info_cache()
            with (
                mock.patch.object(server, "data_dir", str(data_dir)),
                mock.patch.object(server, "_get_trade_date_args", return_value=dates) as calendar,
            ):
                info = server._get_missing_info(force=True)

            self.assertEqual(calendar.call_count, 1)
            self.assertEqual(info["latest_trade_date"], "20260720")


if __name__ == "__main__":
    unittest.main()
