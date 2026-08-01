import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from market_session_service import evaluate_market_session


ET = ZoneInfo("America/New_York")


class MarketSessionTests(unittest.TestCase):
    def evaluate(self, weekday_day: int, hour: int, minute: int):
        return evaluate_market_session(
            datetime(2026, 8, weekday_day, hour, minute, tzinfo=ET)
        )

    def test_weekend_blocks_entries(self) -> None:
        result = self.evaluate(1, 10, 0)

        self.assertEqual(result.status, "BLOCK")

    def test_first_fifteen_minutes_block_entries(self) -> None:
        result = self.evaluate(3, 9, 40)

        self.assertEqual(result.status, "BLOCK")

    def test_morning_window_opens_at_945(self) -> None:
        result = self.evaluate(3, 9, 45)

        self.assertEqual(result.status, "PASS")

    def test_morning_window_includes_1130(self) -> None:
        result = self.evaluate(3, 11, 30)

        self.assertEqual(result.status, "PASS")

    def test_lunch_window_blocks_entries(self) -> None:
        result = self.evaluate(3, 12, 15)

        self.assertEqual(result.status, "BLOCK")

    def test_afternoon_window_opens_at_100(self) -> None:
        result = self.evaluate(3, 13, 0)

        self.assertEqual(result.status, "PASS")

    def test_afternoon_window_includes_330(self) -> None:
        result = self.evaluate(3, 15, 30)

        self.assertEqual(result.status, "PASS")

    def test_after_330_blocks_new_entries(self) -> None:
        result = self.evaluate(3, 15, 31)

        self.assertEqual(result.status, "BLOCK")


if __name__ == "__main__":
    unittest.main()
