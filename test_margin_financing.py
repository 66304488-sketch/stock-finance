import unittest

import pandas as pd

from margin_financing import _aggregate_scheme, _normalize_sse, _normalize_szse


class MarginFinancingTest(unittest.TestCase):
    def test_normalize_sse_keeps_active_stocks_and_excludes_funds(self):
        frame = pd.DataFrame([
            {
                "标的证券代码": "600000",
                "标的证券简称": "浦发银行",
                "融资余额": 1000,
                "融资买入额": 120,
                "融资偿还额": 80,
                "融券余量": 30,
                "融券卖出量": 5,
                "融券偿还量": 4,
            },
            {"标的证券代码": "510050", "标的证券简称": "50ETF"},
        ])

        rows = _normalize_sse(frame, "20260730", {"600000"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "600000")
        self.assertEqual(rows[0]["financing_balance"], 1000)
        self.assertEqual(rows[0]["short_balance_qty"], 30)
        self.assertIsNone(rows[0]["short_balance_exchange"])

    def test_normalize_szse_uses_exchange_short_balance(self):
        frame = pd.DataFrame([{
            "证券代码": "000001",
            "证券简称": "平安银行",
            "融资买入额": 200,
            "融资余额": 2000,
            "融券卖出量": 20,
            "融券余量": 50,
            "融券余额": 550,
            "融资融券余额": 2550,
        }])

        rows = _normalize_szse(frame, "20260730", {"000001"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["financing_buy"], 200)
        self.assertEqual(rows[0]["short_balance_exchange"], 550)

    def test_aggregate_scheme_calculates_changes_intensity_and_details(self):
        dates = ["20260729", "20260730"]
        by_date = {
            "20260729": [
                self._row("600000", "SSE", 1000, 100, 10, 1000),
                self._row("000001", "SZSE", 2000, 200, 20, 2000),
            ],
            "20260730": [
                self._row("600000", "SSE", 1100, 150, 11, 1500),
                self._row("000001", "SZSE", 1900, 250, 19, 2500),
            ],
        }

        result = _aggregate_scheme(
            by_date,
            dates,
            "sw",
            {"600000": "银行", "000001": "银行"},
        )

        industry = result["industries"][0]
        self.assertEqual(industry["industry"], "银行")
        self.assertEqual(industry["financing_balance"], 3000)
        self.assertEqual(industry["financing_change"], 0)
        self.assertEqual(industry["financing_buy"], 400)
        self.assertEqual(industry["buy_intensity"], 10.0)
        self.assertEqual(industry["short_balance"], 30)
        self.assertEqual(industry["stock_count"], 2)
        self.assertEqual(result["market"]["financing_balance"], 3000)
        self.assertEqual(len(result["details"]["银行"]), 2)
        self.assertEqual(result["details"]["银行"][0]["code"], "000001")
        self.assertEqual(result["details"]["银行"][0]["financing_change"], -100)

    @staticmethod
    def _row(code, market, financing_balance, financing_buy, short_balance, turnover):
        return {
            "code": code,
            "name": code,
            "market": market,
            "close": 10,
            "turnover": turnover,
            "financing_balance": financing_balance,
            "financing_buy": financing_buy,
            "financing_repay": None,
            "short_balance": short_balance,
            "short_balance_qty": 1,
            "short_value_method": "exchange",
            "total_balance": financing_balance + short_balance,
        }


if __name__ == "__main__":
    unittest.main()
