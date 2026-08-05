import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import opportunity_summary


AS_OF = "20260724"


def snapshot(*, mode="daily", risk_level="normal", risk_pattern="normal"):
    return {
        "schema_version": 1,
        "model_version": "heatmap-opportunity-v1",
        "scheme": "sw3",
        "period": "month",
        "mode": mode,
        "quality": {
            "status": "valid",
            "latest_date": AS_OF,
            "reasons": [],
            "warnings": [],
        },
        "market_permission": {
            "state": "attack",
            "label": "进攻",
            "message": "允许寻找确认机会",
            "highs": 200,
            "lows": 50,
            "net_breadth_pct": 3.0,
            "market_breadth": 0.25,
        },
        "industries": [
            {
                "industry": "行业甲",
                "stage": "confirmed",
                "stage_label": "确认",
                "score": 78.0,
                "breadth_percentile": 82.0,
                "adjusted_net_breadth_pct": 8.0,
                "acceleration_percentile": 75.0,
                "confirmations": {
                    "breadth": True,
                    "trend": True,
                    "activity": True,
                    "participation": True,
                },
                "risk_level": risk_level,
                "risk_pattern": risk_pattern,
                "risk_reasons": [],
                "risk_domains": [],
                "leaders": [
                    {
                        "code": "000001",
                        "name": "甲公司",
                        "amount": 100,
                        "return_pct": 3.0,
                    }
                ],
                "invalidation": "净扩散转负或相对价格跌破触发位",
            }
        ],
    }


def flow_payload(date=AS_OF):
    return {
        "as_of": date,
        "trade_date": date,
        "data_quality": {"coverage": {"ratio": 1.0}},
        "market": {"date": date},
        "industries": [
            {
                "industry": "行业甲",
                "price_result_pctile": 80,
                "excess_return_pct": 2.0,
                "activity_pctile": 85,
                "active_breadth_pctile": 75,
                "effective_participants": 8,
                "active_direction_breadth": 0.4,
                "leaders": [
                    {
                        "code": "000001",
                        "name": "甲公司",
                        "amount": 100,
                        "return_pct": 3.0,
                    }
                ],
                "top_stocks": [
                    {
                        "code": "000001",
                        "name": "甲公司",
                        "amount": 100,
                        "return_pct": 3.0,
                    },
                    {
                        "code": "000002",
                        "name": "乙公司",
                        "amount": 80,
                        "return_pct": 2.0,
                    },
                ],
            }
        ],
    }


def market_cap_payload(date=AS_OF):
    return {
        "trade_date": date,
        "market": {"date": date},
        "industries": [
            {
                "industry": "行业甲",
                "cap_weighted_return_1d_pct": 2.0,
                "equal_weight_return_1d_pct": 1.5,
                "stock_breadth_1d_pct": 70,
                "relative_1d_pct": 1.2,
                "top_stocks": [
                    {
                        "code": "000001",
                        "name": "甲公司",
                        "weight_pct": 30,
                        "change_pct": 3.0,
                    }
                ],
            }
        ],
    }


def crowding_payload(
    date=AS_OF,
    *,
    risk_state="normal",
    crowding_state="neutral",
    etf_change=2.5,
    etf_change_count=1,
):
    return {
        "as_of": date,
        "trade_date": date,
        "industries": [
            {
                "industry": "行业甲",
                "state": crowding_state,
                "risk_state": risk_state,
                "risk_reasons": [],
                "risk_domains": {},
                "etf_share_change_pct": etf_change,
                "external_evidence": {
                    "etf_share_change_pct": etf_change,
                    "etf_change_count": etf_change_count,
                    "margin_change_pct": None,
                    "margin_change_count": 0,
                },
                "top_stocks": [
                    {
                        "code": "000002",
                        "name": "乙公司",
                        "amount": 80,
                        "return_pct": 2.0,
                    }
                ],
            }
        ],
    }


def etf_payload(date=AS_OF):
    duplicate = {
        "industry": "行业甲",
        "code": "510001",
        "name": "行业甲ETF",
        "score": 72,
        "stage": "emerging",
        "liquid": True,
        "share_change_pct": 2.0,
        "etf": {
            "code": "510001",
            "name": "行业甲ETF",
            "last_date": date,
        },
    }
    return {
        "date": date,
        "etf_date": date,
        "top": [copy.deepcopy(duplicate)],
        "etfs": [
            copy.deepcopy(duplicate),
            {
                "industry": "行业甲",
                "code": "510002",
                "name": "未来ETF",
                "score": 90,
                "stage": "emerging",
                "liquid": True,
                "etf": {
                    "code": "510002",
                    "name": "未来ETF",
                    "last_date": "20260725",
                },
            },
        ],
        "industries": [
            {
                "industry": "行业甲",
                "score": 72,
                "stage": "emerging",
                "liquid": True,
                "etf": {
                    "code": "510001",
                    "name": "行业甲ETF",
                    "last_date": date,
                },
            }
        ],
    }


def momentum_payload(date=AS_OF):
    return {
        "date": date,
        "dynamic_pool": {
            "source_date": date,
            "entries": [
                {
                    "code": "510001",
                    "name": "行业甲ETF",
                    "last_date": date,
                    "passed_all": True,
                    "score": 1.2,
                }
            ],
        },
        "variants": {},
    }


class OpportunitySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.write("capital_flow_v2_sw3.json", flow_payload())
        self.write("market_cap_v2_sw3.json", market_cap_payload())
        self.write("crowding_sw3.json", crowding_payload())
        self.write(
            "market_temperature.json",
            {
                "rows": [
                    {
                        "date": AS_OF,
                        "temperature": 65,
                        "up": 3500,
                        "down": 1500,
                    },
                    {
                        "date": "20260725",
                        "temperature": 80,
                        "up": 4000,
                        "down": 1000,
                    },
                ]
            },
        )
        self.write("etf_recommend_sw3.json", etf_payload())
        self.write("momentum_etf.json", momentum_payload())

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        with open(self.data_dir / name, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)

    def build(self, source_snapshot=None, *, mode="daily", scheme="sw3"):
        source_snapshot = source_snapshot or snapshot(mode=mode)
        source_snapshot["scheme"] = scheme
        with patch.object(
            opportunity_summary,
            "load_opportunity_snapshot",
            return_value=source_snapshot,
        ):
            return opportunity_summary.build_opportunity_summary(
                self.data_dir,
                scheme=scheme,
                period="month",
                mode=mode,
            )

    def test_schema_and_source_dates_are_explicit(self):
        result = self.build()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["model_version"], "opportunity-summary-v1")
        self.assertEqual(result["as_of"], AS_OF)
        self.assertEqual(result["market"]["permission"], "allowed")
        self.assertEqual(
            result["methodology"]["independent_confirmation_domains"],
            ["price", "participation", "structure", "direct_demand"],
        )
        self.assertEqual(
            result["methodology"]["minimum_independent_confirmations"], 2
        )
        self.assertEqual(
            result["market"]["temperature"]["date"],
            AS_OF,
            "future temperature rows must not leak into an earlier decision",
        )
        for source in result["quality"]["sources"].values():
            self.assertIn("date", source)
            self.assertIn("status", source)
            self.assertIn("required", source)
        self.assertEqual(
            set(result["funnel"]),
            {
                "total",
                "triggered",
                "confirmed",
                "risk_passed",
                "with_qualified_etf",
                "actionable",
                "lanes",
            },
        )

    def test_core_date_misalignment_invalidates_action(self):
        self.write("capital_flow_v2_sw3.json", flow_payload("20260723"))
        source_snapshot = snapshot()
        source_snapshot["industries"][0]["leaders"] = [
            {"code": "000009", "name": "错位口径股票", "amount": 999}
        ]
        result = self.build(source_snapshot)
        self.assertEqual(result["quality"]["status"], "invalid")
        self.assertEqual(
            result["quality"]["sources"]["capital_flow_v2"]["status"], "stale"
        )
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["funnel"]["total"], 1)
        self.assertEqual(
            result["funnel"]["lanes"],
            {"confirmed": 0, "watch": 0, "rejected": 0},
        )

    def test_secondary_core_date_misalignment_is_degraded_not_silent(self):
        self.write("market_cap_v2_sw3.json", market_cap_payload("20260723"))
        result = self.build()
        self.assertEqual(result["quality"]["status"], "degraded")
        self.assertFalse(result["quality"]["can_act"])
        self.assertEqual(
            result["quality"]["sources"]["market_cap_v2"]["status"], "stale"
        )
        self.assertEqual(
            result["candidates"][0]["evidence"]["structure"]["status"],
            "missing",
        )

    def test_same_source_subsignals_count_as_one_domain(self):
        result = self.build()
        candidate = result["candidates"][0]
        self.assertEqual(candidate["confirmations"].count("participation"), 1)
        self.assertNotIn("activity", candidate["confirmations"])
        self.assertNotIn("active_breadth", candidate["confirmations"])
        self.assertEqual(
            candidate["confirmation_count"],
            len(set(candidate["independent_confirmations"])),
        )
        self.assertEqual(candidate["confirmation_total"], 4)
        self.assertEqual(candidate["independent_confirmation_total"], 4)
        self.assertIn("trigger", candidate["confirmations"])
        self.assertNotIn("trigger", candidate["independent_confirmations"])
        self.assertIn("trigger", candidate["confirmed_domains"])
        self.assertLessEqual(candidate["confirmation_count"], 4)

    def test_trigger_plus_only_one_follow_through_domain_stays_watch(self):
        source_snapshot = snapshot()
        source_snapshot["industries"][0]["confirmations"].update(
            {"activity": False, "participation": False}
        )
        flow = flow_payload()
        flow_row = flow["industries"][0]
        flow_row.update(
            {
                "activity_pctile": 40,
                "active_breadth_pctile": 40,
                "active_direction_breadth": 0.1,
            }
        )
        self.write("capital_flow_v2_sw3.json", flow)
        market_cap = market_cap_payload()
        market_cap["industries"][0].update(
            {
                "cap_weighted_return_1d_pct": -1,
                "equal_weight_return_1d_pct": -1,
                "stock_breadth_1d_pct": 30,
            }
        )
        self.write("market_cap_v2_sw3.json", market_cap)
        self.write(
            "crowding_sw3.json",
            crowding_payload(etf_change=0, etf_change_count=0),
        )
        result = self.build(source_snapshot)
        candidate = result["candidates"][0]
        self.assertTrue(candidate["triggered"])
        self.assertEqual(candidate["confirmations"], ["trigger", "price"])
        self.assertEqual(candidate["independent_confirmations"], ["price"])
        self.assertEqual(candidate["confirmation_count"], 1)
        self.assertEqual(
            candidate["confirmed_domains"], ["trigger", "price"]
        )
        self.assertEqual(candidate["lane"], "watch")
        self.assertFalse(candidate["actionable"])
        self.assertEqual(result["funnel"]["confirmed"], 0)

    def test_follow_through_domains_cannot_confirm_without_trigger(self):
        source_snapshot = snapshot()
        source_snapshot["industries"][0].update(
            {
                "breadth_percentile": 30,
                "adjusted_net_breadth_pct": -2,
            }
        )
        source_snapshot["industries"][0]["confirmations"]["breadth"] = False
        result = self.build(source_snapshot)
        candidate = result["candidates"][0]
        self.assertFalse(candidate["triggered"])
        self.assertGreaterEqual(candidate["confirmation_count"], 2)
        self.assertEqual(candidate["lane"], "watch")
        self.assertFalse(candidate["actionable"])

    def test_dormant_rows_count_in_universe_but_not_candidate_cards(self):
        source_snapshot = snapshot()
        dormant = copy.deepcopy(source_snapshot["industries"][0])
        dormant.update(
            {
                "industry": "行业乙",
                "stage": "dormant",
                "stage_label": "沉寂",
                "confirmations": {
                    "breadth": False,
                    "trend": False,
                    "activity": False,
                    "participation": False,
                },
                "breadth_percentile": 30,
                "adjusted_net_breadth_pct": -1,
            }
        )
        source_snapshot["industries"].append(dormant)
        result = self.build(source_snapshot)
        self.assertEqual(result["funnel"]["total"], 2)
        self.assertEqual(
            [candidate["industry"] for candidate in result["candidates"]],
            ["行业甲"],
        )

    def test_hard_risk_veto_rejects_otherwise_confirmed_candidate(self):
        source_snapshot = snapshot(
            risk_level="danger", risk_pattern="upside_exhaustion"
        )
        result = self.build(source_snapshot)
        candidate = result["candidates"][0]
        self.assertGreaterEqual(candidate["confirmation_count"], 2)
        self.assertTrue(candidate["vetoes"])
        self.assertEqual(candidate["lane"], "rejected")
        self.assertFalse(candidate["actionable"])

    def test_missing_direct_data_is_not_interpreted_as_zero(self):
        self.write(
            "crowding_sw3.json",
            crowding_payload(etf_change=0, etf_change_count=0),
        )
        result = self.build()
        evidence = result["candidates"][0]["evidence"]["direct_demand"]
        self.assertEqual(evidence["status"], "missing")
        self.assertIn("直接需求确认", result["candidates"][0]["missing"])
        self.assertFalse(
            any("净流出" in conflict for conflict in result["candidates"][0]["conflicts"])
        )

    def test_missing_optional_and_secondary_files_degrade_without_exception(self):
        (self.data_dir / "market_cap_v2_sw3.json").unlink()
        (self.data_dir / "crowding_sw3.json").unlink()
        (self.data_dir / "momentum_etf.json").unlink()
        result = self.build()
        self.assertEqual(result["quality"]["status"], "degraded")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["evidence"]["structure"]["status"], "missing")
        self.assertEqual(
            candidate["evidence"]["direct_demand"]["status"], "missing"
        )
        self.assertFalse(candidate["actionable"])

    def test_intraday_candidates_are_never_actionable(self):
        result = self.build(mode="intraday")
        self.assertTrue(result["candidates"])
        self.assertTrue(
            all(not candidate["actionable"] for candidate in result["candidates"])
        )
        self.assertEqual(
            result["candidates"][0]["evidence"]["price"]["status"], "missing"
        )

    def test_etf_carriers_are_point_in_time_and_deduplicated(self):
        result = self.build()
        etfs = result["candidates"][0]["carriers"]["etfs"]
        self.assertEqual([row["code"] for row in etfs], ["510001"])
        self.assertTrue(etfs[0]["qualified"])
        self.assertTrue(etfs[0]["momentum"]["passed_all"])
        self.assertTrue(result["candidates"][0]["actionable"])

    def test_industry_projection_cannot_upgrade_avoided_etf(self):
        payload = etf_payload()
        payload["top"] = []
        payload["etfs"][0]["stage"] = "avoid"
        payload["industries"][0]["stage"] = "emerging"
        payload["industries"][0]["liquid"] = True
        payload["etfs"][0]["related_industries"] = [
            {"industry": "行业乙"}
        ]
        self.write("etf_recommend_sw3.json", payload)
        result = self.build()
        carrier = result["candidates"][0]["carriers"]["etfs"][0]
        self.assertEqual(carrier["code"], "510001")
        self.assertFalse(carrier["qualified"])
        self.assertFalse(result["candidates"][0]["actionable"])
        related = opportunity_summary._etfs_by_industry(
            payload,
            as_of=AS_OF,
            source_date=AS_OF,
            source_usable=True,
            momentum={},
        )
        self.assertEqual([row["code"] for row in related["行业乙"]], ["510001"])

    def test_stale_etf_is_reference_only_not_actionable_carrier(self):
        payload = etf_payload("20260723")
        self.write("etf_recommend_sw3.json", payload)
        result = self.build()
        carrier = result["candidates"][0]["carriers"]["etfs"][0]
        self.assertEqual(
            result["quality"]["sources"]["etf_recommend_sw3"]["status"],
            "stale",
        )
        self.assertFalse(carrier["qualified"])
        self.assertFalse(result["candidates"][0]["actionable"])

    def test_future_nested_carrier_source_does_not_leak(self):
        etf = etf_payload()
        etf["etf_date"] = "20260725"
        self.write("etf_recommend_sw3.json", etf)
        momentum = momentum_payload()
        momentum["dynamic_pool"]["source_date"] = "20260725"
        momentum["dynamic_pool"]["entries"][0].pop("last_date")
        self.write("momentum_etf.json", momentum)
        result = self.build()
        sources = result["quality"]["sources"]
        self.assertEqual(sources["etf_recommend_sw3"]["status"], "future")
        self.assertEqual(sources["momentum_etf"]["status"], "future")
        self.assertEqual(result["candidates"][0]["carriers"]["etfs"], [])
        self.assertFalse(result["candidates"][0]["actionable"])

    def test_ths_uses_ths_etf_source_not_sw3(self):
        self.write("capital_flow_v2_ths.json", flow_payload())
        self.write("market_cap_v2_ths.json", market_cap_payload())
        self.write("crowding_ths.json", crowding_payload())
        self.write(
            "etf_recommend_ths.json",
            {
                "date": AS_OF,
                "etf_date": AS_OF,
                "top": [
                    {
                        "industry": "行业甲",
                        "code": "560001",
                        "name": "同花顺行业ETF",
                        "score": 75,
                        "stage": "emerging",
                        "liquid": True,
                        "etf": {
                            "code": "560001",
                            "name": "同花顺行业ETF",
                            "last_date": AS_OF,
                        },
                    }
                ],
                "etfs": [],
                "industries": [],
            },
        )
        result = self.build(scheme="ths")
        source = result["quality"]["sources"]["etf_recommend_ths"]
        self.assertEqual(source["file"], "etf_recommend_ths.json")
        self.assertEqual(source["scheme"], "ths")
        self.assertEqual(source["mapping"], "same_scheme")
        self.assertEqual(
            [row["code"] for row in result["candidates"][0]["carriers"]["etfs"]],
            ["560001"],
        )
        self.assertNotIn(
            "510001",
            {
                row["code"]
                for row in result["candidates"][0]["carriers"]["etfs"]
            },
        )

    def test_sw_without_same_scheme_etf_source_is_explicitly_missing(self):
        self.write("capital_flow_v2.json", flow_payload())
        self.write("market_cap_v2.json", market_cap_payload())
        self.write("crowding.json", crowding_payload())
        result = self.build(scheme="sw")
        source = result["quality"]["sources"]["etf_recommend_sw"]
        self.assertEqual(source["status"], "missing")
        self.assertIsNone(source["file"])
        self.assertEqual(source["scheme"], "sw")
        self.assertEqual(source["mapping"], "unavailable")
        self.assertIn("未借用其他分类", source["reason"])
        self.assertEqual(result["candidates"][0]["carriers"]["etfs"], [])
        self.assertFalse(result["candidates"][0]["actionable"])

    def test_intraday_prior_close_crowding_remains_a_risk_veto(self):
        self.write(
            "crowding_sw3.json",
            crowding_payload(
                "20260723",
                risk_state="unwind",
                crowding_state="crowded_decline",
            ),
        )
        result = self.build(mode="intraday")
        candidate = result["candidates"][0]
        self.assertEqual(
            result["quality"]["sources"]["crowding"]["status"], "prior_close"
        )
        self.assertIn("去拥挤中", candidate["vetoes"])
        self.assertEqual(candidate["lane"], "rejected")

    def test_intraday_period_selects_matching_history_window(self):
        source_snapshot = snapshot(mode="intraday")
        (self.data_dir / "intraday_highs_120d_sw3.json").touch()
        (self.data_dir / "intraday_lows_120d_sw3.json").touch()
        with patch.object(
            opportunity_summary,
            "load_opportunity_snapshot",
            return_value=source_snapshot,
        ) as loader:
            opportunity_summary.build_opportunity_summary(
                self.data_dir,
                scheme="sw3",
                period="120d",
                mode="intraday",
            )
        loader.assert_called_once_with(
            str(self.data_dir),
            scheme="sw3",
            period="120d",
            mode="intraday",
            window=120,
            stale=False,
        )
        with self.assertRaisesRegex(ValueError, "does not support alltime"):
            opportunity_summary.build_opportunity_summary(
                self.data_dir,
                scheme="sw3",
                period="alltime",
                mode="intraday",
            )

    def test_intraday_oldest_input_over_120_seconds_is_stale(self):
        high_path = self.data_dir / "intraday_highs_20d_sw3.json"
        low_path = self.data_dir / "intraday_lows_20d_sw3.json"
        high_path.touch()
        low_path.touch()
        old = time.time() - 180
        os.utime(high_path, (old, old))
        source_snapshot = snapshot(mode="intraday")
        with patch.object(
            opportunity_summary,
            "load_opportunity_snapshot",
            return_value=source_snapshot,
        ) as loader:
            result = opportunity_summary.build_opportunity_summary(
                self.data_dir,
                scheme="sw3",
                period="month",
                mode="intraday",
            )
        self.assertTrue(
            result["quality"]["sources"]["heatmap_opportunity"]["stale"]
        )
        self.assertTrue(loader.call_args.kwargs["stale"])
        self.assertEqual(loader.call_args.kwargs["window"], 20)

    def test_stock_carriers_merge_leaders_and_top_stocks_by_code(self):
        result = self.build()
        stocks = result["candidates"][0]["carriers"]["stocks"]
        self.assertEqual({row["code"] for row in stocks}, {"000001", "000002"})
        first = next(row for row in stocks if row["code"] == "000001")
        self.assertIn("heatmap_leader", first["sources"])
        self.assertIn("flow_top", first["sources"])

    def test_missing_heatmap_returns_invalid_empty_summary(self):
        with patch.object(
            opportunity_summary,
            "load_opportunity_snapshot",
            side_effect=FileNotFoundError,
        ):
            result = opportunity_summary.build_opportunity_summary(self.data_dir)
        self.assertEqual(result["quality"]["status"], "invalid")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["funnel"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
