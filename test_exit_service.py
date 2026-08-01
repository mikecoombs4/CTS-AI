import unittest

from exit_service import evaluate_exit


ENTRY = 1.00


class ExitDecisionTests(unittest.TestCase):
    def test_holds_between_initial_stop_and_activation(self) -> None:
        decision = evaluate_exit(ENTRY, 1.10)

        self.assertEqual(decision.action, "HOLD_INITIAL")
        self.assertFalse(decision.trailing_active)

    def test_initial_stop_exits_at_25_percent_loss(self) -> None:
        decision = evaluate_exit(ENTRY, 0.75)

        self.assertEqual(decision.action, "EXIT_INITIAL_STOP")

    def test_twenty_percent_gain_arms_trailing_stop(self) -> None:
        decision = evaluate_exit(ENTRY, 1.20)

        self.assertEqual(decision.action, "ARM_TRAILING_STOP")
        self.assertTrue(decision.trailing_active)
        self.assertAlmostEqual(decision.trailing_stop_price, 1.08)

    def test_trailing_stop_rises_with_new_peak(self) -> None:
        decision = evaluate_exit(
            ENTRY,
            1.28,
            peak_price=1.25,
            trailing_active=True,
        )

        self.assertEqual(decision.action, "HOLD_TRAILING")
        self.assertAlmostEqual(decision.peak_price, 1.28)
        self.assertAlmostEqual(decision.trailing_stop_price, 1.152)

    def test_ten_percent_drop_from_peak_exits(self) -> None:
        decision = evaluate_exit(
            ENTRY,
            1.125,
            peak_price=1.25,
            trailing_active=True,
        )

        self.assertEqual(decision.action, "EXIT_TRAILING_STOP")

    def test_thirty_five_percent_gain_exits_at_target(self) -> None:
        decision = evaluate_exit(
            ENTRY,
            1.35,
            peak_price=1.30,
            trailing_active=True,
        )

        self.assertEqual(decision.action, "EXIT_TARGET")


if __name__ == "__main__":
    unittest.main()
