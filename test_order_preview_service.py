import unittest

from order_preview_service import build_paper_order_preview


class PaperOrderPreviewTests(unittest.TestCase):
    def build(self, **overrides):
        inputs = {
            "ticker": "TEST",
            "contract_symbol": "TEST260807C00100000",
            "final_decision_status": "PASS",
            "limit_price": 0.35,
            "daily_limits_passed": True,
            "market_session_passed": True,
        }
        inputs.update(overrides)
        return build_paper_order_preview(**inputs)

    def test_every_gate_pass_creates_eligible_preview(self) -> None:
        preview = self.build()

        self.assertTrue(preview.eligible)
        self.assertEqual(preview.order_type, "LIMIT")
        self.assertEqual(preview.quantity, 1)

    def test_review_decision_refuses_preview(self) -> None:
        preview = self.build(final_decision_status="REVIEW")

        self.assertFalse(preview.eligible)

    def test_block_decision_refuses_preview(self) -> None:
        preview = self.build(final_decision_status="BLOCK")

        self.assertFalse(preview.eligible)

    def test_contract_over_cap_refuses_preview(self) -> None:
        preview = self.build(limit_price=1.51)

        self.assertFalse(preview.eligible)

    def test_daily_limit_block_refuses_preview(self) -> None:
        preview = self.build(daily_limits_passed=False)

        self.assertFalse(preview.eligible)

    def test_closed_market_window_refuses_preview(self) -> None:
        preview = self.build(market_session_passed=False)

        self.assertFalse(preview.eligible)


if __name__ == "__main__":
    unittest.main()
