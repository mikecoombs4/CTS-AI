import unittest

from daily_limits_service import (
    evaluate_daily_limits,
    record_closed_trade,
)


class DailyLimitsTests(unittest.TestCase):
    def test_start_of_day_allows_trade(self) -> None:
        result = evaluate_daily_limits(0, 0, 0, 0.0)

        self.assertEqual(result.status, "PASS")

    def test_one_open_trade_allows_second_trade(self) -> None:
        result = evaluate_daily_limits(1, 1, 0, 0.0)

        self.assertTrue(result.new_trade_allowed)

    def test_two_trades_blocks_third_trade(self) -> None:
        result = evaluate_daily_limits(2, 0, 0, 15.0)

        self.assertEqual(result.status, "BLOCK")

    def test_two_open_positions_blocks_new_trade(self) -> None:
        result = evaluate_daily_limits(2, 2, 0, 0.0)

        self.assertEqual(result.status, "BLOCK")

    def test_profitable_trailing_exit_does_not_count_as_loss(self) -> None:
        losses, pnl = record_closed_trade(0, 0.0, 6.0)
        result = evaluate_daily_limits(1, 0, losses, pnl)

        self.assertEqual(losses, 0)
        self.assertTrue(result.new_trade_allowed)

    def test_first_realized_loss_ends_entries(self) -> None:
        losses, pnl = record_closed_trade(0, 0.0, -9.0)
        result = evaluate_daily_limits(1, 0, losses, pnl)

        self.assertEqual(losses, 1)
        self.assertEqual(result.status, "BLOCK")

    def test_fifty_dollar_loss_blocks_entries(self) -> None:
        result = evaluate_daily_limits(1, 0, 0, -50.0)

        self.assertEqual(result.status, "BLOCK")


if __name__ == "__main__":
    unittest.main()
