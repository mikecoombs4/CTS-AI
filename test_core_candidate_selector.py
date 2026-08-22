import ast
import itertools
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core_candidate_selector import (
    CORE_ORIGIN,
    CandidateEvaluation,
    decision_identity,
    select_core_candidates,
    validate_completed_bar,
)
from scanner_service import ScannerResult


ET = ZoneInfo("America/New_York")
AS_OF_1000 = datetime(2026, 8, 24, 10, 0, tzinfo=ET)
BAR_0945 = datetime(2026, 8, 24, 9, 45, tzinfo=ET)


def scanner(ticker="AAPL", bar_timestamp=BAR_0945, volume_ratio=2.0, **changes):
    values = {
        "ticker": ticker,
        "bar_timestamp": bar_timestamp,
        "direction": "CALL",
        "last_price": 150.0,
        "ema_9": 151.0,
        "ema_20": 149.0,
        "box_high": 149.5,
        "box_low": 148.0,
        "box_width_percent": 1.0,
        "volume_ratio": volume_ratio,
        "trend_confirmed": True,
        "potter_box_found": True,
        "volume_confirmed": True,
        "breakout_confirmed": True,
        "bar_end_timestamp": bar_timestamp + timedelta(minutes=15) if isinstance(bar_timestamp, datetime) else None,
    }
    values.update(changes)
    return ScannerResult(**values)


def readiness(candidate, status="PASS", **changes):
    values = {
        "status": status,
        "allowed": status == "PASS",
        "submission_allowed": False,
        "scanner_candidate": candidate,
        "broker_readiness": SimpleNamespace(status="PASS", paper_mode=True),
        "option_liquidity": SimpleNamespace(acceptable=True),
        "trade_plan": SimpleNamespace(acceptable=True),
        "news_risk": SimpleNamespace(status="PASS"),
        "earnings_risk": SimpleNamespace(status="PASS"),
        "final_decision": SimpleNamespace(
            status="PASS", automatic_paper_eligible=True
        ),
        "daily_limits": SimpleNamespace(
            status="PASS", new_trade_allowed=True
        ),
        "order_preview": SimpleNamespace(eligible=True, ticker=candidate.ticker),
        "market_session": SimpleNamespace(status="PASS", entry_allowed=True),
        "state": SimpleNamespace(),
        "duplicate_contract": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def evaluation(ticker="AAPL", origin=CORE_ORIGIN, **scanner_changes):
    candidate = scanner(ticker=ticker, **scanner_changes)
    return CandidateEvaluation(origin, candidate, readiness(candidate))


class CompletedBarTests(unittest.TestCase):
    def test_exact_0945_accepts_0930_and_rejects_0945(self):
        as_of = datetime(2026, 8, 24, 9, 45, tzinfo=ET)
        completed = datetime(2026, 8, 24, 9, 30, tzinfo=ET)
        forming = datetime(2026, 8, 24, 9, 45, tzinfo=ET)
        self.assertEqual(validate_completed_bar(completed, as_of), ())
        self.assertTrue(validate_completed_bar(forming, as_of))

    def test_exact_1000_accepts_0945(self):
        self.assertEqual(validate_completed_bar(BAR_0945, AS_OF_1000), ())

    def test_forming_bar_is_rejected(self):
        forming = datetime(2026, 8, 24, 10, 0, tzinfo=ET)
        reasons = validate_completed_bar(forming, AS_OF_1000)
        self.assertTrue(any("forming" in reason for reason in reasons))

    def test_candidate_requires_exact_completed_bar_end_evidence(self):
        for bar_end in (None, BAR_0945, AS_OF_1000 + timedelta(minutes=15)):
            with self.subTest(bar_end=bar_end):
                result = select_core_candidates(
                    [evaluation(bar_end_timestamp=bar_end)], AS_OF_1000
                )
                self.assertIsNone(result.selected)
                self.assertIn("end evidence", " ".join(result.exclusions[0].reasons))

    def test_stale_future_naive_missing_and_malformed_are_rejected(self):
        cases = (
            datetime(2026, 8, 24, 9, 30, tzinfo=ET),
            datetime(2026, 8, 24, 10, 15, tzinfo=ET),
            datetime(2026, 8, 24, 9, 45),
            None,
            "2026-08-24T09:45:00-04:00",
        )
        for timestamp in cases:
            with self.subTest(timestamp=timestamp):
                self.assertTrue(validate_completed_bar(timestamp, AS_OF_1000))


class CandidateSelectionTests(unittest.TestCase):
    def test_block_review_needs_data_and_catalyst_are_excluded(self):
        evaluations = []
        for ticker, status in (
            ("BLOCK", "BLOCK"),
            ("REVIEW", "REVIEW"),
            ("NEEDS", "NEEDS_DATA"),
        ):
            candidate = scanner(ticker=ticker)
            evaluations.append(
                CandidateEvaluation(CORE_ORIGIN, candidate, readiness(candidate, status))
            )
        catalyst = evaluation("CAT")
        evaluations.append(
            CandidateEvaluation("CATALYST", catalyst.scanner_result, catalyst.readiness)
        )
        result = select_core_candidates(evaluations, AS_OF_1000)
        self.assertIsNone(result.selected)
        self.assertEqual(len(result.exclusions), 4)

    def test_all_eligible_candidates_are_ranked(self):
        result = select_core_candidates(
            [evaluation("MSFT", volume_ratio=2.2), evaluation("AAPL", volume_ratio=2.0)],
            AS_OF_1000,
        )
        self.assertEqual(len(result.ranked_eligible), 2)
        self.assertEqual(result.selected.ticker, "MSFT")

    def test_reversing_input_order_does_not_change_winner(self):
        candidates = [
            evaluation("AAPL", volume_ratio=2.1),
            evaluation("MSFT", volume_ratio=2.5),
            evaluation("NVDA", volume_ratio=2.3),
        ]
        forward = select_core_candidates(candidates, AS_OF_1000)
        reverse = select_core_candidates(reversed(candidates), AS_OF_1000)
        self.assertEqual(forward.selected.ticker, "MSFT")
        self.assertEqual(reverse.selected.ticker, "MSFT")
        self.assertEqual(
            [item.ticker for item in forward.ranked_eligible],
            [item.ticker for item in reverse.ranked_eligible],
        )

    def test_every_permutation_has_identical_ranking_and_winner(self):
        candidates = (
            evaluation("AAPL", volume_ratio=2.1),
            evaluation("MSFT", volume_ratio=2.5),
            evaluation("NVDA", volume_ratio=2.3),
        )
        rankings = {
            tuple(
                item.ticker
                for item in select_core_candidates(order, AS_OF_1000).ranked_eligible
            )
            for order in itertools.permutations(candidates)
        }
        self.assertEqual(rankings, {("MSFT", "NVDA", "AAPL")})

    def test_exact_ties_resolve_alphabetically(self):
        result = select_core_candidates(
            [evaluation("ZZZ", volume_ratio=2.0), evaluation("AAA", volume_ratio=2.0)],
            AS_OF_1000,
        )
        self.assertEqual([item.ticker for item in result.ranked_eligible], ["AAA", "ZZZ"])

    def test_no_eligible_candidate_returns_no_selection(self):
        candidate = scanner()
        result = select_core_candidates(
            [CandidateEvaluation(CORE_ORIGIN, candidate, readiness(candidate, "BLOCK"))],
            AS_OF_1000,
        )
        self.assertEqual(result.ranked_eligible, ())
        self.assertIsNone(result.selected)
        self.assertTrue(result.exclusions[0].reasons)

    def test_missing_downstream_data_fails_closed_with_reason(self):
        candidate = scanner()
        report = readiness(candidate, option_liquidity=None, news_risk=None)
        result = select_core_candidates(
            [CandidateEvaluation(CORE_ORIGIN, candidate, report)], AS_OF_1000
        )
        self.assertIsNone(result.selected)
        joined = " ".join(result.exclusions[0].reasons)
        self.assertIn("Option-liquidity", joined)
        self.assertIn("News-risk", joined)

    def test_every_missing_concrete_readiness_gate_fails_closed(self):
        candidate = scanner()
        gates = (
            "broker_readiness", "option_liquidity", "trade_plan", "news_risk",
            "earnings_risk", "final_decision", "daily_limits", "order_preview",
            "market_session", "state",
        )
        for gate in gates:
            with self.subTest(gate=gate):
                report = readiness(candidate, **{gate: None})
                result = select_core_candidates(
                    [CandidateEvaluation(CORE_ORIGIN, candidate, report)], AS_OF_1000
                )
                self.assertIsNone(result.selected)

    def test_submission_allowed_false_is_not_treated_as_approval(self):
        candidate = evaluation()
        self.assertFalse(candidate.readiness.submission_allowed)
        result = select_core_candidates([candidate], AS_OF_1000)
        self.assertEqual(result.selected.ticker, "AAPL")

    def test_duplicate_ticker_and_bar_fails_closed_independent_of_order(self):
        first = evaluation(" Aapl ", volume_ratio=2.0)
        second = evaluation("aAPL", volume_ratio=3.0)
        forward = select_core_candidates([first, second], AS_OF_1000)
        reverse = select_core_candidates([second, first], AS_OF_1000)
        self.assertIsNone(forward.selected)
        self.assertIsNone(reverse.selected)
        self.assertEqual(len(forward.ranked_eligible), 0)
        self.assertEqual(len(forward.exclusions), 2)
        self.assertTrue(all(
            "Duplicate" in " ".join(item.reasons) for item in forward.exclusions
        ))

    def test_invalid_numeric_ranking_data_is_explicitly_excluded(self):
        invalid_values = (None, "bad", math.nan, math.inf, -math.inf)
        for value in invalid_values:
            with self.subTest(score=value):
                candidate = scanner()
                candidate.score = lambda value=value: value
                result = select_core_candidates(
                    [CandidateEvaluation(CORE_ORIGIN, candidate, readiness(candidate))],
                    AS_OF_1000,
                )
                self.assertIsNone(result.selected)
                self.assertIn("technical score", " ".join(result.exclusions[0].reasons))
        for value in invalid_values:
            with self.subTest(volume_ratio=value):
                candidate = scanner(volume_ratio=value)
                result = select_core_candidates(
                    [CandidateEvaluation(CORE_ORIGIN, candidate, readiness(candidate))],
                    AS_OF_1000,
                )
                self.assertIsNone(result.selected)
                self.assertIn("volume ratio", " ".join(result.exclusions[0].reasons))

    def test_ticker_case_and_whitespace_share_identity_and_invalid_tickers_block(self):
        baseline = decision_identity("AAPL", BAR_0945)
        self.assertEqual(baseline, decision_identity(" aapl ", BAR_0945))
        for ticker in ("", "   ", "AAPL!", "ÅAPL", "TOO-LONG-123"):
            with self.subTest(ticker=ticker):
                candidate = evaluation(ticker)
                result = select_core_candidates([candidate], AS_OF_1000)
                self.assertIsNone(result.selected)
                with self.assertRaises(ValueError):
                    decision_identity(ticker, BAR_0945)

    def test_strict_session_rules_are_not_weakened(self):
        for as_of in (
            datetime(2026, 8, 24, 11, 30, tzinfo=ET),
            datetime(2026, 8, 24, 12, 0, tzinfo=ET),
            datetime(2026, 8, 24, 15, 30, tzinfo=ET),
        ):
            with self.subTest(as_of=as_of):
                latest = as_of.replace(
                    minute=(as_of.minute // 15) * 15, second=0, microsecond=0
                )
                candidate = evaluation(
                    bar_timestamp=latest - timedelta(minutes=15)
                )
                result = select_core_candidates([candidate], as_of)
                self.assertIsNone(result.selected)

    def test_decision_identity_is_deterministic_and_changes_by_ticker_or_bar(self):
        first = decision_identity("AAPL", BAR_0945)
        self.assertEqual(first, decision_identity("aapl", BAR_0945))
        next_bar = datetime(2026, 8, 24, 10, 0, tzinfo=ET)
        self.assertNotEqual(first, decision_identity("MSFT", BAR_0945))
        self.assertNotEqual(first, decision_identity("AAPL", next_bar))

    def test_utc_and_eastern_equivalents_share_interval_and_identity(self):
        utc_bar = BAR_0945.astimezone(timezone.utc)
        utc_as_of = AS_OF_1000.astimezone(timezone.utc)
        self.assertEqual(validate_completed_bar(BAR_0945, AS_OF_1000), ())
        self.assertEqual(validate_completed_bar(utc_bar, utc_as_of), ())
        self.assertEqual(
            decision_identity("AAPL", BAR_0945),
            decision_identity("AAPL", utc_bar),
        )

    def test_dst_aware_equivalent_timestamps_are_deterministic(self):
        winter_as_of = datetime(2026, 11, 2, 10, 0, tzinfo=ET)
        winter_bar = datetime(2026, 11, 2, 9, 45, tzinfo=ET)
        self.assertEqual(validate_completed_bar(winter_bar, winter_as_of), ())
        self.assertEqual(
            decision_identity("AAPL", winter_bar),
            decision_identity("AAPL", winter_bar.astimezone(timezone.utc)),
        )

    def test_naive_decision_timestamp_remains_blocked(self):
        with self.assertRaises(ValueError):
            decision_identity("AAPL", datetime(2026, 8, 24, 9, 45))

    def test_module_imports_no_brokerage_order_execution_or_network_path(self):
        tree = ast.parse(Path("core_candidate_selector.py").read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        forbidden = {
            "alpaca", "alpaca_service", "broker_readiness_service",
            "options_service", "paper_execution_service", "requests",
            "supervised_paper_entry_handoff", "catalyst_service",
            "catalyst_monitor",
        }
        self.assertFalse(
            any(name == item or name.startswith(item + ".") for name in imports for item in forbidden)
        )


if __name__ == "__main__":
    unittest.main()
