import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from order_preview_service import PaperOrderPreview
from paper_entry_order_tracker import PaperEntryOrderTracker
from supervised_paper_entry_handoff import (
    CORE_ORIGIN,
    LEGACY_UNKNOWN_ORIGIN,
    SubmissionIntent,
    SubmissionIntentJournal,
    deterministic_client_order_id,
)
from uncertain_submission_reconciler import reconcile_uncertain_submission


NOW = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
SYMBOL = "AAPL260918C00150000"


class UncertainSubmissionReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.journal_path = root / "intents.json"
        self.tracker_path = root / "tracker.json"
        self.preview = PaperOrderPreview(
            "AAPL", SYMBOL, "BUY", 1, "LIMIT", "DAY", 1.0, 100.0, True, ["PASS"]
        )
        self.client_id = deterministic_client_order_id(self.preview, "2026-08-24")

    def tearDown(self):
        self.temp.cleanup()

    def make_intent(self, origin=CORE_ORIGIN, status="SUBMISSION_UNCERTAIN", **overrides):
        values = {
            "paper_only": True, "origin": origin, "trading_date": "2026-08-24",
            "client_order_id": self.client_id, "ticker": "AAPL",
            "option_symbol": SYMBOL, "quantity": 1, "limit_price": 1.0,
            "status": status, "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(), "broker_order_id": None,
            "blocking_reason": "read-only reconciliation required",
        }
        values.update(overrides)
        return SubmissionIntent(**values)

    def save_intent(self, **overrides):
        journal = SubmissionIntentJournal(self.journal_path, now=NOW)
        intent = self.make_intent(status="INTENT_PERSISTED", **overrides)
        journal.persist(intent)
        journal.update(intent, overrides.get("status", "SUBMISSION_UNCERTAIN"), NOW.isoformat())
        return intent

    def broker_order(self, status="accepted", filled_qty="0", **overrides):
        values = {
            "id": "broker-1", "client_order_id": self.client_id, "symbol": SYMBOL,
            "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day",
            "position_intent": "buy_to_open", "limit_price": "1.00",
            "filled_qty": filled_qty,
            "filled_avg_price": "0.95" if float(filled_qty) else None,
            "status": status, "submitted_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "filled_at": NOW.isoformat() if float(filled_qty) else None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def call(self, lookup=None, **overrides):
        values = {
            "journal_path": self.journal_path,
            "tracker_path": self.tracker_path,
            "client_order_id": self.client_id,
            "lookup_by_client_order_id": lookup or Mock(return_value=self.broker_order()),
            "now": NOW,
        }
        values.update(overrides)
        return reconcile_uncertain_submission(**values)

    def test_valid_core_uncertain_lookup_binds_once(self):
        self.save_intent()
        lookup = Mock(return_value=self.broker_order())
        result = self.call(lookup)
        self.assertEqual(result.status, "RECONCILED")
        self.assertTrue(result.reconciled)
        lookup.assert_called_once_with(self.client_id)
        self.assertTrue(result.record.order_shape_verified)
        self.assertEqual(SubmissionIntentJournal(self.journal_path).intents[0].status, "BROKER_RECORDED")

    def test_non_uncertain_and_legacy_refuse_without_lookup(self):
        for index, origin_status in enumerate(((CORE_ORIGIN, "BROKER_RECORDED"), (LEGACY_UNKNOWN_ORIGIN, "SUBMISSION_UNCERTAIN"))):
            with self.subTest(origin_status=origin_status):
                root = Path(self.temp.name)
                self.journal_path = root / f"refuse-{index}.json"
                record = self.make_intent(origin=origin_status[0], status=origin_status[1])
                data = {"version": 2, "intents": [record.__dict__]}
                self.journal_path.write_text(json.dumps(data), encoding="utf-8")
                lookup = Mock()
                result = self.call(lookup)
                self.assertEqual(result.status, "REFUSED")
                lookup.assert_not_called()

    def test_malformed_live_and_catalyst_journals_fail_before_lookup(self):
        cases = (
            {"origin": "CATALYST"}, {"origin": ""}, {"paper_only": False},
            {"client_order_id": ""},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.journal_path = Path(self.temp.name) / f"malformed-{index}.json"
                record = self.make_intent().__dict__ | changes
                self.journal_path.write_text(json.dumps({"version": 2, "intents": [record]}), encoding="utf-8")
                lookup = Mock()
                try:
                    result = self.call(lookup)
                    self.assertIn(result.status, {"REFUSED", "CONFLICT"})
                except RuntimeError:
                    pass
                lookup.assert_not_called()

    def test_tracker_first_verified_record_repairs_without_lookup(self):
        self.save_intent()
        tracker = PaperEntryOrderTracker(self.tracker_path)
        tracker.register_submitted(self.broker_order(), "AAPL")
        lookup = Mock()
        result = self.call(lookup)
        self.assertTrue(result.reconciled)
        self.assertFalse(result.lookup_performed)
        lookup.assert_not_called()

    def test_existing_verified_tracker_mismatch_is_conflict(self):
        self.save_intent()
        order = self.broker_order(symbol="MSFT260918C00400000")
        PaperEntryOrderTracker(self.tracker_path).register_submitted(order, "AAPL")
        lookup = Mock()
        result = self.call(lookup)
        self.assertEqual(result.status, "CONFLICT")
        lookup.assert_not_called()

    def test_pending_filled_and_terminal_failures_preserve_outcomes(self):
        cases = (
            ("accepted", "0", "active", False),
            ("filled", "1", "success", True),
            ("rejected", "0", "failure", False),
            ("canceled", "0.5", "failure_with_exposure", True),
        )
        for index, (status, fill, outcome, handoff) in enumerate(cases):
            with self.subTest(status=status, fill=fill):
                root = Path(self.temp.name)
                self.journal_path = root / f"outcome-intent-{index}.json"
                self.tracker_path = root / f"outcome-tracker-{index}.json"
                self.save_intent()
                result = self.call(Mock(return_value=self.broker_order(status, fill)))
                self.assertTrue(result.reconciled)
                self.assertEqual(result.record.outcome, outcome)
                self.assertEqual(result.requires_exit_monitor_handoff, handoff)
                self.assertEqual(result.record.filled_quantity, float(fill))

    def test_not_found_and_provider_errors_remain_uncertain(self):
        for index, lookup in enumerate((Mock(return_value=None), Mock(side_effect=TimeoutError("offline")))):
            with self.subTest(index=index):
                self.journal_path = Path(self.temp.name) / f"uncertain-{index}.json"
                self.tracker_path = Path(self.temp.name) / f"uncertain-tracker-{index}.json"
                self.save_intent()
                result = self.call(lookup)
                self.assertEqual(result.status, "UNCERTAIN")
                self.assertEqual(SubmissionIntentJournal(self.journal_path).intents[0].status, "SUBMISSION_UNCERTAIN")
                lookup.assert_called_once_with(self.client_id)

    def test_every_order_shape_mismatch_prevents_tracker_mutation(self):
        cases = (
            {"client_order_id": "other"}, {"id": None}, {"symbol": "MSFT260918C00400000"},
            {"side": "sell"}, {"qty": "2"}, {"type": "market"},
            {"time_in_force": "gtc"}, {"position_intent": "sell_to_close"},
            {"limit_price": "1.01"}, {"status": None}, {"status": "mystery"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.journal_path = Path(self.temp.name) / f"shape-intent-{index}.json"
                self.tracker_path = Path(self.temp.name) / f"shape-tracker-{index}.json"
                self.save_intent()
                result = self.call(Mock(return_value=self.broker_order(**changes)))
                self.assertEqual(result.status, "CONFLICT")
                self.assertFalse(self.tracker_path.exists())

    def test_tracker_failure_leaves_journal_uncertain(self):
        self.save_intent()
        with patch.object(PaperEntryOrderTracker, "register_submitted", side_effect=OSError("disk")):
            result = self.call()
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(SubmissionIntentJournal(self.journal_path).intents[0].status, "SUBMISSION_UNCERTAIN")

    def test_journal_failure_after_tracker_success_repairs_without_lookup(self):
        self.save_intent()
        original_update = SubmissionIntentJournal.update
        with patch.object(SubmissionIntentJournal, "update", side_effect=OSError("disk")):
            first = self.call()
        self.assertEqual(first.status, "BLOCKED")
        self.assertEqual(len(PaperEntryOrderTracker(self.tracker_path).records), 1)
        with patch.object(SubmissionIntentJournal, "update", original_update):
            lookup = Mock()
            second = self.call(lookup)
        self.assertTrue(second.reconciled)
        lookup.assert_not_called()

    def test_duplicate_restart_is_idempotent(self):
        self.save_intent()
        self.assertTrue(self.call().reconciled)
        lookup = Mock()
        refused = self.call(lookup)
        self.assertEqual(refused.status, "REFUSED")
        lookup.assert_not_called()
        self.assertEqual(len(PaperEntryOrderTracker(self.tracker_path).records), 1)

    def test_unverified_legacy_tracker_requires_lookup_then_becomes_verified(self):
        self.save_intent()
        PaperEntryOrderTracker(self.tracker_path).register_submitted(self.broker_order(), "AAPL")
        data = json.loads(self.tracker_path.read_text())
        for field in ("side", "order_type", "time_in_force", "position_intent", "order_shape_verified"):
            data["orders"][0].pop(field)
        data["version"] = 2
        self.tracker_path.write_text(json.dumps(data), encoding="utf-8")
        lookup = Mock(return_value=self.broker_order())
        result = self.call(lookup)
        self.assertTrue(result.reconciled)
        lookup.assert_called_once()
        self.assertTrue(PaperEntryOrderTracker(self.tracker_path).record.order_shape_verified)

    def test_unverified_legacy_tracker_without_provider_remains_uncertain(self):
        self.save_intent()
        PaperEntryOrderTracker(self.tracker_path).register_submitted(self.broker_order(), "AAPL")
        data = json.loads(self.tracker_path.read_text())
        for field in ("side", "order_type", "time_in_force", "position_intent", "order_shape_verified"):
            data["orders"][0].pop(field)
        data["version"] = 2
        self.tracker_path.write_text(json.dumps(data), encoding="utf-8")
        result = self.call(Mock(return_value=None))
        self.assertEqual(result.status, "UNCERTAIN")
        self.assertFalse(PaperEntryOrderTracker(self.tracker_path).record.order_shape_verified)

    def test_no_execution_or_network_path_is_exposed(self):
        source = Path("uncertain_submission_reconciler.py").read_text(encoding="utf-8")
        for forbidden in (
            "submit_paper_entry", "submit_order", "cancel_order", "replace_order",
            "close_position", "TradingClient", "alpaca_service", "catalyst",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
