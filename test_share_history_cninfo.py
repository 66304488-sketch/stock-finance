import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from share_history_cninfo import (
    build_point_in_time_share_payload,
    fetch_cninfo_share_records,
    import_raw_checkpoint,
    normalize_stock_records,
    refresh_cninfo_share_cache,
)


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"records": []}


class ShareHistoryCninfoTest(unittest.TestCase):
    def test_units_are_calibrated_against_total_and_circulating_fields(self):
        item = normalize_stock_records(
            "002594",
            [{
                "VARYDATE": "2026-07-01",
                "F003N": 911719.7565,
                "F022N": 348661.35,
                "F002V": "股本变动",
            }],
            9_117_197_565,
            3_486_613_500,
        )
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["calibrated_unit_multiplier"], 10000.0)
        self.assertTrue(item["circulating_calibrated"])
        self.assertEqual(
            item["events"][0]["total_shares"], 9_117_197_565)
        self.assertEqual(
            item["events"][0]["circulating_a_shares"], 3_486_613_500)

    def test_unresolved_unit_is_isolated_instead_of_guessed(self):
        item = normalize_stock_records(
            "000001",
            [{"VARYDATE": "2026-07-01", "F003N": 100}],
            5_000,
        )
        self.assertEqual(item["status"], "unit_unresolved")
        self.assertEqual(item["events"], [])
        self.assertIsNone(item["calibrated_unit_multiplier"])

    def test_bad_f022_validation_never_becomes_free_float_or_proxy(self):
        item = normalize_stock_records(
            "000001",
            [{
                "VARYDATE": "2026-07-01",
                "F003N": 100,
                "F022N": 95,
            }],
            1_000_000,
            100_000,
        )
        self.assertEqual(item["status"], "ok")
        self.assertFalse(item["circulating_calibrated"])
        self.assertIsNone(
            item["events"][0]["circulating_a_shares"])

    def test_enckey_is_created_on_caller_before_worker_posts(self):
        calls = []

        def enckey_factory():
            calls.append(("key", threading.current_thread().name))
            return "test-key"

        def post(*_args, **_kwargs):
            calls.append(("post", threading.current_thread().name))
            return _Response()

        result = fetch_cninfo_share_records(
            ["000001", "000002"],
            "20260101",
            "20260701",
            max_workers=2,
            enckey_factory=enckey_factory,
            post=post,
        )
        self.assertEqual(set(result), {"000001", "000002"})
        self.assertEqual(calls[0], ("key", threading.current_thread().name))
        self.assertTrue(
            all(
                thread_name != threading.current_thread().name
                for kind, thread_name in calls if kind == "post"
            )
        )

    def test_refresh_batches_and_checkpoints_every_eighty_codes(self):
        codes = [f"{index:06d}" for index in range(1, 162)]
        current = {code: 10_000 for code in codes}
        batches = []

        def fetcher(batch, _start, _end, **_kwargs):
            batches.append(list(batch))
            return {
                code: {
                    "records": [{
                        "VARYDATE": "2026-01-01",
                        "F003N": 1,
                    }],
                    "error": None,
                }
                for code in batch
            }

        with tempfile.TemporaryDirectory() as root:
            cache_path = os.path.join(root, "history.json")
            cache = refresh_cninfo_share_cache(
                codes,
                current,
                "20260101",
                "20260701",
                cache_path=cache_path,
                fetcher=fetcher,
            )
            on_disk = json.loads(
                Path(cache_path).read_text(encoding="utf-8"))

        self.assertEqual([len(batch) for batch in batches], [80, 80, 1])
        self.assertEqual(len(cache["stocks"]), 161)
        self.assertEqual(on_disk["checkpoint_pending_codes"], 0)
        self.assertEqual(on_disk["checkpoint_completed_codes"], 161)

    def test_raw_checkpoint_import_and_forward_fill(self):
        raw = {
            "source": "resume-test",
            "query_start": "2025-01-01",
            "query_end": "2026-07-01",
            "stocks": {
                "002594": [
                    {
                        "VARYDATE": "2026-01-01",
                        "F003N": 300,
                        "F022N": 100,
                    },
                    {
                        "VARYDATE": "2026-06-15",
                        "F003N": 330,
                        "F022N": 110,
                    },
                ],
            },
        }
        with tempfile.TemporaryDirectory() as root:
            raw_path = Path(root, "raw.json")
            cache_path = Path(root, "normalized.json")
            raw_path.write_text(json.dumps(raw), encoding="utf-8")
            cache = import_raw_checkpoint(
                str(raw_path),
                {"002594": 3_300_000},
                current_circulating_shares={"002594": 1_100_000},
                cache_path=str(cache_path),
            )
            payload = build_point_in_time_share_payload(
                cache,
                ["20251231", "20260102", "20260614", "20260616"],
            )

        shares = payload["total_shares"]["002594"]
        self.assertNotIn("20251231", shares)
        self.assertEqual(shares["20260102"], 3_000_000)
        self.assertEqual(shares["20260614"], 3_000_000)
        self.assertEqual(shares["20260616"], 3_300_000)
        self.assertEqual(
            payload["circulating_disclaimer"],
            "circulating_share_proxy_not_csi_free_float",
        )

    def test_empty_records_have_short_ttl(self):
        calls = []

        def fetcher(batch, _start, _end, **_kwargs):
            calls.append(list(batch))
            return {
                code: {"records": [], "error": None}
                for code in batch
            }

        with tempfile.TemporaryDirectory() as root:
            cache_path = Path(root, "history.json")
            fresh = {
                "schema_version": 2,
                "stocks": {
                    "000001": {
                        "status": "no_records",
                        "events": [],
                        "fetched_at": datetime.now().isoformat(
                            timespec="seconds"),
                        "query_start": "20260101",
                        "query_end": "20260701",
                    },
                },
            }
            cache_path.write_text(json.dumps(fresh), encoding="utf-8")
            refresh_cninfo_share_cache(
                ["000001"],
                {"000001": 10000},
                "20260101",
                "20260701",
                cache_path=str(cache_path),
                fetcher=fetcher,
            )
            self.assertEqual(calls, [])

            fresh["stocks"]["000001"]["fetched_at"] = (
                datetime.now() - timedelta(hours=7)
            ).isoformat(timespec="seconds")
            cache_path.write_text(json.dumps(fresh), encoding="utf-8")
            refresh_cninfo_share_cache(
                ["000001"],
                {"000001": 10000},
                "20260101",
                "20260701",
                cache_path=str(cache_path),
                fetcher=fetcher,
            )

        self.assertEqual(calls, [["000001"]])


if __name__ == "__main__":
    unittest.main()
