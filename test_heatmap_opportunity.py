import unittest

import heatmap_opportunity


def heat_payload(counts_by_industry, totals, dates, *, intraday=False):
    rows = []
    for industry, counts in counts_by_industry.items():
        row = {
            "industry": industry,
            "total": totals[industry],
            "daily_counts": counts,
            "ratio": round(counts[0] / totals[industry] * 100, 1),
        }
        if intraday:
            row.update({
                "touched_count": counts[0],
                "standing_count": counts[0],
                "retained_count": counts[0],
                "daily_details": {},
            })
        rows.append(row)
    total_counts = [sum(values[index] for values in counts_by_industry.values()) for index in range(len(dates))]
    total = {
        "industry": "全市场合计",
        "total": sum(totals.values()),
        "daily_counts": total_counts,
        "is_total": True,
    }
    if intraday:
        total.update({
            "touched_count": total_counts[0],
            "standing_count": total_counts[0],
            "retained_count": total_counts[0],
        })
    rows.append(total)
    payload = {
        "dates": [
            {"label": value[4:6] + "月" + value[6:] + "日", "full_label": value}
            for value in dates
        ],
        "industries": rows,
    }
    if intraday:
        payload.update({
            "trade_date": dates[0],
            "coverage": {"active": 100, "spot": 98, "market_cap": 95},
        })
    return payload


def flow_payload(date="20260724", *, risk_level="normal", risk_pattern="normal"):
    def row(industry, result, activity, active_breadth, extension=50, concentration=50):
        series = []
        for index, source_date in enumerate(
            ["20260720", "20260721", "20260722", "20260723", "20260724"]
        ):
            series.append({
                "date": source_date,
                "excess_return_pct": 0.2 + index / 10,
                "activity_pctile": activity,
                "risk_level": risk_level,
                "risk_pattern": risk_pattern,
            })
        return {
            "industry": industry,
            "price_result_pctile": result,
            "activity_pctile": activity,
            "active_breadth_pctile": active_breadth,
            "amount": 123456789,
            "excess_return_pct": 1.2,
            "effective_participants": 8,
            "persistence": 3,
            "risk_level": risk_level,
            "risk_pattern": risk_pattern,
            "risk_pattern_label": "上涨衰竭" if risk_pattern == "upside_exhaustion" else "正常",
            "price_extension_pctile": extension,
            "internal_top5_pctile": concentration,
            "crowding_score": 50,
            "series": series,
        }

    return {
        "as_of": date,
        "market": {"breadth": 0.2, "price_change_pct": 1.0},
        "data_quality": {"coverage": {"ratio": 0.99}},
        "industries": [
            row("行业甲", 80, 75, 70),
            row("行业乙", 75, 70, 65),
        ],
    }


class HeatmapOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.dates = ["20260724", "20260723", "20260722", "20260721", "20260720"]
        totals = {"行业甲": 40, "行业乙": 60}
        self.highs = heat_payload(
            {"行业甲": [12, 8, 4, 2, 1], "行业乙": [2, 2, 2, 2, 2]},
            totals,
            self.dates,
        )
        self.lows = heat_payload(
            {"行业甲": [1, 1, 2, 2, 2], "行业乙": [3, 3, 3, 3, 3]},
            totals,
            self.dates,
        )

    def test_valid_daily_snapshot_uses_state_not_raw_count_only(self):
        result = heatmap_opportunity.build_opportunity_snapshot(
            self.highs,
            self.lows,
            flow_payload(),
            scheme="sw",
            period="month",
            mode="daily",
        )
        self.assertEqual(result["quality"]["status"], "valid")
        candidate = next(row for row in result["industries"] if row["industry"] == "行业甲")
        self.assertGreater(candidate["breadth_percentile"], 60)
        self.assertEqual(candidate["turnover_amount"], 123456789.0)
        self.assertIn(candidate["stage"], {"emerging", "confirmed", "extending"})
        self.assertFalse(result["calibration"]["probability_available"])
        self.assertEqual(result["calibration"]["label"], "校准中")

    def test_chinese_date_labels_are_normalized_for_alignment(self):
        self.highs["dates"][0]["full_label"] = "2026年7月24日"
        self.lows["dates"][0]["full_label"] = "2026年7月24日"
        quality = heatmap_opportunity.validate_inputs(
            self.highs,
            self.lows,
            flow_payload(),
            mode="daily",
        )
        self.assertEqual(quality["latest_date"], "20260724")
        self.assertTrue(quality["dates_aligned"])
        self.assertTrue(quality["flow_aligned"])

    def test_dangerous_extension_becomes_crowded_not_opportunity(self):
        flow = flow_payload(risk_level="danger", risk_pattern="upside_exhaustion")
        for row in flow["industries"]:
            row["price_extension_pctile"] = 98
            row["internal_top5_pctile"] = 96
        result = heatmap_opportunity.build_opportunity_snapshot(
            self.highs,
            self.lows,
            flow,
            scheme="sw",
            period="month",
            mode="daily",
        )
        candidate = next(row for row in result["industries"] if row["industry"] == "行业甲")
        self.assertEqual(candidate["stage"], "crowded")
        self.assertFalse(candidate["actionable"])
        self.assertIn("价格延伸", candidate["risk_domains"])

    def test_missing_market_dates_pause_scoring(self):
        broken_highs = heat_payload(
            {"行业甲": [12, 0, 0, 2, 1], "行业乙": [2, 0, 0, 2, 2]},
            {"行业甲": 40, "行业乙": 60},
            self.dates,
        )
        broken_lows = heat_payload(
            {"行业甲": [1, 0, 0, 2, 2], "行业乙": [3, 0, 0, 3, 3]},
            {"行业甲": 40, "行业乙": 60},
            self.dates,
        )
        result = heatmap_opportunity.build_opportunity_snapshot(
            broken_highs,
            broken_lows,
            flow_payload(),
            scheme="sw",
            period="month",
            mode="daily",
        )
        self.assertEqual(result["quality"]["status"], "invalid")
        self.assertFalse(result["quality"]["can_score"])
        self.assertEqual(result["market_permission"]["state"], "paused")
        self.assertTrue(all(row["stage"] == "insufficient" for row in result["industries"]))

    def test_cross_scheme_total_mismatch_is_a_hard_gate(self):
        quality = heatmap_opportunity.validate_inputs(
            self.highs,
            self.lows,
            flow_payload(),
            mode="daily",
            peer_totals=[
                {"scheme": "sw", "date": "20260724", "highs": 14, "lows": 4},
                {"scheme": "ths", "date": "20260724", "highs": 15, "lows": 4},
            ],
        )
        self.assertEqual(quality["status"], "invalid")
        self.assertFalse(quality["peer_totals_consistent"])

    def test_intraday_keeps_prior_close_flow_out_of_confirmation(self):
        highs = heat_payload(
            {"行业甲": [5], "行业乙": [1]},
            {"行业甲": 40, "行业乙": 60},
            ["20260724"],
            intraday=True,
        )
        lows = heat_payload(
            {"行业甲": [1], "行业乙": [2]},
            {"行业甲": 40, "行业乙": 60},
            ["20260724"],
            intraday=True,
        )
        result = heatmap_opportunity.build_opportunity_snapshot(
            highs,
            lows,
            flow_payload("20260723"),
            scheme="sw",
            period="month",
            mode="intraday",
        )
        candidate = next(row for row in result["industries"] if row["industry"] == "行业甲")
        self.assertNotEqual(result["quality"]["status"], "invalid")
        self.assertFalse(candidate["confirmations"]["activity"])
        self.assertFalse(candidate["actionable"])
        self.assertIn("同时段成交活跃确认", candidate["missing_confirmations"])
        self.assertEqual(candidate["high_count"], candidate["high_retained"])
        self.assertEqual(candidate["low_count"], candidate["low_retained"])

    def test_intraday_explicit_zero_retention_is_not_replaced_by_standing_count(self):
        highs = heat_payload(
            {"行业甲": [5], "行业乙": [1]},
            {"行业甲": 40, "行业乙": 60},
            ["20260724"],
            intraday=True,
        )
        lows = heat_payload(
            {"行业甲": [1], "行业乙": [2]},
            {"行业甲": 40, "行业乙": 60},
            ["20260724"],
            intraday=True,
        )
        for payload in (highs, lows):
            for row in payload["industries"]:
                row["retained_count"] = 0
                row["standing_count"] = 9
        result = heatmap_opportunity.build_opportunity_snapshot(
            highs,
            lows,
            flow_payload("20260723"),
            scheme="sw",
            period="month",
            mode="intraday",
        )
        self.assertEqual(result["market_permission"]["highs"], 0)
        self.assertEqual(result["market_permission"]["lows"], 0)
        self.assertEqual(result["market_permission"]["net_breadth_pct"], 0)
        self.assertTrue(all(row["high_count"] == 0 for row in result["industries"]))


if __name__ == "__main__":
    unittest.main()
