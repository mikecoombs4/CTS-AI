import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_autonomous_paper


class AutonomousPaperCliTests(unittest.TestCase):
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
                    status="CHECK_OK", entry_gate_open=True
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
        runner.run.return_value = SimpleNamespace(status="COMPLETED", entry_gate_open=False)
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


if __name__ == "__main__":
    unittest.main()
