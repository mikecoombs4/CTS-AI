import unittest

from position_tracker import simulate_price_path


class SimulatedPositionTests(unittest.TestCase):
    def test_target_path_exits_at_target(self) -> None:
        position, _ = simulate_price_path(
            entry_price=1.00,
            prices=[1.00, 1.20, 1.30, 1.35],
        )

        self.assertTrue(position.closed)
        self.assertEqual(position.exit_action, "EXIT_TARGET")
        self.assertAlmostEqual(position.exit_price, 1.35)

    def test_trailing_path_exits_after_reversal(self) -> None:
        position, _ = simulate_price_path(
            entry_price=1.00,
            prices=[1.00, 1.20, 1.30, 1.17],
        )

        self.assertTrue(position.closed)
        self.assertEqual(
            position.exit_action,
            "EXIT_TRAILING_STOP",
        )

    def test_initial_stop_path_exits(self) -> None:
        position, _ = simulate_price_path(
            entry_price=1.00,
            prices=[1.00, 0.90, 0.75],
        )

        self.assertTrue(position.closed)
        self.assertEqual(
            position.exit_action,
            "EXIT_INITIAL_STOP",
        )

    def test_simulation_stops_after_exit(self) -> None:
        _, steps = simulate_price_path(
            entry_price=1.00,
            prices=[1.00, 0.75, 1.50],
        )

        self.assertEqual(len(steps), 2)

    def test_peak_and_trailing_state_are_preserved(self) -> None:
        position, _ = simulate_price_path(
            entry_price=1.00,
            prices=[1.00, 1.20, 1.28],
        )

        self.assertFalse(position.closed)
        self.assertTrue(position.trailing_active)
        self.assertAlmostEqual(position.peak_price, 1.28)


if __name__ == "__main__":
    unittest.main()
