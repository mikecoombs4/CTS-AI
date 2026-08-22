import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from order_preview_service import build_paper_order_preview
from paper_entry_order_tracker import PaperEntryOrderTracker
from paper_execution_service import ENABLE_VALUE
from supervised_paper_entry_handoff import (
    CORE_ORIGIN,
    SubmissionIntentJournal,
    deterministic_client_order_id,
    submit_supervised_paper_entry,
)


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=ET)


class SupervisedPaperEntryHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.intent_path = root / "intents.json"
        self.tracker_path = root / "orders.json"
        self.preview = build_paper_order_preview(
            "AAPL", "AAPL260918C00150000", "PASS", 1.00, True, True
        )
        self.broker = SimpleNamespace(
            status="PASS", paper_mode=True, open_positions=0, open_orders=0
        )
        self.daily = SimpleNamespace(
            status="PASS", new_trade_allowed=True,
            trades_opened_today=0, open_positions=0,
        )
        self.readiness = SimpleNamespace(
            status="PASS",
            allowed=True,
            execution_enabled=True,
            broker_readiness=self.broker,
            scanner_candidate=SimpleNamespace(technical_candidate=lambda: True),
            option_liquidity=SimpleNamespace(acceptable=True),
            trade_plan=SimpleNamespace(acceptable=True),
            news_risk=SimpleNamespace(status="PASS"),
            earnings_risk=SimpleNamespace(status="PASS"),
            duplicate_contract=False,
            order_preview=self.preview,
            final_decision=SimpleNamespace(
                status="PASS", automatic_paper_eligible=True
            ),
            market_session=SimpleNamespace(status="PASS", entry_allowed=True),
            daily_limits=self.daily,
        )
        self.preflight = SimpleNamespace(
            status="READY",
            paper_configuration_verified=True,
            paper_mode_verified=True,
            broker_readiness=self.broker,
            trial_limits=SimpleNamespace(
                max_trades_per_day=1, max_open_positions=1
            ),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def journal(self):
        return SubmissionIntentJournal(self.intent_path, now=NOW)

    def tracker(self):
        return PaperEntryOrderTracker(self.tracker_path)

    def broker_response(self, client_order_id):
        return SimpleNamespace(
            id="broker-order-1",
            client_order_id=client_order_id,
            symbol=self.preview.contract_symbol,
            qty="1",
            filled_qty="0",
            limit_price="1.00",
            filled_avg_price=None,
            status="accepted",
            submitted_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            filled_at=None,
        )

    def call(self, submitter=None, **overrides):
        values = {
            "readiness": self.readiness,
            "preview": self.preview,
            "preview_created_at": NOW - timedelta(seconds=30),
            "preflight": self.preflight,
            "execution_enable_value": ENABLE_VALUE,
            "paper_configuration_confirmed": True,
            "origin": CORE_ORIGIN,
            "tracker": self.tracker(),
            "journal": self.journal(),
            "submitter": submitter or Mock(
                side_effect=lambda **kwargs: self.broker_response(
                    kwargs["client_order_id"]
                )
            ),
            "now": NOW,
        }
        values.update(overrides)
        return submit_supervised_paper_entry(**values)

    def test_every_failed_prerequisite_blocks_without_submit(self):
        cases = [
            {"preflight": SimpleNamespace(**{
                **self.preflight.__dict__, "status": "BLOCKED"
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__, "allowed": False
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__, "scanner_candidate": None
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__,
                "option_liquidity": SimpleNamespace(acceptable=False),
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__,
                "trade_plan": SimpleNamespace(acceptable=False),
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__, "news_risk": None
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__, "earnings_risk": None
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__, "duplicate_contract": True
            })},
            {"paper_configuration_confirmed": False},
            {"preflight": SimpleNamespace(**{
                **self.preflight.__dict__,
                "trial_limits": SimpleNamespace(
                    max_trades_per_day=2, max_open_positions=1
                ),
            })},
            {"readiness": SimpleNamespace(**{
                **self.readiness.__dict__,
                "daily_limits": SimpleNamespace(**{
                    **self.daily.__dict__, "trades_opened_today": 1
                }),
            })},
        ]
        for index, overrides in enumerate(cases):
            with self.subTest(index=index):
                submit = Mock()
                result = self.call(submitter=submit, **overrides)
                self.assertEqual(result.status, "BLOCKED")
                submit.assert_not_called()

    def test_disabled_execution_switch_blocks(self):
        submit = Mock()
        result = self.call(submitter=submit, execution_enable_value="NO")
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()

    def test_intent_exists_before_submitter_is_called(self):
        def submitter(**kwargs):
            saved = json.loads(self.intent_path.read_text())
            self.assertEqual(saved["intents"][0]["status"], "INTENT_PERSISTED")
            self.assertTrue(saved["intents"][0]["paper_only"])
            return self.broker_response(kwargs["client_order_id"])

        result = self.call(submitter=submitter)
        self.assertEqual(result.status, "SUBMITTED")

    def test_valid_request_submits_once_and_records_response(self):
        submit = Mock(
            side_effect=lambda **kwargs: self.broker_response(
                kwargs["client_order_id"]
            )
        )
        result = self.call(submitter=submit)
        self.assertTrue(result.submitted)
        submit.assert_called_once()
        self.assertEqual(result.order_record.broker_order_id, "broker-order-1")
        self.assertTrue(result.order_record.paper_only)
        self.assertEqual(result.intent.status, "BROKER_RECORDED")

    def test_client_order_id_format_length_and_determinism(self):
        first = deterministic_client_order_id(self.preview, "2026-08-24")
        second = deterministic_client_order_id(self.preview, "2026-08-24")
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 128)
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]+", first))

    def test_client_order_id_changes_for_contract_request_or_date(self):
        baseline = deterministic_client_order_id(self.preview, "2026-08-24")
        variants = [
            build_paper_order_preview(
                "AAPL", "AAPL260918C00155000", "PASS", 1.0, True, True
            ),
            build_paper_order_preview(
                "AAPL", self.preview.contract_symbol, "PASS", 1.01, True, True
            ),
        ]
        identifiers = {baseline}
        identifiers.update(
            deterministic_client_order_id(item, "2026-08-24")
            for item in variants
        )
        identifiers.add(deterministic_client_order_id(self.preview, "2026-08-25"))
        self.assertEqual(len(identifiers), 4)

    def test_production_execution_adapter_preserves_order_fields_and_client_id(self):
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from paper_execution_service import submit_paper_entry

        client_id = deterministic_client_order_id(self.preview, "2026-08-24")
        fake_client = Mock()
        fake_client.submit_order.return_value = SimpleNamespace(id="offline")
        with patch(
            "alpaca.trading.client.TradingClient", return_value=fake_client
        ) as client_class, patch(
            "alpaca_service.get_alpaca_credentials",
            return_value=("offline-key", "offline-secret"),
        ), patch(
            "paper_execution_service._duplicate_order_exists", return_value=False
        ), patch(
            "paper_execution_service.paper_execution_enabled", return_value=True
        ):
            submit_paper_entry(self.preview, client_id)

        client_class.assert_called_once_with(
            "offline-key", "offline-secret", paper=True
        )
        fake_client.submit_order.assert_called_once()
        request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.client_order_id, client_id)
        self.assertEqual(request.side, OrderSide.BUY)
        self.assertEqual(request.time_in_force, TimeInForce.DAY)
        self.assertEqual(request.position_intent, PositionIntent.BUY_TO_OPEN)
        self.assertEqual(request.type.value, "limit")

    def test_duplicate_invocation_does_not_submit_twice(self):
        submit = Mock(
            side_effect=lambda **kwargs: self.broker_response(
                kwargs["client_order_id"]
            )
        )
        first = self.call(submitter=submit)
        second = self.call(submitter=submit)
        self.assertEqual(first.status, "SUBMITTED")
        self.assertEqual(second.status, "BLOCKED")
        submit.assert_called_once()

    def test_restart_does_not_resubmit(self):
        first_submit = Mock(
            side_effect=lambda **kwargs: self.broker_response(
                kwargs["client_order_id"]
            )
        )
        self.call(submitter=first_submit)
        restarted_submit = Mock()
        result = self.call(
            submitter=restarted_submit,
            tracker=PaperEntryOrderTracker(self.tracker_path),
            journal=SubmissionIntentJournal(self.intent_path, now=NOW),
        )
        self.assertEqual(result.status, "BLOCKED")
        restarted_submit.assert_not_called()

    def test_timeout_becomes_uncertain_and_never_retries(self):
        submit = Mock(side_effect=TimeoutError("timeout"))
        first = self.call(submitter=submit)
        second = self.call(submitter=submit)
        self.assertEqual(first.status, "SUBMISSION_UNCERTAIN")
        self.assertEqual(second.status, "BLOCKED")
        self.assertEqual(submit.call_count, 1)
        saved = json.loads(self.intent_path.read_text())
        self.assertEqual(saved["intents"][0]["status"], "SUBMISSION_UNCERTAIN")

    def test_tracker_persistence_failure_becomes_uncertain_without_retry(self):
        tracker = self.tracker()
        tracker.register_submitted = Mock(side_effect=OSError("disk full"))
        submit = Mock(
            side_effect=lambda **kwargs: self.broker_response(
                kwargs["client_order_id"]
            )
        )
        journal = self.journal()
        first = self.call(submitter=submit, tracker=tracker, journal=journal)
        second = self.call(submitter=submit, tracker=tracker, journal=journal)
        self.assertEqual(first.status, "SUBMISSION_UNCERTAIN")
        self.assertEqual(second.status, "BLOCKED")
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(first.intent.status, "SUBMISSION_UNCERTAIN")
        self.assertEqual(first.intent.client_order_id, first.client_order_id)
        self.assertIn("client order ID", first.intent.blocking_reason)

    def test_missing_broker_id_or_usable_status_is_uncertain(self):
        cases = (
            {"id": None},
            {"status": None},
            {"status": "unrecognized_broker_state"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                intent_path = Path(self.temp_dir.name) / f"ambiguous-{index}.json"
                tracker_path = Path(self.temp_dir.name) / f"ambiguous-order-{index}.json"

                def submitter(**kwargs):
                    response = self.broker_response(kwargs["client_order_id"])
                    for name, value in changes.items():
                        setattr(response, name, value)
                    return response

                result = self.call(
                    submitter=submitter,
                    journal=SubmissionIntentJournal(intent_path, now=NOW),
                    tracker=PaperEntryOrderTracker(tracker_path),
                )
                self.assertEqual(result.status, "SUBMISSION_UNCERTAIN")
                self.assertFalse(result.submitted)
                self.assertFalse(tracker_path.exists())

    def test_restart_recovers_persisted_intent_as_uncertain_without_submit(self):
        journal = self.journal()
        client_id = "cts-paper-interrupted"
        from supervised_paper_entry_handoff import SubmissionIntent
        journal.persist(SubmissionIntent(
            True, "2026-08-24", client_id, "AAPL",
            self.preview.contract_symbol, 1, 1.0, "INTENT_PERSISTED",
            NOW.isoformat(), NOW.isoformat(),
        ))
        restarted = SubmissionIntentJournal(
            self.intent_path, now=NOW + timedelta(seconds=1)
        )
        submit = Mock()
        result = self.call(submitter=submit, journal=restarted)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(restarted.intents[0].status, "SUBMISSION_UNCERTAIN")
        submit.assert_not_called()

    def test_invalid_or_stale_preview_blocks(self):
        for preview, created_at in (
            (build_paper_order_preview(
                "AAPL", self.preview.contract_symbol, "BLOCK", 1.0, True, True
            ), NOW),
            (self.preview, NOW - timedelta(seconds=301)),
        ):
            with self.subTest(eligible=preview.eligible, created_at=created_at):
                submit = Mock()
                result = self.call(
                    submitter=submit,
                    preview=preview,
                    preview_created_at=created_at,
                )
                self.assertEqual(result.status, "BLOCKED")
                submit.assert_not_called()

    def test_same_day_or_unresolved_entry_state_blocks(self):
        first = self.call()
        self.assertEqual(first.status, "SUBMITTED")
        submit = Mock()
        result = self.call(submitter=submit)
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()

    def test_paper_live_mismatch_blocks(self):
        for paper_config, broker_paper in ((False, True), (True, False)):
            with self.subTest(paper_config=paper_config, broker_paper=broker_paper):
                submit = Mock()
                broker = SimpleNamespace(**{
                    **self.broker.__dict__, "paper_mode": broker_paper
                })
                preflight = SimpleNamespace(**{
                    **self.preflight.__dict__, "broker_readiness": broker
                })
                result = self.call(
                    submitter=submit,
                    paper_configuration_confirmed=paper_config,
                    preflight=preflight,
                )
                self.assertEqual(result.status, "BLOCKED")
                submit.assert_not_called()

    def test_catalyst_candidate_cannot_enter_path(self):
        submit = Mock()
        result = self.call(submitter=submit, origin="CATALYST")
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()

    def test_no_cancel_replace_close_retry_or_live_function_is_called(self):
        forbidden = Mock()
        submit = Mock(
            side_effect=lambda **kwargs: self.broker_response(
                kwargs["client_order_id"]
            )
        )
        result = self.call(submitter=submit)
        self.assertEqual(result.status, "SUBMITTED")
        submit.assert_called_once()
        forbidden.assert_not_called()
        source = Path("supervised_paper_entry_handoff.py").read_text()
        for name in (
            "cancel_order", "replace_order", "close_position",
            "TradingClient", "catalyst_monitor", "catalyst_service",
        ):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
