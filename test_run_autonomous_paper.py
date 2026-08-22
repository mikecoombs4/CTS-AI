import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_autonomous_paper


class AutonomousPaperCliTests(unittest.TestCase):
    def test_repo_local_environment_is_loaded_before_runner_build(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "ALPACA_PAPER=true\nCTS_AUTONOMOUS_PAPER_ENABLED=true\n",
                encoding="utf-8",
            )
            runner = Mock()
            runner.run.return_value = SimpleNamespace(
                status="CHECK_OK", entry_gate_open=False, reasons=(),
            )

            def build_after_load():
                self.assertEqual(os.environ.get("ALPACA_PAPER"), "true")
                self.assertEqual(os.environ.get("CTS_AUTONOMOUS_PAPER_ENABLED"), "true")
                return runner

            with patch.dict(os.environ, {}, clear=True), patch.object(
                run_autonomous_paper, "ENV_FILE", env_file
            ), patch(
                "run_autonomous_paper.build_runner", side_effect=build_after_load
            ), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(run_autonomous_paper.main(["--check"]), 0)

    def test_different_working_directory_uses_script_adjacent_path(self):
        with tempfile.TemporaryDirectory() as env_directory, tempfile.TemporaryDirectory() as cwd:
            env_file = Path(env_directory) / ".env"
            env_file.write_text("ALPACA_PAPER=true\n", encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    run_autonomous_paper, "ENV_FILE", env_file
                ):
                    self.assertTrue(run_autonomous_paper.load_repository_environment())
                    self.assertEqual(os.environ.get("ALPACA_PAPER"), "true")
            finally:
                os.chdir(previous)

    def test_process_environment_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("ALPACA_PAPER=false\n", encoding="utf-8")
            with patch.dict(os.environ, {"ALPACA_PAPER": "true"}, clear=True), patch.object(
                run_autonomous_paper, "ENV_FILE", env_file
            ):
                run_autonomous_paper.load_repository_environment()
                self.assertEqual(os.environ["ALPACA_PAPER"], "true")

    def test_missing_environment_file_fails_normal_validation_without_broker_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.env"
            state_root = Path(directory) / "state"
            with patch.dict(os.environ, {}, clear=True), patch.object(
                run_autonomous_paper, "ENV_FILE", missing
            ), patch(
                "run_autonomous_paper._state_root", return_value=state_root
            ), patch(
                "run_autonomous_paper.evaluate_broker_readiness"
            ) as broker, patch(
                "run_autonomous_paper.fetch_scanner_results"
            ) as scanner, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(run_autonomous_paper.main(["--check"]), 2)
            broker.assert_not_called()
            scanner.assert_not_called()
            self.assertIn("Explicit Alpaca/autonomous paper configuration is missing.", output.getvalue())

    def test_exactly_one_mode_is_required(self):
        for argv in ([], ["--check", "--dry-run"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                run_autonomous_paper.parser().parse_args(argv)

    def test_all_three_modes_map_without_live_mode(self):
        for flag, mode in (
            ("--check", "check"), ("--dry-run", "dry-run"),
            ("--execute-paper", "execute-paper"),
        ):
            with self.subTest(flag=flag):
                runner = Mock()
                runner.run.return_value = SimpleNamespace(
                    status="CHECK_OK", entry_gate_open=True, reasons=()
                )
                with patch("run_autonomous_paper.build_runner", return_value=runner), patch(
                    "sys.stdout", new_callable=io.StringIO
                ) as output:
                    self.assertEqual(run_autonomous_paper.main([flag]), 0)
                runner.run.assert_called_once_with(mode)
                self.assertNotIn("credential", output.getvalue().lower())
        help_text = run_autonomous_paper.parser().format_help().lower()
        self.assertNotIn("live", help_text)

    def test_dry_run_output_is_explicit_and_execute_warns_paper_only(self):
        runner = Mock()
        runner.run.return_value = SimpleNamespace(
            status="COMPLETED", entry_gate_open=False, reasons=()
        )
        for flag, expected in (("--dry-run", "DRY_RUN_ONLY"), ("--execute-paper", "PAPER ONLY")):
            with patch("run_autonomous_paper.build_runner", return_value=runner), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                run_autonomous_paper.main([flag])
            self.assertIn(expected, output.getvalue())

    def test_blocked_and_keyboard_interrupt_are_sanitized(self):
        runner = Mock()
        runner.run.side_effect = RuntimeError("secret-account-123")
        with patch("run_autonomous_paper.build_runner", return_value=runner), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(run_autonomous_paper.main(["--check"]), 2)
        self.assertNotIn("secret-account-123", output.getvalue())
        runner.run.side_effect = KeyboardInterrupt()
        with patch("run_autonomous_paper.build_runner", return_value=runner), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output:
            self.assertEqual(run_autonomous_paper.main(["--dry-run"]), 130)
        self.assertIn("lock released", output.getvalue())

    def test_all_modes_print_sanitized_result_reasons_without_secrets(self):
        secret = "test-secret-credential-value"
        for flag in ("--check", "--dry-run", "--execute-paper"):
            runner = Mock()
            runner.run.return_value = SimpleNamespace(
                status="BLOCKED", entry_gate_open=False,
                reasons=(
                    "Managed paper state verification is stale.",
                    f"provider rejected {secret}",
                    "broker account ID: PAPER-12345 unavailable",
                ),
            )
            with self.subTest(flag=flag), patch.dict(
                os.environ, {"ALPACA_SECRET_KEY": secret}, clear=True
            ), patch(
                "run_autonomous_paper.build_runner", return_value=runner
            ), patch("run_autonomous_paper.load_repository_environment"), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                self.assertEqual(run_autonomous_paper.main([flag]), 2)
            text = output.getvalue()
            self.assertIn("REASON: Managed paper state verification is stale.", text)
            self.assertIn("[REDACTED]", text)
            self.assertNotIn(secret, text)
            self.assertNotIn("PAPER-12345", text)


if __name__ == "__main__":
    unittest.main()
