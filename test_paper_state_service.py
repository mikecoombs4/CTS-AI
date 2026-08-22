import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from paper_state_service import (
    add_position,
    load_state,
    make_position,
    new_state,
    record_submitted_contract,
    remove_position,
    save_state,
    record_verified_realized_pnl,
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

    def test_new_day_rollover_resets_daily_counters_and_preserves_positions(self) -> None:
        state = new_state(date(2026, 7, 31))
        state.trades_opened = 2
        state.losing_trades = 1
        state.realized_pnl = -9.0
        state.realized_pnl_verified_at = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc).isoformat()
        state.submitted_contracts = ["TEST_CALL"]
        add_position(
            state,
            make_position("TEST", "TEST_CALL", 0.35, "order-1"),
        )
        save_state(state, self.path)
        restored = load_state(self.path, today=TODAY)

        self.assertEqual(restored.trades_opened, 0)
        self.assertEqual(restored.losing_trades, 0)
        self.assertEqual(restored.realized_pnl, -9.0)
        self.assertEqual(
            restored.realized_pnl_verified_at,
            datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc).isoformat(),
        )
        self.assertEqual(restored.submitted_contracts, [])
        self.assertEqual(len(restored.positions), 1)

    def test_submitted_contracts_survive_save_and_reload(self) -> None:
        state = new_state(TODAY)
        record_submitted_contract(state, "TEST_CALL")
        save_state(state, self.path)
        restored = load_state(self.path, today=TODAY)

        self.assertEqual(restored.submitted_contracts, ["TEST_CALL"])

    def test_record_submitted_contract_and_add_position_counts_one_trade(self) -> None:
        state = new_state(TODAY)
        record_submitted_contract(state, "TEST_CALL")
        add_position(
            state,
            make_position("TEST", "TEST_CALL", 0.35, "order-1"),
        )

        self.assertEqual(state.trades_opened, 1)
        self.assertEqual(state.submitted_contracts, ["TEST_CALL"])

    def test_update_realized_pnl_verified_at_accepts_aware_timestamp(self) -> None:
        state = new_state(TODAY)
        aware_timestamp = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        from paper_state_service import update_realized_pnl_verified_at

        update_realized_pnl_verified_at(state, aware_timestamp)
        self.assertEqual(state.realized_pnl_verified_at, aware_timestamp.isoformat())

    def test_update_realized_pnl_verified_at_rejects_naive_timestamp(self) -> None:
        state = new_state(TODAY)
        naive_timestamp = datetime(2026, 8, 1, 10, 0)
        from paper_state_service import update_realized_pnl_verified_at

        with self.assertRaises(ValueError):
            update_realized_pnl_verified_at(state, naive_timestamp)

    def test_remove_position_contract_matching_is_case_insensitive(self) -> None:
        state = new_state(TODAY)
        add_position(
            state,
            make_position("TEST", "test_call", 0.35, "order-1"),
        )
        pnl = remove_position(state, "TEST_CALL", 0.26)

        self.assertAlmostEqual(pnl, -9.0)
        self.assertEqual(state.positions, [])

    def test_corrupt_state_fails_closed(self) -> None:
        self.path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            load_state(self.path, today=TODAY)

    def test_verified_pnl_source_and_evidence_round_trip(self) -> None:
        state = new_state(TODAY)
        verified = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(record_verified_realized_pnl(
            state, trading_date=TODAY, realized_pnl=-12.5,
            verified_at=verified, source="PAPER_BROKER_MANAGED_FILLS",
            evidence_id="digest",
        ))
        save_state(state, self.path)
        restored = load_state(self.path, today=TODAY)
        self.assertEqual(restored.realized_pnl, -12.5)
        self.assertEqual(restored.realized_pnl_verification_source, "PAPER_BROKER_MANAGED_FILLS")
        self.assertEqual(restored.realized_pnl_evidence_id, "digest")

    def test_older_verified_evidence_cannot_overwrite_newer(self) -> None:
        state = new_state(TODAY)
        newer = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
        record_verified_realized_pnl(
            state, trading_date=TODAY, realized_pnl=-20, verified_at=newer,
            source="PAPER_BROKER_MANAGED_FILLS", evidence_id="newer",
        )
        self.assertFalse(record_verified_realized_pnl(
            state, trading_date=TODAY, realized_pnl=10,
            verified_at=newer.replace(hour=14),
            source="PAPER_BROKER_MANAGED_FILLS", evidence_id="older",
        ))
        self.assertEqual(state.realized_pnl, -20)


if __name__ == "__main__":
    unittest.main()
