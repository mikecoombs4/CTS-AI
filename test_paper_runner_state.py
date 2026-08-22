import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core_candidate_selector import decision_identity
from paper_runner_state import (
    PaperRunnerLedger,
    RunnerLockUnavailable,
    SingleRunnerLock,
)


ET = ZoneInfo("America/New_York")
BAR = datetime(2026, 8, 24, 9, 45, tzinfo=ET)
AS_OF = datetime(2026, 8, 24, 10, 0, tzinfo=ET)


class SingleRunnerLockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runner.lock"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_lock_acquisition_succeeds_and_context_releases(self):
        lock = SingleRunnerLock(self.path)
        with lock as owned:
            self.assertIs(owned, lock)
            self.assertTrue(lock.owned)
            self.assertTrue(self.path.exists())
        self.assertFalse(lock.owned)
        self.assertTrue(self.path.exists())

    def test_concurrent_active_lock_is_blocked_immediately(self):
        first = SingleRunnerLock(self.path).acquire()
        self.addCleanup(first.release)
        with self.assertRaises(RunnerLockUnavailable):
            SingleRunnerLock(self.path).acquire()

    def test_failed_contender_does_not_overwrite_owner_metadata(self):
        owner = SingleRunnerLock(self.path).acquire()
        self.addCleanup(owner.release)
        metadata = self.path.read_bytes()
        with self.assertRaises(RunnerLockUnavailable):
            SingleRunnerLock(self.path).acquire()
        self.assertEqual(self.path.read_bytes(), metadata)

    def test_metadata_failure_releases_and_closes_acquired_lock(self):
        with patch(
            "paper_runner_state._write_lock_metadata",
            side_effect=OSError("metadata failure"),
        ):
            with self.assertRaises(OSError):
                SingleRunnerLock(self.path).acquire()
        recovered = SingleRunnerLock(self.path).acquire()
        recovered.release()

    def test_repeated_acquire_same_instance_fails_without_losing_lock(self):
        lock = SingleRunnerLock(self.path).acquire()
        self.addCleanup(lock.release)
        with self.assertRaises(RuntimeError):
            lock.acquire()
        with self.assertRaises(RunnerLockUnavailable):
            SingleRunnerLock(self.path).acquire()

    def test_lock_file_uses_conservative_permissions(self):
        lock = SingleRunnerLock(self.path).acquire()
        self.addCleanup(lock.release)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_release_allows_later_acquisition(self):
        first = SingleRunnerLock(self.path).acquire()
        first.release()
        second = SingleRunnerLock(self.path).acquire()
        self.assertTrue(second.owned)
        second.release()

    def test_stale_file_without_os_lock_is_reacquired(self):
        self.path.write_text("stale pid metadata", encoding="utf-8")
        lock = SingleRunnerLock(self.path).acquire()
        self.assertTrue(lock.owned)
        lock.release()

    def test_unowned_release_does_not_unlock_owner(self):
        owner = SingleRunnerLock(self.path).acquire()
        self.addCleanup(owner.release)
        SingleRunnerLock(self.path).release()
        with self.assertRaises(RunnerLockUnavailable):
            SingleRunnerLock(self.path).acquire()

    def test_abrupt_subprocess_termination_releases_os_lock(self):
        script = (
            "import sys,time; "
            "from pathlib import Path; "
            "from paper_runner_state import SingleRunnerLock; "
            "lock=SingleRunnerLock(Path(sys.argv[1])).acquire(); "
            "print('locked', flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.path)],
            cwd=Path(__file__).parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(RunnerLockUnavailable):
                SingleRunnerLock(self.path).acquire()
            process.terminate()
            process.communicate(timeout=5)
            recovered = SingleRunnerLock(self.path).acquire()
            recovered.release()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


class PaperRunnerLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runner-state.json"
        self.ledger = PaperRunnerLedger(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def claim_cycle(self, bar=BAR, as_of=AS_OF):
        return self.ledger.claim_cycle(bar, as_of)

    def claim_decision(self, ticker="AAPL", bar=BAR, as_of=AS_OF):
        identity = decision_identity(ticker, bar)
        return self.ledger.claim_decision(identity, ticker, bar, as_of)

    def test_first_initialization_is_versioned_and_claimed_before_work(self):
        self.assertFalse(self.path.exists())
        record = self.claim_cycle()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(record.status, "CLAIMED")
        self.assertEqual(saved["cycles"][0]["status"], "CLAIMED")
        self.assertEqual(saved["decisions"], [])

    def test_duplicate_cycle_is_blocked_in_process_and_after_reload(self):
        self.claim_cycle()
        with self.assertRaises(RuntimeError):
            self.claim_cycle()
        restarted = PaperRunnerLedger(self.path)
        with self.assertRaises(RuntimeError):
            restarted.claim_cycle(BAR, AS_OF)

    def test_later_completed_cycle_and_trading_date_are_accepted(self):
        self.claim_cycle()
        later_bar = BAR + timedelta(minutes=15)
        later_as_of = AS_OF + timedelta(minutes=15)
        self.ledger.claim_cycle(later_bar, later_as_of)
        next_bar = datetime(2026, 8, 25, 9, 45, tzinfo=ET)
        next_as_of = datetime(2026, 8, 25, 10, 0, tzinfo=ET)
        self.ledger.claim_cycle(next_bar, next_as_of)
        self.assertEqual(len(self.ledger.cycles), 3)

    def test_duplicate_decision_is_blocked_across_reload_and_case_normalized(self):
        self.claim_cycle()
        claimed = self.claim_decision(" aapl ")
        self.assertEqual(claimed.ticker, "AAPL")
        restarted = PaperRunnerLedger(self.path)
        with self.assertRaises(RuntimeError):
            restarted.claim_decision(claimed.decision_id, "AAPL", BAR, AS_OF)

    def test_crash_left_claims_remain_consumed_and_visible(self):
        cycle = self.claim_cycle()
        decision = self.claim_decision()
        restarted = PaperRunnerLedger(self.path)
        self.assertEqual(restarted.cycles[0], cycle)
        self.assertEqual(restarted.decisions[0], decision)
        self.assertEqual(restarted.cycles[0].status, "CLAIMED")
        self.assertEqual(restarted.decisions[0].status, "CLAIMED")

    def test_exact_terminal_updates_are_idempotent(self):
        self.claim_cycle()
        decision = self.claim_decision()
        completed = AS_OF + timedelta(seconds=1)
        first_decision = self.ledger.complete_decision(
            decision.decision_id, "ELIGIBLE", completed
        )
        second_decision = self.ledger.complete_decision(
            decision.decision_id, "ELIGIBLE", completed
        )
        first_cycle = self.ledger.complete_cycle(
            BAR, "SELECTED", completed, decision.decision_id
        )
        second_cycle = self.ledger.complete_cycle(
            BAR, "SELECTED", completed, decision.decision_id
        )
        self.assertEqual(first_decision, second_decision)
        self.assertEqual(first_cycle, second_cycle)

    def test_terminal_regression_or_conflict_is_blocked(self):
        self.claim_cycle()
        decision = self.claim_decision()
        completed = AS_OF + timedelta(seconds=1)
        self.ledger.complete_decision(decision.decision_id, "BLOCKED", completed)
        self.ledger.complete_cycle(BAR, "BLOCKED", completed)
        with self.assertRaises(RuntimeError):
            self.ledger.complete_decision(decision.decision_id, "ERROR", completed)
        with self.assertRaises(RuntimeError):
            self.ledger.complete_cycle(BAR, "ERROR", completed)
        with self.assertRaises(RuntimeError):
            self.ledger.complete_cycle(BAR, "BLOCKED", completed + timedelta(seconds=1))

    def test_all_cycle_terminal_outcomes_are_supported(self):
        for index, outcome in enumerate(("NO_SELECTION", "BLOCKED", "ERROR")):
            with self.subTest(outcome=outcome):
                path = Path(self.temp_dir.name) / f"{index}.json"
                ledger = PaperRunnerLedger(path)
                ledger.claim_cycle(BAR, AS_OF)
                record = ledger.complete_cycle(
                    BAR, outcome, AS_OF + timedelta(seconds=1)
                )
                self.assertEqual(record.status, outcome)

    def test_decision_cannot_be_added_after_any_terminal_cycle(self):
        for index, outcome in enumerate(("NO_SELECTION", "BLOCKED", "ERROR")):
            with self.subTest(outcome=outcome):
                path = Path(self.temp_dir.name) / f"terminal-{index}.json"
                ledger = PaperRunnerLedger(path)
                ledger.claim_cycle(BAR, AS_OF)
                ledger.complete_cycle(BAR, outcome, AS_OF + timedelta(seconds=1))
                identity = decision_identity("AAPL", BAR)
                with self.assertRaises(RuntimeError):
                    ledger.claim_decision(identity, "AAPL", BAR, AS_OF)

    def test_selected_requires_eligible_decision_from_exact_cycle(self):
        self.claim_cycle()
        decision = self.claim_decision()
        completed = AS_OF + timedelta(seconds=1)
        for outcome in ("CLAIMED", "EXCLUDED", "BLOCKED", "ERROR"):
            with self.subTest(outcome=outcome):
                if outcome != "CLAIMED":
                    path = Path(self.temp_dir.name) / f"selected-{outcome}.json"
                    ledger = PaperRunnerLedger(path)
                    ledger.claim_cycle(BAR, AS_OF)
                    item = ledger.claim_decision(
                        decision_identity("AAPL", BAR), "AAPL", BAR, AS_OF
                    )
                    ledger.complete_decision(item.decision_id, outcome, completed)
                else:
                    ledger = self.ledger
                    item = decision
                with self.assertRaises(RuntimeError):
                    ledger.complete_cycle(BAR, "SELECTED", completed, item.decision_id)

        path = Path(self.temp_dir.name) / "selected-eligible.json"
        ledger = PaperRunnerLedger(path)
        ledger.claim_cycle(BAR, AS_OF)
        identity = decision_identity("AAPL", BAR)
        ledger.claim_decision(identity, "AAPL", BAR, AS_OF)
        ledger.complete_decision(identity, "ELIGIBLE", completed)
        result = ledger.complete_cycle(BAR, "SELECTED", completed, identity)
        self.assertEqual(result.selected_decision_id, identity)

    def test_no_selection_rejects_eligible_or_unresolved_decisions(self):
        for terminal in (None, "ELIGIBLE"):
            with self.subTest(terminal=terminal):
                path = Path(self.temp_dir.name) / f"no-selection-{terminal}.json"
                ledger = PaperRunnerLedger(path)
                ledger.claim_cycle(BAR, AS_OF)
                identity = decision_identity("AAPL", BAR)
                ledger.claim_decision(identity, "AAPL", BAR, AS_OF)
                if terminal:
                    ledger.complete_decision(
                        identity, terminal, AS_OF + timedelta(seconds=1)
                    )
                with self.assertRaises(RuntimeError):
                    ledger.complete_cycle(
                        BAR, "NO_SELECTION", AS_OF + timedelta(seconds=2)
                    )

    def test_blocked_and_error_cycles_may_preserve_unresolved_claims(self):
        for index, outcome in enumerate(("BLOCKED", "ERROR")):
            with self.subTest(outcome=outcome):
                path = Path(self.temp_dir.name) / f"unresolved-{index}.json"
                ledger = PaperRunnerLedger(path)
                ledger.claim_cycle(BAR, AS_OF)
                identity = decision_identity("AAPL", BAR)
                ledger.claim_decision(identity, "AAPL", BAR, AS_OF)
                ledger.complete_cycle(
                    BAR, outcome, AS_OF + timedelta(seconds=1)
                )
                restored = PaperRunnerLedger(path)
                self.assertEqual(restored.cycles[0].status, outcome)
                self.assertEqual(restored.decisions[0].status, "CLAIMED")

    def test_corrupt_and_unknown_version_are_untouched_and_block(self):
        for index, content in enumerate(("not json", '{"version": 999, "cycles": [], "decisions": []}')):
            with self.subTest(content=content):
                path = Path(self.temp_dir.name) / f"invalid-{index}.json"
                path.write_text(content, encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(RuntimeError):
                    PaperRunnerLedger(path)
                self.assertEqual(path.read_bytes(), before)

    def test_atomic_write_failure_preserves_previous_valid_state(self):
        self.claim_cycle()
        before = self.path.read_bytes()
        later_bar = BAR + timedelta(minutes=15)
        later_as_of = AS_OF + timedelta(minutes=15)
        with patch("pathlib.Path.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.ledger.claim_cycle(later_bar, later_as_of)
        self.assertEqual(self.path.read_bytes(), before)
        restored = PaperRunnerLedger(self.path)
        self.assertEqual(len(restored.cycles), 1)

    def test_each_atomic_stage_failure_preserves_state_and_cleans_temp(self):
        self.claim_cycle()
        before = self.path.read_bytes()
        later_bar = BAR + timedelta(minutes=15)
        later_as_of = AS_OF + timedelta(minutes=15)
        stages = (
            "paper_runner_state._write_temporary",
            "paper_runner_state._flush_temporary",
            "paper_runner_state._fsync_temporary",
            "pathlib.Path.replace",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                with patch(stage, side_effect=OSError("stage failure")):
                    with self.assertRaises(RuntimeError):
                        self.ledger.claim_cycle(later_bar, later_as_of)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_utc_and_eastern_equivalent_intervals_are_duplicates(self):
        self.claim_cycle()
        with self.assertRaises(RuntimeError):
            self.ledger.claim_cycle(BAR.astimezone(timezone.utc), AS_OF.astimezone(timezone.utc))

    def test_naive_malformed_future_and_mismatched_data_fail_closed(self):
        with self.assertRaises(ValueError):
            self.ledger.claim_cycle(datetime(2026, 8, 24, 9, 45), AS_OF)
        with self.assertRaises(ValueError):
            self.ledger.claim_cycle("bad", AS_OF)
        with self.assertRaises(ValueError):
            self.ledger.claim_cycle(AS_OF, AS_OF)
        self.claim_cycle()
        valid_id = decision_identity("AAPL", BAR)
        with self.assertRaises(ValueError):
            self.ledger.claim_decision("bad-id", "AAPL", BAR, AS_OF)
        with self.assertRaises(ValueError):
            self.ledger.claim_decision(valid_id, "MSFT", BAR, AS_OF)
        with self.assertRaises(ValueError):
            self.ledger.claim_decision(valid_id, "AAPL", AS_OF, AS_OF)

    def test_audit_history_is_preserved(self):
        self.claim_cycle()
        first = self.claim_decision("AAPL")
        second = self.claim_decision("MSFT")
        self.ledger.complete_decision(
            first.decision_id, "EXCLUDED", AS_OF + timedelta(seconds=1)
        )
        self.ledger.complete_decision(
            second.decision_id, "BLOCKED", AS_OF + timedelta(seconds=2)
        )
        restarted = PaperRunnerLedger(self.path)
        self.assertEqual(len(restarted.cycles), 1)
        self.assertEqual(len(restarted.decisions), 2)

    def test_reload_rejects_duplicate_keys_and_mismatched_selected_identity(self):
        duplicate = (
            '{"version":1,"cycles":[{"interval_start":"2026-08-24T13:45:00+00:00",'
            '"trading_date":"2026-08-24","status":"CLAIMED","status":"ERROR",'
            '"claimed_at":"2026-08-24T14:00:00+00:00","completed_at":null,'
            '"selected_decision_id":null}],"decisions":[]}'
        )
        duplicate_path = Path(self.temp_dir.name) / "duplicate-key.json"
        duplicate_path.write_text(duplicate, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            PaperRunnerLedger(duplicate_path)

        self.claim_cycle()
        identity = decision_identity("AAPL", BAR)
        self.ledger.claim_decision(identity, "AAPL", BAR, AS_OF)
        self.ledger.complete_decision(
            identity, "ELIGIBLE", AS_OF + timedelta(seconds=1)
        )
        self.ledger.complete_cycle(
            BAR, "SELECTED", AS_OF + timedelta(seconds=1), identity
        )
        data = json.loads(self.path.read_text())
        data["cycles"][0]["selected_decision_id"] = decision_identity("MSFT", BAR)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            PaperRunnerLedger(self.path)

    def test_module_imports_no_brokerage_execution_order_catalyst_or_network(self):
        tree = ast.parse(Path("paper_runner_state.py").read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        forbidden = {
            "alpaca", "alpaca_service", "broker_readiness_service", "requests",
            "options_service", "paper_execution_service", "order_preview_service",
            "supervised_paper_entry_handoff", "catalyst_service", "catalyst_monitor",
        }
        self.assertFalse(any(
            name == item or name.startswith(item + ".")
            for name in imports for item in forbidden
        ))


if __name__ == "__main__":
    unittest.main()
