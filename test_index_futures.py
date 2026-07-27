import math
import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import index_futures as futures


TZ = futures.SHANGHAI_TZ


def quote_line(
    contract,
    *,
    last=4010.0,
    bid=4009.0,
    ask=4011.0,
    prev_settle=4000.0,
    volume=100,
    oi=1000,
    previous_oi=950,
    quote_date="2026-07-17",
    quote_time="14:00:00",
):
    fields = [""] * 50
    fields[0] = "4005.0"
    fields[1] = "4020.0"
    fields[2] = "3990.0"
    fields[3] = str(last)
    fields[4] = str(volume)
    fields[5] = "1000000"
    fields[6] = str(oi)
    fields[13] = str(prev_settle + 1)
    fields[14] = str(prev_settle)
    fields[15] = str(previous_oi)
    fields[16] = str(bid)
    fields[17] = "3"
    fields[26] = str(ask)
    fields[27] = "4"
    fields[36] = quote_date
    fields[37] = quote_time
    fields[48] = str(last)
    fields[49] = f"{contract[:2]}指数期货{contract[2:]}"
    return f'var hq_str_nf_{contract}="' + ",".join(fields) + '";'


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MappingAndCalendarTests(unittest.TestCase):
    def test_product_index_mapping_and_multipliers(self):
        self.assertEqual(futures.INDEX_FUTURES["IF"]["index_code"], "000300")
        self.assertEqual(futures.INDEX_FUTURES["IH"]["index_code"], "000016")
        self.assertEqual(futures.INDEX_FUTURES["IC"]["index_code"], "000905")
        self.assertEqual(futures.INDEX_FUTURES["IM"]["index_code"], "000852")
        self.assertEqual(futures.INDEX_FUTURES["IF"]["multiplier"], 300)
        self.assertEqual(futures.INDEX_FUTURES["IM"]["multiplier"], 200)

    def test_generate_nearby_and_two_quarter_months(self):
        now = datetime(2026, 1, 5, 10, 0, tzinfo=TZ)
        self.assertEqual(
            futures.generate_contracts("IF", now),
            ["IF2601", "IF2602", "IF2603", "IF2606"],
        )

    def test_rolls_after_expiry_close_and_across_year(self):
        before_close = datetime(2026, 7, 17, 14, 59, tzinfo=TZ)
        after_close = datetime(2026, 7, 17, 15, 1, tzinfo=TZ)
        self.assertEqual(
            futures.generate_contracts("IM", before_close),
            ["IM2607", "IM2608", "IM2609", "IM2612"],
        )
        self.assertEqual(
            futures.generate_contracts("IM", after_close),
            ["IM2608", "IM2609", "IM2612", "IM2703"],
        )
        december = datetime(2026, 12, 1, 10, 0, tzinfo=TZ)
        self.assertEqual(
            futures.generate_contracts("IH", december),
            ["IH2612", "IH2701", "IH2703", "IH2706"],
        )

    def test_expiry_override_controls_roll(self):
        now = datetime(2026, 7, 17, 15, 1, tzinfo=TZ)
        override = {"IF2607": date(2026, 7, 20)}
        self.assertEqual(
            futures.generate_contracts("IF", now, expiry_overrides=override)[0],
            "IF2607",
        )


class SinaParserTests(unittest.TestCase):
    def setUp(self):
        futures.reset_in_memory_state()

    def test_parses_actual_nf_contract_fields(self):
        now = datetime(2026, 7, 17, 14, 0, 20, tzinfo=TZ)
        parsed = futures.parse_sina_futures_payload(
            quote_line(
                "IF2607",
                last=4010,
                bid=4008,
                ask=4012,
                prev_settle=4000,
                volume=12345,
                oi=1100,
                previous_oi=1000,
            ),
            now=now,
        )["IF2607"]
        self.assertEqual(parsed["last"], 4010)
        self.assertEqual(parsed["bid"], 4008)
        self.assertEqual(parsed["ask"], 4012)
        self.assertEqual(parsed["mark"], 4010)
        self.assertEqual(parsed["prev_settle"], 4000)
        self.assertEqual(parsed["change_pct"], 0.25)
        self.assertEqual(parsed["volume"], 12345)
        self.assertEqual(parsed["OI"], 1100)
        self.assertEqual(parsed["OI_change"], 100)
        self.assertEqual(parsed["quote_time"], "2026-07-17T14:00:00+08:00")
        self.assertEqual(parsed["freshness"]["status"], "fresh")
        self.assertFalse(parsed["stale"])

    def test_mark_falls_back_to_last_for_invalid_book(self):
        parsed = futures.parse_sina_futures_payload(
            quote_line("IC2607", last=6200, bid=0, ask=0),
            now=datetime(2026, 7, 17, 14, 0, tzinfo=TZ),
        )["IC2607"]
        self.assertIsNone(parsed["bid"])
        self.assertIsNone(parsed["ask"])
        self.assertEqual(parsed["mark"], 6200)

    def test_filters_unrequested_and_malformed_contracts(self):
        payload = "\n".join(
            [
                quote_line("IF2607"),
                quote_line("IH2607"),
                'var hq_str_nf_IF0="1,2,3";',
            ]
        )
        parsed = futures.parse_sina_futures_payload(
            payload,
            requested_contracts=["IF2607"],
            now=datetime(2026, 7, 17, 14, 0, tzinfo=TZ),
        )
        self.assertEqual(set(parsed), {"IF2607"})

    def test_selects_main_by_volume_then_oi(self):
        quotes = {
            "IF2607": {"contract": "IF2607", "volume": 100, "OI": 9999},
            "IF2608": {"contract": "IF2608", "volume": 300, "OI": 100},
            "IF0": {"contract": "IF0", "volume": 999999, "OI": 999999},
        }
        self.assertEqual(
            futures.select_main_contract(quotes)["contract"],
            "IF2608",
        )


class FetchFallbackTests(unittest.TestCase):
    def setUp(self):
        futures.reset_in_memory_state()

    def test_one_batch_request_and_last_good_is_forced_stale_on_failure(self):
        now = datetime(2026, 7, 17, 14, 0, tzinfo=TZ)
        requester = mock.Mock()
        requester.get.return_value = FakeResponse(
            "\n".join(
                [
                    quote_line("IF2607"),
                    quote_line("IF2608", volume=300),
                ]
            )
        )
        first = futures.fetch_futures_quotes(
            ["IF2607", "IF2608"],
            now=now,
            requester=requester,
        )
        self.assertEqual(requester.get.call_count, 1)
        self.assertEqual(first["source"], "sina_nf")
        self.assertFalse(first["stale"])
        requested_url = requester.get.call_args.args[0]
        self.assertIn("nf_IF2607,nf_IF2608", requested_url)

        requester.get.side_effect = OSError("offline")
        second = futures.fetch_futures_quotes(
            ["IF2607", "IF2608"],
            now=now + timedelta(minutes=1),
            requester=requester,
        )
        self.assertEqual(second["source"], "last_good")
        self.assertTrue(second["stale"])
        self.assertEqual(second["quotes"]["IF2607"]["source"], "last_good")
        self.assertTrue(second["quotes"]["IF2607"]["freshness"]["stale"])
        self.assertIn("last-good", second["quotes"]["IF2607"]["freshness"]["reason"])

    def test_partial_response_uses_cached_quote_only_for_missing_contract(self):
        now = datetime(2026, 7, 17, 14, 0, tzinfo=TZ)
        requester = mock.Mock()
        requester.get.return_value = FakeResponse(
            "\n".join([quote_line("IF2607"), quote_line("IF2608")])
        )
        futures.fetch_futures_quotes(
            ["IF2607", "IF2608"], now=now, requester=requester
        )
        requester.get.return_value = FakeResponse(quote_line("IF2608", last=4020))
        result = futures.fetch_futures_quotes(
            ["IF2607", "IF2608"],
            now=now + timedelta(seconds=10),
            requester=requester,
        )
        self.assertEqual(result["source"], "mixed")
        self.assertEqual(result["quotes"]["IF2607"]["source"], "last_good")
        self.assertEqual(result["quotes"]["IF2608"]["source"], "sina_nf")

    def test_failure_without_cache_is_unavailable(self):
        requester = mock.Mock()
        requester.get.side_effect = OSError("offline")
        result = futures.fetch_futures_quotes(
            ["IF2607"],
            now=datetime(2026, 7, 17, 14, 0, tzinfo=TZ),
            requester=requester,
        )
        self.assertEqual(result["source"], "unavailable")
        self.assertTrue(result["stale"])
        self.assertEqual(result["missing"], ["IF2607"])


class BasisAndHistoryTests(unittest.TestCase):
    def setUp(self):
        futures.reset_in_memory_state()

    def _quote(self, contract="IF2608", mark=4020, at=None):
        at = at or datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        return {
            "contract": contract,
            "product": contract[:2],
            "mark": mark,
            "last": mark,
            "quote_time": at.isoformat(timespec="seconds"),
            "freshness": {
                "status": "fresh",
                "stale": False,
                "age_seconds": 0,
                "source": "sina_nf",
                "reason": None,
            },
            "stale": False,
            "source": "sina_nf",
            "volume": 100,
            "OI": 1000,
        }

    def test_basis_and_annualized_basis_but_no_direction_signal(self):
        now = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        with mock.patch.dict(os.environ, {}, clear=True):
            enriched = futures.enrich_futures_quote(
                self._quote(at=now),
                {"last": 4000, "quote_time": now.isoformat()},
                now=now,
            )
        self.assertEqual(enriched["basis"], 20)
        self.assertEqual(enriched["basis_pct"], 0.5)
        self.assertIsNotNone(enriched["annualized_basis"])
        self.assertIsNone(enriched["basis_direction_signal"])
        self.assertIn("不可直接解释", enriched["basis_interpretation"])
        self.assertIsNone(enriched["fair_value"])
        self.assertIsNone(enriched["fair_basis_residual"])
        self.assertEqual(enriched["fair_value_status"], "unavailable")
        self.assertIn("未同时配置", enriched["fair_value_reason"])

    def test_fair_value_only_when_both_rates_are_configured(self):
        now = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        env = {
            "INDEX_FUTURES_IF_FUNDING_RATE": "2%",
            "INDEX_FUTURES_IF_DIVIDEND_YIELD": "0.01",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            enriched = futures.enrich_futures_quote(
                self._quote(at=now),
                {"price": 4000, "quote_time": now.isoformat()},
                now=now,
            )
        years = enriched["days_to_expiry"] / 365
        expected = 4000 * math.exp((0.02 - 0.01) * years)
        self.assertEqual(enriched["fair_value_status"], "available")
        self.assertAlmostEqual(enriched["fair_value"], expected, places=4)
        self.assertAlmostEqual(
            enriched["fair_basis_residual"],
            4020 - expected,
            places=4,
        )
        self.assertIsNone(enriched["fair_value_reason"])

    def test_in_memory_one_five_and_fifteen_minute_changes(self):
        start = datetime(2026, 7, 20, 9, 30, tzinfo=TZ)
        futures.record_futures_sample(
            "IF2608", mark=100, basis=5, at=start
        )
        futures.record_futures_sample(
            "IF2608", mark=102, basis=7, at=start + timedelta(minutes=10)
        )
        futures.record_futures_sample(
            "IF2608", mark=103, basis=8, at=start + timedelta(minutes=14)
        )
        futures.record_futures_sample(
            "IF2608", mark=104, basis=9, at=start + timedelta(minutes=15)
        )
        changes = futures.calculate_intraday_changes(
            "IF2608", at=start + timedelta(minutes=15)
        )
        self.assertAlmostEqual(changes["1m"]["futures_return_pct"], 104 / 103 * 100 - 100, places=4)
        self.assertEqual(changes["1m"]["basis_change"], 1)
        self.assertAlmostEqual(changes["5m"]["futures_return_pct"], 104 / 102 * 100 - 100, places=4)
        self.assertEqual(changes["5m"]["basis_change"], 2)
        self.assertEqual(changes["15m"]["futures_return_pct"], 4)
        self.assertEqual(changes["15m"]["basis_change"], 4)

    def test_history_does_not_cross_lunch_break(self):
        morning = datetime(2026, 7, 20, 11, 30, tzinfo=TZ)
        afternoon = datetime(2026, 7, 20, 13, 1, tzinfo=TZ)
        futures.record_futures_sample("IF2608", mark=100, basis=5, at=morning)
        futures.record_futures_sample("IF2608", mark=101, basis=6, at=afternoon)
        changes = futures.calculate_intraday_changes("IF2608", at=afternoon)
        self.assertEqual(changes["1m"]["status"], "insufficient_history")


class SettlementMonitorTests(unittest.TestCase):
    def setUp(self):
        futures.reset_in_memory_state()

    def test_expiry_afternoon_arithmetic_mean_and_deviation(self):
        quote = {
            "contract": "IF2607",
            "product": "IF",
            "mark": 4020.0,
        }
        first_time = datetime(2026, 7, 17, 13, 0, tzinfo=TZ)
        second_time = datetime(2026, 7, 17, 13, 1, tzinfo=TZ)
        first = futures.build_settlement_monitor(
            "IF",
            [quote],
            {"last": 4000, "quote_time": first_time.isoformat()},
            now=first_time,
        )
        self.assertEqual(first["samples"], 1)
        second = futures.build_settlement_monitor(
            "IF",
            [quote],
            {"last": 4010, "quote_time": second_time.isoformat()},
            now=second_time,
        )
        self.assertTrue(second["active"])
        self.assertEqual(second["phase"], "collecting")
        self.assertEqual(second["samples"], 2)
        self.assertEqual(second["simulated_settlement"], 4005)
        self.assertEqual(second["futures_deviation"], 15)

    def test_non_expiry_day_is_inactive(self):
        now = datetime(2026, 7, 16, 13, 1, tzinfo=TZ)
        result = futures.build_settlement_monitor(
            "IF",
            [{"contract": "IF2607", "product": "IF", "mark": 4020}],
            {"last": 4000, "quote_time": now.isoformat()},
            now=now,
        )
        self.assertFalse(result["active"])
        self.assertEqual(result["phase"], "inactive")
        self.assertIsNone(result["simulated_settlement"])


class FourProductOverviewTests(unittest.TestCase):
    def setUp(self):
        futures.reset_in_memory_state()

    def test_four_products_use_one_batch_and_choose_volume_main(self):
        now = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        lines = []
        for product in ("IF", "IH", "IC", "IM"):
            contracts = futures.generate_contracts(product, now)
            for index, contract in enumerate(contracts):
                lines.append(
                    quote_line(
                        contract,
                        last=4000 + index,
                        bid=3999 + index,
                        ask=4001 + index,
                        volume=100 + index * 100,
                        quote_date="2026-07-20",
                        quote_time="10:00:00",
                    )
                )
        requester = mock.Mock()
        requester.get.return_value = FakeResponse("\n".join(lines))
        spot_quotes = {
            "000300": {"last": 3990, "quote_time": now.isoformat()},
            "000016": {"last": 3990, "quote_time": now.isoformat()},
            "000905": {"last": 3990, "quote_time": now.isoformat()},
            "000852": {"last": 3990, "quote_time": now.isoformat()},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            overview = futures.get_index_futures_overview(
                spot_quotes,
                now=now,
                requester=requester,
            )
        self.assertEqual(requester.get.call_count, 1)
        self.assertEqual(set(overview["products"]), {"IF", "IH", "IC", "IM"})
        for product, item in overview["products"].items():
            self.assertEqual(item["main_contract"], item["candidate_contracts"][-1])
            self.assertEqual(item["quote"]["volume"], 400)
            self.assertIsNone(item["quote"]["basis_direction_signal"])
            self.assertIsNone(item["quote"]["fair_value"])
            self.assertEqual(len(item["contracts"]), 4)
        self.assertIn("不产生方向信号", overview["methodology"]["basis"])

    def test_missing_spot_keeps_basis_null(self):
        now = datetime(2026, 7, 20, 10, 0, tzinfo=TZ)
        contracts = futures.generate_contracts("IF", now)
        quotes = {
            contract: futures.parse_sina_futures_payload(
                quote_line(
                    contract,
                    quote_date="2026-07-20",
                    quote_time="10:00:00",
                ),
                now=now,
            )[contract]
            for contract in contracts
        }
        item = futures.get_product_overview(
            "IF",
            None,
            now=now,
            quotes=quotes,
        )
        self.assertIsNone(item["quote"]["basis"])
        self.assertIsNone(item["quote"]["annualized_basis"])
        self.assertTrue(
            any("缺少现货指数点位" in warning for warning in item["warnings"])
        )


if __name__ == "__main__":
    unittest.main()
