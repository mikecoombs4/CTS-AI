import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autonomous_paper_policy import evaluate_autonomous_paper_policy
from news_service import NewsHeadline, NewsRiskResult
from paper_trial_preflight import AutonomousPaperStartupPreflightResult, TrialLimits


NOW = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)


def startup(**changes):
    values = {
        "status": "STARTUP_READY",
        "paper_configuration_verified": True,
        "autonomous_configuration_verified": True,
        "execution_configuration_verified": True,
        "broker_ready": True,
        "trial_limits": TrialLimits(1, 1),
        "state_write_ready": True,
        "log_write_ready": True,
        "entry_gate_open": False,
        "submission_authorized": False,
        "reasons": ["startup only"],
        "broker_readiness": SimpleNamespace(paper_mode=True),
    }
    values.update(changes)
    return AutonomousPaperStartupPreflightResult(**values)


def ordinary_news(**changes):
    values = {
        "ticker": "AAPL",
        "status": "REVIEW",
        "headlines": [NewsHeadline(
            NOW - timedelta(minutes=10), "Wire", "AAPL opens a distribution center", [], []
        )],
        "blocking_matches": [],
        "catalyst_matches": [],
        "provider_query_succeeded": True,
        "queried_at": NOW,
    }
    values.update(changes)
    return NewsRiskResult(**values)


def readiness(**changes):
    values = {
        "status": "BLOCK", "allowed": False, "submission_allowed": False,
        "reasons": [
            "News risk gate did not return PASS for the requested ticker.",
            "News requires human or AI review",
            "Final CTS decision is REVIEW, not PASS",
        ],
        "scanner_candidate": SimpleNamespace(technical_candidate=lambda: True),
        "broker_readiness": SimpleNamespace(status="PASS", paper_mode=True),
        "option_liquidity": SimpleNamespace(acceptable=True),
        "trade_plan": SimpleNamespace(acceptable=True),
        "market_session": SimpleNamespace(status="PASS", entry_allowed=True),
        "news_risk": ordinary_news(),
        "earnings_risk": SimpleNamespace(status="PASS"),
        "daily_limits": SimpleNamespace(status="PASS", new_trade_allowed=True),
        "state": SimpleNamespace(), "duplicate_contract": False,
        "final_decision": SimpleNamespace(
            status="REVIEW", automatic_paper_eligible=False,
            reasons=["News requires human or AI review"],
        ),
        "order_preview": SimpleNamespace(
            eligible=False, side="BUY", order_type="LIMIT", time_in_force="DAY",
            quantity=1, limit_price=1.0, estimated_cost=100.0,
            reasons=["Final CTS decision is REVIEW, not PASS"],
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class AutonomousPaperPolicyTests(unittest.TestCase):
    def evaluate(self, report=None, **changes):
        values = {
            "readiness": report or readiness(), "origin": "CORE_CTS",
            "startup_preflight": startup(), "soft_news_configuration": "true",
            "preview_created_at": NOW - timedelta(seconds=30), "as_of": NOW,
        }
        values.update(changes)
        return evaluate_autonomous_paper_policy(**values)

    def test_only_ordinary_recent_review_becomes_distinct_soft_pass(self):
        result = self.evaluate()
        self.assertEqual(result.status, "PAPER_SOFT_PASS")
        self.assertTrue(result.allowed)
        self.assertFalse(result.live_execution_eligible)
        self.assertEqual(result.softened_gate, "news_risk")
        self.assertEqual(result.audit_headlines, ("AAPL opens a distribution center",))

    def test_soft_pass_never_mutates_underlying_core_decision(self):
        report = readiness()
        decision = report.final_decision
        self.assertTrue(self.evaluate(report).allowed)
        self.assertEqual(decision.status, "REVIEW")
        self.assertFalse(decision.automatic_paper_eligible)

    def test_missing_failed_stale_adverse_and_catalyst_news_block(self):
        cases = (
            None,
            ordinary_news(provider_query_succeeded=False),
            ordinary_news(queried_at=NOW - timedelta(minutes=10)),
            ordinary_news(status="BLOCK", blocking_matches=["fraud investigation"]),
            ordinary_news(catalyst_matches=["upgrade"]),
            ordinary_news(headlines=[NewsHeadline(None, "Wire", "ordinary", [], [])]),
            ordinary_news(headlines=[NewsHeadline(
                NOW, "Wire", "concealed adverse", ["fraud investigation"], []
            )]),
        )
        for news in cases:
            with self.subTest(news=news):
                self.assertFalse(self.evaluate(readiness(news_risk=news)).allowed)

    def test_earnings_or_second_failed_gate_is_never_softened(self):
        cases = (
            {"earnings_risk": SimpleNamespace(status="REVIEW")},
            {"option_liquidity": SimpleNamespace(acceptable=False)},
            {"trade_plan": SimpleNamespace(acceptable=False)},
            {"market_session": SimpleNamespace(status="BLOCK", entry_allowed=False)},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(self.evaluate(readiness(**changes)).allowed)
        self.assertFalse(self.evaluate(readiness(
            reasons=[
                "News risk gate did not return PASS for the requested ticker.",
                "News requires human or AI review",
                "Final CTS decision is REVIEW, not PASS",
                "Another unresolved gate",
            ]
        )).allowed)

    def test_non_core_or_unverified_paper_configuration_blocks(self):
        self.assertFalse(self.evaluate(origin="CATALYST").allowed)
        self.assertFalse(self.evaluate(soft_news_configuration="True").allowed)
        self.assertFalse(self.evaluate(
            startup_preflight=startup(paper_configuration_verified=False)
        ).allowed)

    def test_malformed_or_stale_preview_blocks(self):
        self.assertFalse(self.evaluate(preview_created_at=NOW - timedelta(minutes=6)).allowed)
        self.assertFalse(self.evaluate(readiness(order_preview=None)).allowed)

    def test_proven_successful_empty_news_can_hard_pass_paper_policy(self):
        news = ordinary_news(status="PASS", headlines=[])
        report = readiness(
            status="PASS", allowed=True, news_risk=news,
            final_decision=SimpleNamespace(
                status="PASS", automatic_paper_eligible=True, reasons=["all passed"]
            ),
            order_preview=SimpleNamespace(
                eligible=True, side="BUY", order_type="LIMIT", time_in_force="DAY",
                quantity=1, limit_price=1.0, estimated_cost=100.0,
                reasons=["All preview-only safety checks passed"],
            ),
        )
        result = self.evaluate(report)
        self.assertEqual(result.status, "PAPER_POLICY_PASS")
        self.assertTrue(result.allowed)

    def test_empty_news_without_provider_proof_fails_closed(self):
        report = readiness(news_risk=ordinary_news(
            status="PASS", headlines=[], provider_query_succeeded=False
        ))
        self.assertFalse(self.evaluate(report).allowed)


if __name__ == "__main__":
    unittest.main()
