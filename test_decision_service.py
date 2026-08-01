import unittest

from decision_service import evaluate_final_decision


class FinalDecisionTests(unittest.TestCase):
    def evaluate(
        self,
        **overrides,
    ):
        inputs = {
            "ticker": "TEST",
            "technical_passed": True,
            "options_passed": True,
            "risk_plan_passed": True,
            "news_status": "PASS",
            "earnings_status": "PASS",
        }
        inputs.update(overrides)
        return evaluate_final_decision(**inputs)

    def test_every_gate_passes(self) -> None:
        decision = self.evaluate()

        self.assertEqual(decision.status, "PASS")
        self.assertTrue(decision.automatic_paper_eligible)

    def test_news_review_requires_review(self) -> None:
        decision = self.evaluate(news_status="REVIEW")

        self.assertEqual(decision.status, "REVIEW")
        self.assertFalse(decision.automatic_paper_eligible)

    def test_earnings_review_requires_review(self) -> None:
        decision = self.evaluate(earnings_status="REVIEW")

        self.assertEqual(decision.status, "REVIEW")

    def test_blocking_news_blocks_candidate(self) -> None:
        decision = self.evaluate(news_status="BLOCK")

        self.assertEqual(decision.status, "BLOCK")

    def test_failed_technical_setup_blocks_candidate(self) -> None:
        decision = self.evaluate(technical_passed=False)

        self.assertEqual(decision.status, "BLOCK")

    def test_failed_risk_plan_blocks_candidate(self) -> None:
        decision = self.evaluate(risk_plan_passed=False)

        self.assertEqual(decision.status, "BLOCK")

    def test_missing_provider_data_fails_closed(self) -> None:
        decision = self.evaluate(
            news_status=None,
            earnings_status=None,
        )

        self.assertEqual(decision.status, "BLOCK")
        self.assertFalse(decision.automatic_paper_eligible)

    def test_closed_entry_window_blocks_candidate(self) -> None:
        decision = self.evaluate(market_session_passed=False)

        self.assertEqual(decision.status, "BLOCK")


if __name__ == "__main__":
    unittest.main()
