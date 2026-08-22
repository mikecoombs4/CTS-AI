import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from autonomous_paper_runner import AutonomousPaperRunner, RunnerDependencies, RunnerPaths
from core_candidate_selector import select_core_candidates
from paper_runner_state import RunnerLockUnavailable, SingleRunnerLock
from scanner_service import ScannerResult


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 24, 9, 45, tzinfo=ET)


def scanner(ticker="AAPL"):
    start = NOW - timedelta(minutes=15)
    return ScannerResult(
        ticker, start, "CALL", 150.0, 151.0, 149.0, 149.5, 148.0, 1.0, 2.0,
        True, True, True, True, start + timedelta(minutes=15),
    )


def startup():
    return SimpleNamespace(
        status="STARTUP_READY", paper_configuration_verified=True,
        autonomous_configuration_verified=True, execution_configuration_verified=True,
        broker_ready=True, entry_gate_open=False, submission_authorized=False,
        trial_limits=SimpleNamespace(max_trades_per_day=1, max_open_positions=1),
        broker_readiness=SimpleNamespace(status="PASS", paper_mode=True), reasons=[],
    )


def readiness(candidate):
    preview = SimpleNamespace(
        ticker=candidate.ticker, contract_symbol="AAPL260918C00150000", side="BUY",
        quantity=1, order_type="LIMIT", time_in_force="DAY", limit_price=1.0,
        estimated_cost=100.0, eligible=True, reasons=["passed"],
    )
    return SimpleNamespace(
        status="PASS", allowed=True, submission_allowed=False, execution_enabled=True,
        reasons=[], scanner_candidate=candidate,
        broker_readiness=SimpleNamespace(status="PASS", paper_mode=True),
        option_liquidity=SimpleNamespace(acceptable=True),
        trade_plan=SimpleNamespace(acceptable=True),
        news_risk=SimpleNamespace(status="PASS"), earnings_risk=SimpleNamespace(status="PASS"),
        final_decision=SimpleNamespace(status="PASS", automatic_paper_eligible=True),
        daily_limits=SimpleNamespace(status="PASS", new_trade_allowed=True),
        order_preview=preview, market_session=SimpleNamespace(status="PASS", entry_allowed=True),
        state=SimpleNamespace(), duplicate_contract=False,
    )


class AutonomousPaperRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = RunnerPaths(Path(self.temp.name))
        self.candidate = scanner()
        self.handoff = Mock(return_value=SimpleNamespace(
            submitted=True, status="SUBMITTED", client_order_id="safe-client-id"
        ))
        self.scan = Mock(return_value=([self.candidate], []))
        self.monitor = Mock(return_value=SimpleNamespace(
            success=True, monitored_symbols=[], failed_actions=[], blocking_reasons=[],
            heartbeat_at=NOW.isoformat(),
        ))
        self.positions = Mock(return_value=[])
        self.broker = Mock(return_value=SimpleNamespace(status="PASS", paper_mode=True))
        self.preflight = Mock(return_value=startup())
        self.sleep = Mock()
        self.dependencies = RunnerDependencies(
            configuration={
                "ALPACA_PAPER": "true", "CTS_AUTONOMOUS_PAPER_ENABLED": "true",
                "CTS_PAPER_EXECUTION_ENABLED": "YES_PAPER_ONLY",
                "CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW": "true",
            },
            resolve_watchlist=Mock(return_value=["AAPL", "MSFT"]), scan=self.scan,
            build_readiness=Mock(side_effect=lambda item, now: (readiness(item), now)),
            policy=Mock(return_value=SimpleNamespace(
                status="PAPER_POLICY_PASS", allowed=True, live_execution_eligible=False,
                softened_gate=None, reasons=("paper pass",),
            )),
            selector=select_core_candidates, startup_preflight=self.preflight,
            broker_readiness=self.broker,
            clock=Mock(return_value=SimpleNamespace(timestamp=NOW, market_open=True)),
            lookup_client_order=Mock(), lookup_broker_order=Mock(),
            positions=self.positions, monitor_cycle=self.monitor,
            synchronize_realized_pl=Mock(return_value=SimpleNamespace(
                success=True, reason=None, realized_pnl=0,
            )),
            paper_state_health=Mock(return_value=(True, "fresh")),
            handoff=self.handoff, submitter=Mock(), now=Mock(return_value=NOW),
            sleep=self.sleep,
        )

    def tearDown(self):
        self.temp.cleanup()

    def runner(self):
        return AutonomousPaperRunner(self.paths, self.dependencies, poll_seconds=1)

    def test_check_runs_health_diagnostics_without_scan_or_submission(self):
        result = self.runner().run("check")
        self.assertEqual(result.status, "CHECK_OK")
        self.scan.assert_not_called()
        self.handoff.assert_not_called()
        self.monitor.assert_not_called()
        self.dependencies.synchronize_realized_pl.assert_called_once_with(NOW)
        self.assertFalse(result.entry_gate_open)

    def test_dry_run_executes_real_selection_and_never_handoff(self):
        result = self.runner().run("dry-run", max_iterations=1)
        self.assertEqual(result.cycles_processed, 1)
        self.handoff.assert_not_called()
        events = json.loads(self.paths.audit.read_text())["events"]
        self.assertIn("DRY_RUN_ONLY", [item["event"] for item in events])

    def test_execute_calls_only_handoff_once(self):
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertTrue(result.submitted)
        self.handoff.assert_called_once()
        self.assertEqual(self.handoff.call_args.kwargs["origin"], "CORE_CTS")
        self.assertEqual(self.dependencies.synchronize_realized_pl.call_count, 3)

    def test_sync_failure_blocks_entry_but_exit_supervision_continues(self):
        self.dependencies.synchronize_realized_pl.return_value = SimpleNamespace(
            success=False, reason="untrusted history", realized_pnl=None,
        )
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(result.submitted)
        self.monitor.assert_called()
        self.scan.assert_not_called()
        self.handoff.assert_not_called()

    def test_running_loop_refreshes_pl_every_poll_cycle(self):
        self.dependencies.clock.return_value = SimpleNamespace(
            timestamp=NOW.replace(hour=9, minute=31), market_open=True,
        )
        self.runner().run("dry-run", max_iterations=3)
        # Startup plus every loop iteration; this is stricter than five minutes.
        self.assertEqual(self.dependencies.synchronize_realized_pl.call_count, 4)

    def test_immediate_pre_handoff_sync_failure_prevents_submission(self):
        good = SimpleNamespace(success=True, reason=None, realized_pnl=0)
        bad = SimpleNamespace(success=False, reason="changed evidence", realized_pnl=None)
        self.dependencies.synchronize_realized_pl.side_effect = [good, good, bad]
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(result.submitted)
        self.assertEqual(self.dependencies.synchronize_realized_pl.call_count, 3)
        self.handoff.assert_not_called()

    def test_restart_cannot_repeat_claimed_bar_or_submit(self):
        self.runner().run("dry-run", max_iterations=1)
        self.handoff.reset_mock()
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertEqual(result.cycles_processed, 0)
        self.handoff.assert_not_called()

    def test_configuration_exit_health_and_market_clock_fail_closed(self):
        self.dependencies.configuration["ALPACA_PAPER"] = "false"
        self.assertEqual(self.runner().run("dry-run", max_iterations=1).status, "BLOCKED")
        self.dependencies.configuration["ALPACA_PAPER"] = "true"
        self.monitor.return_value = SimpleNamespace(
            success=False, monitored_symbols=[], failed_actions=["failure"],
            blocking_reasons=["failed"], heartbeat_at=NOW.isoformat(),
        )
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(result.submitted)
        self.handoff.assert_not_called()

    def test_provider_failure_consumes_cycle_and_never_submits(self):
        self.scan.side_effect = TimeoutError("provider")
        first = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(first.submitted)
        self.scan.reset_mock(side_effect=True)
        self.scan.return_value = ([self.candidate], [])
        second = self.runner().run("execute-paper", max_iterations=1)
        self.assertEqual(second.cycles_processed, 0)
        self.handoff.assert_not_called()

    def test_no_eligible_candidates_completes_safely(self):
        self.scan.return_value = ([], ["AAPL"])
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(result.submitted)
        self.handoff.assert_not_called()

    def test_lock_releases_on_keyboard_interrupt(self):
        self.dependencies.clock.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.runner().run("dry-run", max_iterations=1)
        self.dependencies.clock.side_effect = None
        self.dependencies.clock.return_value = SimpleNamespace(timestamp=NOW, market_open=True)
        self.assertEqual(self.runner().run("check").status, "CHECK_OK")

    def test_invalid_mode_and_polling_interval_fail(self):
        with self.assertRaises(ValueError):
            self.runner().run("live")
        with self.assertRaises(ValueError):
            AutonomousPaperRunner(self.paths, self.dependencies, poll_seconds=0)

    def test_audit_contains_no_configuration_or_credentials(self):
        self.runner().run("dry-run", max_iterations=0)
        text = self.paths.audit.read_text()
        self.assertNotIn("ALPACA", text)
        self.assertNotIn("secret", text.lower())

    def test_startup_call_order_runs_monitor_before_entry_preflight(self):
        order = []
        self.positions.side_effect = lambda: order.append("positions") or []
        self.monitor.side_effect = lambda **kwargs: order.append("monitor") or SimpleNamespace(
            success=True, monitored_symbols=[], failed_actions=[], blocking_reasons=[],
            heartbeat_at=NOW.isoformat(),
        )
        self.broker.side_effect = lambda: order.append("broker") or SimpleNamespace(
            status="PASS", paper_mode=True
        )
        self.preflight.side_effect = lambda **kwargs: order.append("preflight") or startup()
        self.runner().run("dry-run", max_iterations=0)
        self.assertEqual(order[:4], ["positions", "monitor", "broker", "preflight"])

    def test_duplicate_runner_and_corrupt_state_fail_closed(self):
        with SingleRunnerLock(self.paths.lock):
            with self.assertRaises(RunnerLockUnavailable):
                self.runner().run("check")
        self.paths.audit.write_text("not-json", encoding="utf-8")
        result = self.runner().run("check")
        self.assertEqual(result.status, "BLOCKED")
        self.handoff.assert_not_called()

    def test_changed_hard_gate_during_final_revalidation_prevents_handoff(self):
        self.dependencies.policy.side_effect = (
            SimpleNamespace(
                status="PAPER_POLICY_PASS", allowed=True,
                live_execution_eligible=False, softened_gate=None, reasons=("pass",),
            ),
            SimpleNamespace(
                status="BLOCKED", allowed=False,
                live_execution_eligible=False, softened_gate=None, reasons=("changed",),
            ),
        )
        result = self.runner().run("execute-paper", max_iterations=1)
        self.assertFalse(result.submitted)
        self.handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
