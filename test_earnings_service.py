import unittest
from datetime import date

from earnings_service import classify_earnings_date


TODAY = date(2026, 8, 1)


class EarningsRiskTests(unittest.TestCase):
    def test_earnings_tomorrow_blocks_candidate(self) -> None:
        result = classify_earnings_date(
            ticker="TEST",
            report_date=date(2026, 8, 2),
            today=TODAY,
        )

        self.assertEqual(result.status, "BLOCK")

    def test_earnings_in_three_days_requires_review(self) -> None:
        result = classify_earnings_date(
            ticker="TEST",
            report_date=date(2026, 8, 4),
            today=TODAY,
        )

        self.assertEqual(result.status, "REVIEW")

    def test_distant_earnings_passes(self) -> None:
        result = classify_earnings_date(
            ticker="TEST",
            report_date=date(2026, 8, 10),
            today=TODAY,
        )

        self.assertEqual(result.status, "PASS")

    def test_missing_stock_date_requires_review(self) -> None:
        result = classify_earnings_date(
            ticker="TEST",
            report_date=None,
            today=TODAY,
        )

        self.assertEqual(result.status, "REVIEW")

    def test_etf_passes_without_earnings(self) -> None:
        result = classify_earnings_date(
            ticker="SPY",
            report_date=None,
            today=TODAY,
        )

        self.assertEqual(result.status, "PASS")


if __name__ == "__main__":
    unittest.main()
