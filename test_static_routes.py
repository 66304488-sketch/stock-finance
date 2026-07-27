import os
import sys
import tempfile
import unittest
from pathlib import Path


class StaticRouteTest(unittest.TestCase):
    def test_resources_and_runtime_data_use_separate_roots(self):
        with tempfile.TemporaryDirectory() as root:
            resource_dir = Path(root, "resources")
            data_dir = Path(root, "data")
            user_dir = Path(root, "user")
            resource_dir.mkdir()
            data_dir.mkdir()
            user_dir.mkdir()
            (resource_dir / "app.html").write_text("resource page", encoding="utf-8")
            (resource_dir / "new_highs_data_month.json").write_text('{"source":"seed"}', encoding="utf-8")
            (data_dir / "new_highs_data_month.json").write_text('{"source":"user"}', encoding="utf-8")
            (data_dir / "crowding.json").write_text(
                '{"scheme":"sw"}', encoding="utf-8")
            (data_dir / "crowding_ths.json").write_text(
                '{"scheme":"ths"}', encoding="utf-8")
            (data_dir / "crowding_sw3.json").write_text(
                '{"scheme":"sw3"}', encoding="utf-8")
            (data_dir / "capital_flow.json").write_text(
                '{"model_version":"legacy"}', encoding="utf-8")
            (data_dir / "capital_flow_v2.json").write_text(
                '{"model_version":"turnover-momentum-v2","scheme":"sw"}',
                encoding="utf-8",
            )
            (data_dir / "capital_flow_ths.json").write_text(
                '{"model_version":"legacy","scheme":"ths"}', encoding="utf-8")
            (data_dir / "capital_flow_v2_sw3.json").write_text(
                '{"model_version":"turnover-momentum-v2","scheme":"sw3"}',
                encoding="utf-8",
            )
            (data_dir / "market_cap.json").write_text(
                '{"model_version":"legacy","scheme":"sw"}', encoding="utf-8")
            (data_dir / "market_cap_v2.json").write_text(
                '{"model_version":"market-cap-structure-v2","scheme":"sw"}',
                encoding="utf-8",
            )
            (data_dir / "market_cap_ths.json").write_text(
                '{"model_version":"legacy","scheme":"ths"}', encoding="utf-8")
            (data_dir / "market_cap_v2_sw3.json").write_text(
                '{"model_version":"market-cap-structure-v2","scheme":"sw3"}',
                encoding="utf-8",
            )
            (data_dir / "private.txt").write_text("secret", encoding="utf-8")

            os.environ.update({
                "STOCK_FINANCE_RESOURCE_DIR": str(resource_dir),
                "STOCK_FINANCE_DATA_DIR": str(data_dir),
                "STOCK_FINANCE_USER_DATA_DIR": str(user_dir),
            })

            sys.modules.pop("server", None)
            sys.modules.pop("runtime_paths", None)
            from fastapi.testclient import TestClient
            from server import app

            with TestClient(app) as client:
                self.assertEqual(client.get("/app.html").text, "resource page")
                self.assertEqual(client.get("/new_highs_data_month.json").json()["source"], "user")
                for scheme in ("sw", "ths", "sw3"):
                    response = client.get(
                        f"/api/crowding?scheme={scheme}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["scheme"], scheme)
                self.assertEqual(
                    client.get("/api/crowding?scheme=bad").status_code, 400)
                # V2 is preferred when present; a scheme without V2 keeps the
                # legacy response until its next full momentum rebuild.
                self.assertEqual(
                    client.get("/api/capital-flow?scheme=sw").json()[
                        "model_version"],
                    "turnover-momentum-v2",
                )
                self.assertEqual(
                    client.get("/api/capital-flow?scheme=ths").json()[
                        "model_version"],
                    "legacy",
                )
                self.assertEqual(
                    client.get("/api/capital-flow?scheme=sw3").json()[
                        "model_version"],
                    "turnover-momentum-v2",
                )
                self.assertEqual(
                    client.get("/api/market-cap?scheme=sw").json()[
                        "model_version"],
                    "market-cap-structure-v2",
                )
                self.assertEqual(
                    client.get("/api/market-cap?scheme=ths").json()[
                        "model_version"],
                    "legacy",
                )
                self.assertEqual(
                    client.get("/api/market-cap?scheme=sw3").json()[
                        "model_version"],
                    "market-cap-structure-v2",
                )
                self.assertEqual(client.get("/private.txt").status_code, 404)
                info = client.get("/api/runtime-info").json()
                self.assertEqual(info["resource_static_dir"], str(resource_dir.resolve()))
                self.assertEqual(info["data_dir"], str(data_dir.resolve()))
                blocked = client.post(
                    "/api/settings", json={"ai_provider":"anthropic"},
                    headers={"Origin":"https://malicious.example"},
                )
                self.assertEqual(blocked.status_code, 403)
                allowed = client.post("/api/settings", json={"ai_provider":"anthropic"})
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual((user_dir / "config.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
