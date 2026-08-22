import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exit_monitor import MonitorConfig, PaperExitMonitor
from exit_monitor_supervisor import supervise_exit_monitor
from paper_entry_order_tracker import PaperEntryOrderTracker
from supervised_paper_entry_handoff import (
    CORE_ORIGIN,
    SubmissionIntent,
    SubmissionIntentJournal,
)


NOW = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
SYMBOL = "AAPL260918C00150000"


def broker_position(symbol=SYMBOL, qty="1"):
    return SimpleNamespace(
        symbol=symbol, qty=qty, side="long", asset_class="us_option",
        current_price="1.0", avg_entry_price="1.0", unrealized_plpc="0",
    )


def broker_order(status="filled", filled_qty="1", **overrides):
    values = {
        "id": "broker-1", "client_order_id": "client-1", "symbol": SYMBOL,
        "qty": "1", "filled_qty": filled_qty, "limit_price": "1.00",
        "filled_avg_price": "0.95" if float(filled_qty) else None,
        "status": status, "submitted_at": NOW.isoformat(),
        "side": "buy", "type": "limit", "time_in_force": "day",
        "position_intent": "buy_to_open",
        "updated_at": NOW.isoformat(),
        "filled_at": NOW.isoformat() if float(filled_qty) else None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ExitMonitorSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.health_path = root / "health.json"
        self.journal_path = root / "intents.json"
        self.tracker_path = root / "tracker.json"

    def tearDown(self):
        self.temp.cleanup()

    def cycle_result(self, symbols=(), **overrides):
        values = {
            "success": True,
            "monitored_symbols": list(symbols),
            "failed_actions": [],
            "blocking_reasons": [],
            "heartbeat_at": NOW.isoformat(),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def call(self, positions=(), cycle=None, **overrides):
        values = {
            "health_path": self.health_path,
            "journal_path": self.journal_path,
            "tracker_path": self.tracker_path,
            "retrieve_broker_positions": Mock(return_value=list(positions)),
            "run_monitor_cycle": cycle or Mock(return_value=self.cycle_result()),
            "now": NOW,
        }
        values.update(overrides)
        return supervise_exit_monitor(**values)

    def track(self, status="filled", filled_qty="1"):
        return PaperEntryOrderTracker(self.tracker_path).register_submitted(
            broker_order(status, filled_qty), "AAPL"
        )

    def uncertain_intent(self):
        journal = SubmissionIntentJournal(self.journal_path, now=NOW)
        journal.persist(SubmissionIntent(
            paper_only=True, origin=CORE_ORIGIN, trading_date="2026-08-24",
            client_order_id="intent-client", ticker="AAPL", option_symbol=SYMBOL,
            quantity=1, limit_price=1.0, status="INTENT_PERSISTED",
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
        ))
        journal.update(journal.intents[0], "SUBMISSION_UNCERTAIN", NOW.isoformat())

    def test_healthy_startup_with_no_positions_runs_fresh_cycle(self):
        cycle = Mock(return_value=self.cycle_result())
        result = self.call(cycle=cycle)
        self.assertTrue(result.ready)
        self.assertEqual(result.status, "READY")
        cycle.assert_called_once_with(now=NOW, positions_snapshot=[])
        self.assertEqual(json.loads(self.health_path.read_text())["version"], 1)

    def test_healthy_startup_with_exact_tracked_position(self):
        self.track()
        result = self.call(
            positions=[broker_position()],
            cycle=Mock(return_value=self.cycle_result([SYMBOL.lower()])),
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.record.monitored_symbols, [SYMBOL])

    def test_tracker_exposure_missing_from_broker_blocks(self):
        self.track()
        result = self.call(cycle=Mock(return_value=self.cycle_result([SYMBOL])))
        self.assertFalse(result.ready)
        self.assertTrue(any("missing from broker" in item for item in result.reasons))

    def test_unknown_broker_option_position_blocks_even_when_monitored(self):
        result = self.call(
            positions=[broker_position()],
            cycle=Mock(return_value=self.cycle_result([SYMBOL])),
        )
        self.assertFalse(result.ready)
        self.assertTrue(any("outside CTS" in item for item in result.reasons))

    def test_pending_and_uncertain_entries_block(self):
        cases = ("pending", "uncertain")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                root = Path(self.temp.name)
                self.health_path = root / f"health-{index}.json"
                self.journal_path = root / f"journal-{index}.json"
                self.tracker_path = root / f"tracker-{index}.json"
                if case == "pending":
                    self.track("accepted", "0")
                else:
                    self.uncertain_intent()
                self.assertFalse(self.call().ready)

    def test_monitor_exception_failed_cycle_and_failed_action_block(self):
        cycles = (
            Mock(side_effect=TimeoutError("timeout")),
            Mock(return_value=self.cycle_result(success=False)),
            Mock(return_value=self.cycle_result(failed_actions=["close failed"])),
        )
        for index, cycle in enumerate(cycles):
            with self.subTest(index=index):
                self.health_path = Path(self.temp.name) / f"cycle-{index}.json"
                self.assertFalse(self.call(cycle=cycle).ready)

    def test_stale_heartbeat_blocks_and_restart_requires_fresh_cycle(self):
        stale = Mock(return_value=self.cycle_result(
            heartbeat_at=(NOW - timedelta(minutes=5)).isoformat()
        ))
        self.assertFalse(self.call(cycle=stale).ready)
        fresh_cycle = Mock(return_value=self.cycle_result())
        self.assertTrue(self.call(cycle=fresh_cycle).ready)
        fresh_cycle.assert_called_once()

    def test_old_successful_heartbeat_never_bypasses_startup_cycle(self):
        self.assertTrue(self.call().ready)
        failing = Mock(side_effect=RuntimeError("startup failed"))
        result = self.call(cycle=failing, now=NOW + timedelta(seconds=10))
        self.assertFalse(result.ready)
        failing.assert_called_once()

    def test_corrupt_and_unknown_health_state_fail_closed_without_repair(self):
        for index, content in enumerate(("not-json", '{"version":99,"record":{}}')):
            with self.subTest(content=content):
                self.health_path = Path(self.temp.name) / f"corrupt-{index}.json"
                self.health_path.write_text(content, encoding="utf-8")
                before = self.health_path.read_bytes()
                cycle = Mock()
                with self.assertRaises(RuntimeError):
                    self.call(cycle=cycle)
                cycle.assert_not_called()
                self.assertEqual(self.health_path.read_bytes(), before)

    def test_health_persistence_failure_blocks_and_preserves_previous_file(self):
        self.assertTrue(self.call().ready)
        before = self.health_path.read_bytes()
        stages = (
            "exit_monitor_supervisor._write_temporary",
            "exit_monitor_supervisor._flush_temporary",
            "exit_monitor_supervisor._fsync_temporary",
            "pathlib.Path.replace",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                with patch(stage, side_effect=OSError("disk failure")):
                    result = self.call(now=NOW + timedelta(seconds=1))
                self.assertFalse(result.ready)
                self.assertEqual(self.health_path.read_bytes(), before)
                self.assertFalse(self.health_path.with_suffix(".json.tmp").exists())

    def test_partial_fill_terminal_exposure_requires_monitor_handoff(self):
        record = self.track("canceled", "0.5")
        self.assertTrue(record.requires_exit_monitor_handoff)
        result = self.call(
            positions=[broker_position(qty="0.5")],
            cycle=Mock(return_value=self.cycle_result([SYMBOL])),
        )
        self.assertTrue(result.ready)

    def test_quantity_symbol_and_duplicate_disagreements_block(self):
        self.track()
        cases = (
            [broker_position(qty="2")],
            [broker_position("MSFT260918C00400000")],
            [broker_position(), broker_position()],
        )
        for index, positions in enumerate(cases):
            with self.subTest(index=index):
                self.health_path = Path(self.temp.name) / f"mismatch-{index}.json"
                result = self.call(
                    positions=positions,
                    cycle=Mock(return_value=self.cycle_result([SYMBOL])),
                )
                self.assertFalse(result.ready)

    def test_tracker_duplicate_exposure_records_fail_closed(self):
        first = self.track()
        tracker_data = json.loads(self.tracker_path.read_text())
        duplicate = dict(tracker_data["orders"][0])
        duplicate["client_order_id"] = "client-2"
        duplicate["broker_order_id"] = "broker-2"
        tracker_data["orders"].append(duplicate)
        self.tracker_path.write_text(json.dumps(tracker_data), encoding="utf-8")
        result = self.call(
            positions=[broker_position(qty=str(first.filled_quantity))],
            cycle=Mock(return_value=self.cycle_result([SYMBOL])),
        )
        self.assertFalse(result.ready)

    def test_malformed_cycle_and_position_results_fail_closed(self):
        cases = (
            {"retrieve_broker_positions": Mock(return_value=None)},
            {"retrieve_broker_positions": Mock(side_effect=TimeoutError("provider"))},
            {"run_monitor_cycle": Mock(return_value=SimpleNamespace(success=True))},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(index=index):
                self.health_path = Path(self.temp.name) / f"malformed-{index}.json"
                self.assertFalse(self.call(**overrides).ready)

    def test_equivalent_utc_and_eastern_heartbeat_is_fresh(self):
        eastern = timezone(timedelta(hours=-4))
        result = self.call(
            cycle=Mock(return_value=self.cycle_result(
                heartbeat_at=NOW.astimezone(eastern).isoformat()
            ))
        )
        self.assertTrue(result.ready)

    def test_real_structured_monitor_interface_accepts_same_empty_snapshot(self):
        root = Path(self.temp.name)
        client = SimpleNamespace(
            get_all_positions=Mock(side_effect=AssertionError("snapshot must be reused")),
            get_orders=Mock(return_value=[]),
        )
        logger = logging.getLogger(f"supervisor.integration.{id(self)}")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        monitor = PaperExitMonitor(
            client,
            config=MonitorConfig(
                state_file=root / "real-monitor-state.json",
                log_file=root / "real-monitor.log",
            ),
            logger=logger,
        )
        actual_now = datetime.now(timezone.utc)
        result = self.call(cycle=monitor.cycle, now=actual_now)
        self.assertTrue(result.ready)
        client.get_all_positions.assert_not_called()

    def test_no_entry_or_live_execution_path_is_imported_or_called(self):
        source = Path("exit_monitor_supervisor.py").read_text(encoding="utf-8")
        for forbidden in (
            "submit_paper_entry", "submit_order", "cancel_order", "replace_order",
            "close_position", "TradingClient", "alpaca_service", "catalyst",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
