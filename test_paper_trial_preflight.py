import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from broker_readiness_service import BrokerReadinessResult
from paper_trial_preflight import (
    DEFAULT_MAX_OPEN_POSITIONS,
    DEFAULT_MAX_TRADES_PER_DAY,
    TrialLimits,
    resolve_paper_mode,
    resolve_trial_limits,
    run_autonomous_paper_startup_preflight,
    run_paper_trial_preflight,
)


class PaperTrialPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state_path = root / "state" / "paper.json"
        self.log_path = root / "logs" / "monitor.log"
        self.state_path.parent.mkdir()
        self.log_path.parent.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def broker(self, **overrides):
        values = {
            "status": "PASS",
            "paper_mode": True,
            "account_status": "ACTIVE",
            "options_trading_level": 2,
            "options_buying_power": 500.0,
            "market_open": True,
            "open_orders": 0,
            "open_positions": 0,
            "reasons": [],
        }
        values.update(overrides)
        return BrokerReadinessResult(**values)

    def preflight(self, **overrides):
        values = {
            "broker_readiness": self.broker(),
            "execution_enabled": False,
            "state_path": self.state_path,
            "log_path": self.log_path,
            "paper_configuration": True,
        }
        values.update(overrides)
        return run_paper_trial_preflight(**values)

    def test_verified_paper_configuration_returns_ready(self):
        result = self.preflight(limits=TrialLimits())
        self.assertEqual(result.status, "READY")
        self.assertTrue(result.paper_mode_verified)
        self.assertTrue(result.account_ready)
        self.assertTrue(result.options_ready)
        self.assertEqual(result.trial_limits, TrialLimits())

    def test_missing_or_uncertain_paper_status_blocks(self):
        with patch(
            "paper_trial_preflight._safe_broker_readiness",
            side_effect=RuntimeError("paper status unavailable"),
        ):
            result = self.preflight(broker_readiness=None)
        self.assertEqual(result.status, "BLOCKED")
        self.assertFalse(result.paper_mode_verified)

        result = self.preflight(broker_readiness=self.broker(paper_mode=False))
        self.assertEqual(result.status, "BLOCKED")

    def test_missing_false_or_ambiguous_paper_configuration_blocks(self):
        for config in ({}, {"ALPACA_PAPER": "false"}, {"ALPACA_PAPER": "yes"}):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    resolve_paper_mode(config)

        with patch("paper_trial_preflight.load_paper_mode", side_effect=ValueError("missing")):
            result = self.preflight(paper_configuration=None)
        self.assertEqual(result.status, "BLOCKED")
        self.assertFalse(result.paper_configuration_verified)

    def test_explicit_true_requires_broker_paper_confirmation(self):
        result = self.preflight(
            paper_configuration=True,
            broker_readiness=self.broker(paper_mode=False),
        )
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(result.paper_configuration_verified)
        self.assertFalse(result.paper_mode_verified)

    def test_explicit_true_and_broker_true_confirm_paper_mode(self):
        result = self.preflight(
            paper_configuration=True,
            broker_readiness=self.broker(paper_mode=True),
        )
        self.assertTrue(result.paper_configuration_verified)
        self.assertTrue(result.paper_mode_verified)

    def test_trading_blocked_or_options_unavailable_blocks(self):
        blocked = self.preflight(
            broker_readiness=self.broker(
                reasons=["Account trading is blocked"],
            )
        )
        self.assertEqual(blocked.status, "BLOCKED")

        options = self.preflight(
            broker_readiness=self.broker(
                options_trading_level=None,
                options_buying_power=0.0,
                reasons=["Options unavailable"],
            )
        )
        self.assertEqual(options.status, "BLOCKED")

    def test_one_and_one_trial_limits_are_accepted(self):
        result = self.preflight(
            limits=TrialLimits(
                max_trades_per_day=1,
                max_open_positions=1,
            )
        )
        self.assertEqual(result.status, "READY")
        self.assertEqual(result.trial_limits.max_trades_per_day, 1)
        self.assertEqual(result.trial_limits.max_open_positions, 1)

    def test_invalid_limits_fail_closed(self):
        for config in (
            {"CTS_TRIAL_MAX_TRADES_PER_DAY": "0"},
            {"CTS_TRIAL_MAX_OPEN_POSITIONS": "-1"},
            {"CTS_TRIAL_MAX_TRADES_PER_DAY": "1.5"},
            {"CTS_TRIAL_MAX_OPEN_POSITIONS": "3"},
        ):
            with self.subTest(config=config):
                with patch("paper_trial_preflight.load_trial_limits", side_effect=ValueError("invalid")):
                    result = self.preflight()
                self.assertEqual(result.status, "BLOCKED")
                self.assertIsNone(result.trial_limits)

    def test_existing_defaults_remain_two_and_two(self):
        self.assertEqual(
            resolve_trial_limits({}),
            TrialLimits(DEFAULT_MAX_TRADES_PER_DAY, DEFAULT_MAX_OPEN_POSITIONS),
        )

    def test_locked_kill_switch_is_required(self):
        result = self.preflight(execution_enabled=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(result.execution_kill_switch_enabled)

    def test_no_order_or_execution_submission_is_called(self):
        with patch("paper_execution_service.submit_paper_entry") as submit, \
             patch("paper_trial_preflight._safe_broker_readiness", return_value=self.broker()), \
             patch("paper_trial_preflight.paper_execution_enabled", return_value=False):
            result = self.preflight(broker_readiness=None)

        self.assertEqual(result.status, "READY")
        submit.assert_not_called()

    def test_state_and_log_write_readiness_blocks_missing_directories(self):
        result = run_paper_trial_preflight(
            limits=TrialLimits(1, 1),
            broker_readiness=self.broker(),
            execution_enabled=False,
            state_path=Path(self.temp_dir.name) / "missing" / "state.json",
            log_path=self.log_path,
        )
        self.assertEqual(result.status, "BLOCKED")

    def autonomous(self, **overrides):
        values = {
            "config": {
                "ALPACA_PAPER": "true",
                "CTS_AUTONOMOUS_PAPER_ENABLED": "true",
                "CTS_PAPER_EXECUTION_ENABLED": "YES_PAPER_ONLY",
            },
            "limits": TrialLimits(1, 1),
            "broker_readiness": self.broker(),
            "state_path": self.state_path,
            "log_path": self.log_path,
        }
        values.update(overrides)
        return run_autonomous_paper_startup_preflight(**values)

    def test_autonomous_two_phase_preflight_is_startup_only(self):
        result = self.autonomous()
        self.assertEqual(result.status, "STARTUP_READY")
        self.assertFalse(result.entry_gate_open)
        self.assertFalse(result.submission_authorized)

    def test_autonomous_requires_exact_explicit_configuration(self):
        base = {
            "ALPACA_PAPER": "true",
            "CTS_AUTONOMOUS_PAPER_ENABLED": "true",
            "CTS_PAPER_EXECUTION_ENABLED": "YES_PAPER_ONLY",
        }
        for key, invalid in (
            ("ALPACA_PAPER", "True"),
            ("CTS_AUTONOMOUS_PAPER_ENABLED", True),
            ("CTS_PAPER_EXECUTION_ENABLED", "true"),
        ):
            with self.subTest(key=key, invalid=invalid):
                config = dict(base)
                config[key] = invalid
                result = self.autonomous(config=config)
                self.assertEqual(result.status, "BLOCKED")
                self.assertFalse(result.entry_gate_open)

    def test_autonomous_requires_one_and_one_and_broker_paper_readiness(self):
        self.assertEqual(
            self.autonomous(limits=TrialLimits(2, 1)).status, "BLOCKED"
        )
        self.assertEqual(
            self.autonomous(broker_readiness=self.broker(paper_mode=False)).status,
            "BLOCKED",
        )

    def test_autonomous_preflight_never_changes_environment_or_submits(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "paper_execution_service.submit_paper_entry"
        ) as submit:
            before = dict(__import__("os").environ)
            result = self.autonomous()
            self.assertEqual(dict(__import__("os").environ), before)
        self.assertEqual(result.status, "STARTUP_READY")
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
