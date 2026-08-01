import unittest

from order_preview_service import build_paper_order_preview
from paper_execution_service import (
    build_paper_entry_payload,
    validate_submission_authorization,
)


def eligible_preview():
    return build_paper_order_preview(
        ticker="TEST",
        contract_symbol="TEST260807C00100000",
        final_decision_status="PASS",
        limit_price=0.35,
        daily_limits_passed=True,
        market_session_passed=True,
    )


class PaperExecutionTests(unittest.TestCase):
    def test_payload_is_paper_options_limit_order(self) -> None:
        payload = build_paper_entry_payload(
            eligible_preview(),
            "cts-test-001",
        )

        self.assertEqual(payload.order_type, "limit")
        self.assertEqual(payload.time_in_force, "day")
        self.assertEqual(payload.position_intent, "buy_to_open")
        self.assertFalse(payload.extended_hours)

    def test_locked_kill_switch_refuses_submission(self) -> None:
        failures = validate_submission_authorization(
            preview=eligible_preview(),
            execution_enabled=False,
            duplicate_open_order=False,
        )

        self.assertIn(
            "Paper execution kill switch is locked",
            failures,
        )

    def test_duplicate_open_order_is_refused(self) -> None:
        failures = validate_submission_authorization(
            preview=eligible_preview(),
            execution_enabled=True,
            duplicate_open_order=True,
        )

        self.assertIn(
            "An open order already exists for this contract",
            failures,
        )

    def test_ineligible_preview_is_refused(self) -> None:
        preview = build_paper_order_preview(
            ticker="TEST",
            contract_symbol="TEST260807C00100000",
            final_decision_status="REVIEW",
            limit_price=0.35,
            daily_limits_passed=True,
            market_session_passed=True,
        )
        failures = validate_submission_authorization(
            preview=preview,
            execution_enabled=True,
            duplicate_open_order=False,
        )

        self.assertIn("Paper-order preview is not eligible", failures)


if __name__ == "__main__":
    unittest.main()
