import unittest
from unittest.mock import patch

from watchlist_service import (
    DEFAULT_WATCHLIST,
    load_watchlist,
    normalize_watchlist,
    resolve_watchlist,
)


class WatchlistServiceTests(unittest.TestCase):
    def test_default_list_matches_former_scanner_list(self):
        self.assertEqual(
            DEFAULT_WATCHLIST,
            [
                "QQQ", "IWM", "SPY", "NVDA", "AMD", "SMCI", "AVGO",
                "MU", "ARM", "INTC", "PLTR", "SOFI", "RIVN", "SOUN",
                "AAPL", "MSFT", "AMZN", "META", "GOOGL", "NFLX",
            ],
        )

    def test_every_call_returns_a_separate_list(self):
        first = resolve_watchlist()
        second = resolve_watchlist()
        first.pop()
        self.assertNotEqual(first, second)
        self.assertEqual(second, DEFAULT_WATCHLIST)

    def test_environment_values_are_normalized_and_deduplicated(self):
        with patch(
            "watchlist_service.dotenv_values",
            return_value={"CTS_WATCHLIST": " $aapl, MS.FT, rivn-1, AAPL "},
        ):
            self.assertEqual(
                load_watchlist(),
                ["AAPL", "MS.FT", "RIVN-1"],
            )

    def test_normalization_accepts_standard_symbols(self):
        self.assertEqual(
            normalize_watchlist([" $nvda ", " ms.ft ", "rivn-1"]),
            ["NVDA", "MS.FT", "RIVN-1"],
        )

    def test_missing_blank_and_invalid_configuration_fall_back(self):
        for configured in (None, "", "bad symbol,??"):
            with self.subTest(configured=configured), patch(
                "watchlist_service.dotenv_values",
                return_value={"CTS_WATCHLIST": configured},
            ):
                self.assertEqual(load_watchlist(), DEFAULT_WATCHLIST)

    def test_explicit_override_has_precedence(self):
        with patch(
            "watchlist_service.dotenv_values",
            return_value={"CTS_WATCHLIST": "MSFT,AMZN"},
        ):
            self.assertEqual(
                resolve_watchlist("$aapl, nvda, aapl"),
                ["AAPL", "NVDA"],
            )

    def test_invalid_override_falls_back_without_mutating_defaults(self):
        original = list(DEFAULT_WATCHLIST)
        with patch(
            "watchlist_service.dotenv_values",
            return_value={"CTS_WATCHLIST": "MSFT"},
        ):
            result = resolve_watchlist("bad symbol,??")
            result.pop()

        self.assertEqual(DEFAULT_WATCHLIST, original)
        self.assertEqual(load_watchlist(), DEFAULT_WATCHLIST)


if __name__ == "__main__":
    unittest.main()
