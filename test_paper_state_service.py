import tempfile
import unittest
from datetime import date
from pathlib import Path

from paper_state_service import (
    add_position,
    load_state,
    make_position,
    new_state,
    remove_position,
    save_state,
)


TODAY = date(2026, 8, 1)


class PaperStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "state.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_missing_file_loads_empty_state(self) -> None:
        state = load_state(self.path, today=TODAY)

        self.assertEqual(state.trades_opened, 0)
        self.assertEqual(state.positions, [])

    def test_position_survives_save_and_reload(self) -> None:
        state = new_state(TODAY)
        add_position(
            state,
            make_position("TEST", "TEST_CALL", 0.35, "order-1"),
        )
        save_state(state, self.path)
        restored = load_state(self.path, today=TODAY)

        self.assertEqual(restored.trades_opened, 1)
        self.assertEqual(restored.positions[0].contract_symbol, "TEST_CALL")

    def test_new_day_resets_counters_but_preserves_positions(self) -> None:
        state = new_state(date(2026, 7, 31))
        state.trades_opened = 2
        state.losing_trades = 1
        state.realized_pnl = -9.0
        add_position(
            state,
            make_position("TEST", "TEST_CALL", 0.35, "order-1"),
        )
        save_state(state, self.path)
        restored = load_state(self.path, today=TODAY)

        self.assertEqual(restored.trades_opened, 0)
        self.assertEqual(restored.losing_trades, 0)
        self.assertEqual(len(restored.positions), 1)

    def test_closed_position_updates_pnl_and_loss_count(self) -> None:
        state = new_state(TODAY)
        add_position(
            state,
            make_position("TEST", "TEST_CALL", 0.35, "order-1"),
        )
        pnl = remove_position(state, "TEST_CALL", 0.26)

        self.assertAlmostEqual(pnl, -9.0)
        self.assertEqual(state.losing_trades, 1)
        self.assertEqual(state.positions, [])

    def test_corrupt_state_fails_closed(self) -> None:
        self.path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            load_state(self.path, today=TODAY)


if __name__ == "__main__":
    unittest.main()
