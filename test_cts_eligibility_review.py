import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cts_eligibility_review import review_candidate

NOW = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)


def candidate(score=4):
    return {
        "ticker": "AAPL",
        "catalyst_fingerprint": "id:catalyst-1",
        "candidate_status": "PAPER_ONLY_CANDIDATE",
        "paper_only": True,
        "catalyst_headline": {
            "headline": "AAPL wins contract award",
            "source": "Wire",
        },
        "technical_confirmation": {
            "direction": "CALL",
            "score": score,
            "bar_timestamp": NOW.isoformat(),
            "last_price": 100.0,
            "ema_9": 101.0,
            "ema_20": 99.0,
            "box_high": 99.0,
            "box_low": 97.0,
            "volume_ratio": 2.0,
        },
    }


class EligibilityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "state.json"
        self.state_file.write_text(json.dumps({
            "version": 3,
            "paper_candidates": {"AAPL:id:catalyst-1": candidate()},
        }))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_candidate_is_reviewed_but_needs_trade_data(self):
        review = review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)

        self.assertEqual(review.overall_result, "NEEDS_DATA")
        self.assertTrue(review.paper_only)
        self.assertEqual(review.gates["technical_confirmation"]["status"], "PASS")
        self.assertEqual(review.gates["options_contract"]["status"], "NEEDS_DATA")

    def test_nonpaper_or_malformed_candidate_is_blocked(self):
        state = json.loads(self.state_file.read_text())
        state["paper_candidates"]["BAD"] = {
            "ticker": "BAD",
            "candidate_status": "PAPER_ONLY_CANDIDATE",
            "paper_only": False,
        }
        self.state_file.write_text(json.dumps(state))

        review = review_candidate("BAD", self.state_file, NOW)
        self.assertEqual(review.overall_result, "BLOCKED")
        self.assertEqual(review.gates["candidate_validation"]["status"], "FAIL")

    def test_failed_technical_gate_blocks_with_reason(self):
        state = json.loads(self.state_file.read_text())
        state["paper_candidates"]["AAPL:id:catalyst-1"] = candidate(score=3)
        self.state_file.write_text(json.dumps(state))

        review = review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)
        self.assertEqual(review.overall_result, "BLOCKED")
        self.assertTrue(any("Technical confirmation" in reason for reason in review.reasons))

    def test_review_is_idempotent_for_unchanged_candidate(self):
        first = review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)
        second = review_candidate(
            "AAPL:id:catalyst-1", self.state_file,
            datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
        )
        state = json.loads(self.state_file.read_text())
        self.assertEqual(first.evaluation_time, second.evaluation_time)
        self.assertEqual(len(state["eligibility_reviews"]), 1)

    def test_review_survives_reload(self):
        review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(
            state["eligibility_reviews"]["AAPL:id:catalyst-1"]["overall_result"],
            "NEEDS_DATA",
        )
        self.assertTrue(state["eligibility_reviews"]["AAPL:id:catalyst-1"]["paper_only"])

    def test_dynamic_session_gate_recomputes_and_updates_one_review(self):
        blocked_session = type(
            "Session", (), {"entry_allowed": False, "reason": "Lunch window"}
        )()
        open_session = type(
            "Session", (), {"entry_allowed": True, "reason": "Entry window open"}
        )()
        with patch(
            "cts_eligibility_review.evaluate_market_session",
            side_effect=[blocked_session, open_session],
        ) as session:
            first = review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)
            second = review_candidate(
                "AAPL:id:catalyst-1",
                self.state_file,
                datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(first.overall_result, "BLOCKED")
        self.assertEqual(second.overall_result, "NEEDS_DATA")
        self.assertEqual(session.call_count, 2)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(len(state["eligibility_reviews"]), 1)
        self.assertEqual(
            state["eligibility_reviews"]["AAPL:id:catalyst-1"]["overall_result"],
            "NEEDS_DATA",
        )

    def test_review_write_preserves_unrelated_monitor_state(self):
        state = json.loads(self.state_file.read_text())
        state["seen_articles"] = {"id:news-1": {"ticker": "MSFT"}}
        state["pending_technical"] = {"MSFT": {"status": "PENDING"}}
        state["unrelated_monitor_field"] = {"keep": True}
        self.state_file.write_text(json.dumps(state))

        review_candidate("AAPL:id:catalyst-1", self.state_file, NOW)
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved["seen_articles"], state["seen_articles"])
        self.assertEqual(saved["pending_technical"], state["pending_technical"])
        self.assertEqual(saved["unrelated_monitor_field"], {"keep": True})

    def test_missing_candidate_is_blocked_without_brokerage_calls(self):
        with patch("alpaca_service.get_alpaca_credentials") as credentials, \
             patch("paper_execution_service.submit_paper_entry") as submit:
            review = review_candidate("MISSING", self.state_file, NOW)

        self.assertEqual(review.overall_result, "BLOCKED")
        credentials.assert_not_called()
        submit.assert_not_called()

    def test_review_service_has_no_protected_imports(self):
        source = Path("cts_eligibility_review.py").read_text()
        for name in (
            "alpaca_service",
            "options_service",
            "paper_entry_service",
            "paper_execution_service",
            "order_preview_service",
            "risk_service",
            "decision_service",
        ):
            self.assertNotIn(f"import {name}", source)


if __name__ == "__main__":
    unittest.main()
