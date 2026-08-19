import unittest
from unittest.mock import patch

from scanner_service import fetch_scanner_results


class ScannerWatchlistTests(unittest.TestCase):
    def test_scanner_sends_resolved_symbols_to_request(self):
        client = type("Client", (), {
            "get_stock_bars": lambda self, request: type(
                "BarSet", (), {"data": {}}
            )()
        })()

        with patch("scanner_service.resolve_watchlist", return_value=["AAPL", "MS.FT"]), \
             patch("alpaca_service.get_alpaca_credentials", return_value=("key", "secret")), \
             patch("alpaca.data.historical.StockHistoricalDataClient", return_value=client), \
             patch("alpaca.data.requests.StockBarsRequest") as request_type:
            request_type.return_value = object()
            fetch_scanner_results(["AAPL", "MS.FT"])

        self.assertEqual(
            request_type.call_args.kwargs["symbol_or_symbols"],
            ["AAPL", "MS.FT"],
        )

    def test_scanner_override_does_not_change_technical_analysis(self):
        from scanner_service import analyze_bars
        from test_scanner_service import make_bars

        self.assertEqual(analyze_bars("BULL", make_bars("bull")).direction, "CALL")
        self.assertEqual(analyze_bars("BEAR", make_bars("bear")).direction, "PUT")


if __name__ == "__main__":
    unittest.main()
