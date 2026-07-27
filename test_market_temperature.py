import unittest

import pandas as pd

from market_temperature import _percentile_of, _rolling_pct_rank


class MarketTemperatureTest(unittest.TestCase):
    def test_rolling_rank_does_not_change_when_future_values_are_appended(self):
        original = pd.Series(range(1, 31), dtype=float)
        extended = pd.concat([original, pd.Series([1000.0, -1000.0])], ignore_index=True)

        original_rank = _rolling_pct_rank(original)
        extended_rank = _rolling_pct_rank(extended)

        pd.testing.assert_series_equal(
            original_rank,
            extended_rank.iloc[: len(original)].reset_index(drop=True),
        )

    def test_rolling_rank_requires_enough_point_in_time_samples(self):
        ranks = _rolling_pct_rank(pd.Series(range(1, 25), dtype=float))

        self.assertTrue(ranks.iloc[:19].isna().all())
        self.assertAlmostEqual(ranks.iloc[19], 100.0)

    def test_intraday_percentile_uses_average_rank_for_ties(self):
        history = [1.0] * 10 + [2.0] * 10

        self.assertEqual(_percentile_of(history, 1.0), 27.5)
        self.assertEqual(_percentile_of(history, 2.0), 77.5)


if __name__ == "__main__":
    unittest.main()
