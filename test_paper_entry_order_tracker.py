import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paper_entry_order_tracker import PaperEntryOrderTracker


ENTRY_DAY = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)


def order(status="new", filled_qty="0", filled_avg_price=None, **overrides):
    values = {
        "id": "broker-1",
        "client_order_id": "cts-entry-1",
        "symbol": "SPY260821C00650000",
        "qty": "1",
        "filled_qty": filled_qty,
        "limit_price": "0.40",
        "filled_avg_price": filled_avg_price,
        "status": status,
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "position_intent": "buy_to_open",
        "submitted_at": "2026-08-21T14:00:00+00:00",
        "updated_at": "2026-08-21T14:00:00+00:00",
        "filled_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PaperEntryOrderTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "entry-order.json"
        self.tracker = PaperEntryOrderTracker(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, broker_order=None):
        return self.tracker.register_submitted(broker_order or order(), "SPY")

    def test_new_and_accepted_orders_persist_as_active(self):
        for status in ("new", "accepted"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"{status}.json"
                tracker = PaperEntryOrderTracker(path)
                record = tracker.register_submitted(order(status=status), "SPY")
                self.assertFalse(record.terminal)
                self.assertEqual(record.outcome, "active")
                self.assertTrue(record.order_shape_verified)
                self.assertEqual(
                    (record.side, record.order_type, record.time_in_force, record.position_intent),
                    ("buy", "limit", "day", "buy_to_open"),
                )
                self.assertTrue(json.loads(path.read_text())["orders"][0]["paper_only"])

    def test_new_record_rejects_missing_or_conflicting_order_shape(self):
        cases = (
            {"side": None}, {"side": "sell"}, {"type": None}, {"type": "market"},
            {"time_in_force": None}, {"time_in_force": "gtc"},
            {"position_intent": None}, {"position_intent": "sell_to_close"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                path = Path(self.temp_dir.name) / f"shape-{index}.json"
                tracker = PaperEntryOrderTracker(path)
                with self.assertRaises(ValueError):
                    tracker.register_submitted(order(**changes), "SPY")
                self.assertEqual(tracker.records, [])
                self.assertFalse(path.exists())

    def test_version_two_migrates_unverified_without_read_rewrite(self):
        self.register()
        data = json.loads(self.path.read_text())
        record = data["orders"][0]
        for field in (
            "side", "order_type", "time_in_force", "position_intent",
            "order_shape_verified",
        ):
            record.pop(field)
        data["version"] = 2
        self.path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        before = self.path.read_bytes()
        migrated = PaperEntryOrderTracker(self.path)
        self.assertFalse(migrated.record.order_shape_verified)
        self.assertEqual(migrated.record.side, "unknown")
        self.assertEqual(migrated.record.filled_quantity, 0)
        self.assertFalse(migrated.new_entry_allowed(ENTRY_DAY))
        self.assertEqual(self.path.read_bytes(), before)

    def test_later_write_preserves_migrated_legacy_record(self):
        self.register(order(status="rejected"))
        data = json.loads(self.path.read_text())
        for field in (
            "side", "order_type", "time_in_force", "position_intent",
            "order_shape_verified",
        ):
            data["orders"][0].pop(field)
        data["version"] = 2
        self.path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        migrated = PaperEntryOrderTracker(self.path)
        migrated.register_submitted(order(
            id="broker-2", client_order_id="client-2",
            submitted_at="2026-08-24T14:00:00+00:00",
            updated_at="2026-08-24T14:00:00+00:00",
        ), "SPY")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["version"], 3)
        self.assertEqual(len(saved["orders"]), 2)
        self.assertFalse(saved["orders"][0]["order_shape_verified"])
        self.assertEqual(saved["orders"][0]["position_intent"], "unknown")

    def test_duplicate_json_keys_and_client_ids_fail_closed_without_rewrite(self):
        record = order().__dict__
        tracker = PaperEntryOrderTracker(self.path)
        tracker.register_submitted(order(), "SPY")
        valid = json.loads(self.path.read_text())
        duplicate_key = self.path.with_name("duplicate-key.json")
        duplicate_key.write_text('{"version":3,"version":3,"orders":[]}', encoding="utf-8")
        duplicate_id = self.path.with_name("duplicate-id.json")
        second = dict(valid["orders"][0])
        second["broker_order_id"] = "broker-2"
        valid["orders"].append(second)
        duplicate_id.write_text(json.dumps(valid), encoding="utf-8")
        for path in (duplicate_key, duplicate_id):
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                PaperEntryOrderTracker(path)
            self.assertEqual(path.read_bytes(), before)

    def test_atomic_register_failure_preserves_prior_state_and_cleans_temp(self):
        self.register(order(status="rejected"))
        before = self.path.read_bytes()
        tracker = PaperEntryOrderTracker(self.path)
        with patch("pathlib.Path.replace", side_effect=OSError("disk")):
            with self.assertRaises(RuntimeError):
                tracker.register_submitted(order(
                    id="broker-2", client_order_id="client-2",
                    submitted_at="2026-08-24T14:00:00+00:00",
                    updated_at="2026-08-24T14:00:00+00:00",
                ), "SPY")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(tracker.records), 1)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_partial_fill_updates_absolute_quantity_and_price_without_duplication(self):
        self.register()
        update = order(
            status="partially_filled",
            filled_qty="0.5",
            filled_avg_price="0.38",
            updated_at="2026-08-21T14:01:00+00:00",
        )
        first = self.tracker.reconcile(Mock(return_value=update))
        second = self.tracker.reconcile(Mock(return_value=update))
        self.assertEqual(first.record.filled_quantity, 0.5)
        self.assertEqual(first.record.average_fill_price, 0.38)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)

    def test_filled_becomes_terminal_success(self):
        self.register()
        result = self.tracker.reconcile(Mock(return_value=order(
            status="filled", filled_qty="1", filled_avg_price="0.39",
            filled_at="2026-08-21T14:02:00+00:00",
            updated_at="2026-08-21T14:02:00+00:00",
        )))
        self.assertTrue(result.record.terminal)
        self.assertEqual(result.record.outcome, "success")

    def test_rejected_canceled_and_expired_are_terminal_failures(self):
        for status in ("rejected", "canceled", "expired"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"failure-{status}.json"
                tracker = PaperEntryOrderTracker(path)
                record = tracker.register_submitted(order(status=status), "SPY")
                self.assertTrue(record.terminal)
                self.assertEqual(record.outcome, "failure")
                self.assertIsNotNone(record.blocking_reason)

    def test_additional_broker_statuses_are_classified_conservatively(self):
        expectations = {
            "done_for_day": True,
            "replaced": True,
            "pending_replace": False,
            "stopped": False,
            "suspended": True,
            "calculated": False,
        }
        for status, terminal in expectations.items():
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"extra-{status}.json"
                record = PaperEntryOrderTracker(path).register_submitted(
                    order(status=status), "SPY"
                )
                self.assertEqual(record.terminal, terminal)
                self.assertIsNotNone(record.blocking_reason)

    def test_unknown_status_fails_closed(self):
        record = self.tracker.register_submitted(order(status="mystery"), "SPY")
        self.assertTrue(record.terminal)
        self.assertEqual(record.outcome, "failure")
        self.assertIn("Unknown", record.blocking_reason)
        self.assertFalse(self.tracker.new_entry_allowed(ENTRY_DAY))

    def test_identical_updates_are_idempotent(self):
        self.register()
        before = self.path.read_text()
        result = self.tracker.reconcile(Mock(return_value=order()))
        self.assertFalse(result.changed)
        self.assertEqual(self.path.read_text(), before)

    def test_pending_or_partial_order_blocks_second_entry(self):
        for status in ("pending_new", "partially_filled"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"blocked-{status}.json"
                tracker = PaperEntryOrderTracker(path)
                tracker.register_submitted(order(status=status), "SPY")
                self.assertFalse(tracker.new_entry_allowed(ENTRY_DAY))
                with self.assertRaises(RuntimeError):
                    tracker.register_submitted(order(id="broker-2"), "QQQ")

    def test_restart_reload_preserves_record_and_consumed_allowance(self):
        self.register()
        restored = PaperEntryOrderTracker(self.path)
        self.assertEqual(restored.record.broker_order_id, "broker-1")
        self.assertEqual(restored.record.option_symbol, "SPY260821C00650000")
        self.assertFalse(restored.new_entry_allowed(ENTRY_DAY))

    def test_reconciliation_failure_preserves_existing_state(self):
        original = self.register()
        before = self.path.read_text()
        result = self.tracker.reconcile(Mock(side_effect=RuntimeError("offline")))
        self.assertFalse(result.reconciled)
        self.assertEqual(result.record, original)
        self.assertEqual(self.path.read_text(), before)
        self.assertFalse(self.tracker.new_entry_allowed(ENTRY_DAY))

    def test_terminal_failure_still_consumes_same_day_allowance(self):
        for status in ("rejected", "canceled", "expired"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"allowance-{status}.json"
                tracker = PaperEntryOrderTracker(path)
                tracker.register_submitted(order(status=status), "SPY")
                self.assertFalse(tracker.new_entry_allowed(ENTRY_DAY))

    def test_next_eastern_trading_date_has_new_allowance_and_preserves_audit(self):
        self.register(order(status="rejected"))
        self.assertTrue(self.tracker.new_entry_allowed(NEXT_DAY))
        self.tracker.register_submitted(order(
            id="broker-2",
            client_order_id="cts-entry-2",
            submitted_at="2026-08-24T14:00:00+00:00",
            updated_at="2026-08-24T14:00:00+00:00",
        ), "SPY")
        self.assertEqual(len(self.tracker.records), 2)
        self.assertEqual(self.tracker.records[0].trading_date, "2026-08-21")
        self.assertEqual(self.tracker.records[1].trading_date, "2026-08-24")

    def test_prior_day_nonterminal_order_still_blocks_second_entry(self):
        self.register(order(status="partially_filled", filled_qty="0.5"))
        self.assertFalse(self.tracker.new_entry_allowed(NEXT_DAY))

    def test_restart_preserves_daily_allowances_and_prior_day_audit(self):
        self.register(order(status="canceled"))
        restarted = PaperEntryOrderTracker(self.path)
        self.assertFalse(restarted.new_entry_allowed(ENTRY_DAY))
        self.assertTrue(restarted.new_entry_allowed(NEXT_DAY))
        self.assertEqual(len(restarted.records), 1)

    def test_stale_active_update_cannot_downgrade_terminal_states(self):
        for status in ("filled", "rejected", "canceled", "expired"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"monotonic-{status}.json"
                tracker = PaperEntryOrderTracker(path)
                tracker.register_submitted(order(
                    status=status,
                    filled_qty="1" if status == "filled" else "0",
                    filled_avg_price="0.39" if status == "filled" else None,
                    updated_at="2026-08-21T14:05:00+00:00",
                    filled_at="2026-08-21T14:05:00+00:00" if status == "filled" else None,
                ), "SPY")
                result = tracker.reconcile(Mock(return_value=order(
                    status="new",
                    updated_at="2026-08-21T14:01:00+00:00",
                )))
                self.assertTrue(result.record.terminal)
                self.assertEqual(result.record.normalized_status, status)
                self.assertEqual(result.record.updated_at, "2026-08-21T14:05:00+00:00")

    def test_stale_update_cannot_reduce_fill_or_regress_timestamps(self):
        self.register()
        self.tracker.reconcile(Mock(return_value=order(
            status="partially_filled",
            filled_qty="0.75",
            filled_avg_price="0.38",
            updated_at="2026-08-21T14:05:00+00:00",
            filled_at="2026-08-21T14:05:00+00:00",
        )))
        result = self.tracker.reconcile(Mock(return_value=order(
            status="partially_filled",
            filled_qty="0.25",
            filled_avg_price="0.35",
            updated_at="2026-08-21T14:02:00+00:00",
            filled_at="2026-08-21T14:02:00+00:00",
        )))
        self.assertEqual(result.record.filled_quantity, 0.75)
        self.assertEqual(result.record.average_fill_price, 0.38)
        self.assertEqual(result.record.updated_at, "2026-08-21T14:05:00+00:00")
        self.assertEqual(result.record.filled_at, "2026-08-21T14:05:00+00:00")

    def test_partial_fill_then_canceled_or_expired_preserves_exposure(self):
        for status in ("canceled", "expired"):
            with self.subTest(status=status):
                path = Path(self.temp_dir.name) / f"partial-{status}.json"
                tracker = PaperEntryOrderTracker(path)
                tracker.register_submitted(order(), "SPY")
                tracker.reconcile(Mock(return_value=order(
                    status="partially_filled",
                    filled_qty="0.5",
                    filled_avg_price="0.38",
                    updated_at="2026-08-21T14:01:00+00:00",
                )))
                result = tracker.reconcile(Mock(return_value=order(
                    status=status,
                    filled_qty="0.5",
                    filled_avg_price=None,
                    updated_at="2026-08-21T14:02:00+00:00",
                )))
                self.assertTrue(result.record.terminal)
                self.assertEqual(result.record.outcome, "failure_with_exposure")
                self.assertEqual(result.record.filled_quantity, 0.5)
                self.assertEqual(result.record.average_fill_price, 0.38)
                self.assertTrue(result.record.position_exposure_exists)
                self.assertTrue(result.record.requires_exit_monitor_handoff)
                self.assertIn("exit-monitor handoff", result.record.blocking_reason)

    def test_partial_fill_then_unknown_status_fails_closed_with_exposure(self):
        self.register(order(
            status="partially_filled",
            filled_qty="0.5",
            filled_avg_price="0.38",
        ))
        result = self.tracker.reconcile(Mock(return_value=order(
            status="broker_mystery_terminal",
            filled_qty="0.5",
            updated_at="2026-08-21T14:02:00+00:00",
        )))
        self.assertTrue(result.record.terminal)
        self.assertEqual(result.record.outcome, "failure_with_exposure")
        self.assertTrue(result.record.requires_exit_monitor_handoff)
        self.assertIn("Unknown or ambiguous", result.record.blocking_reason)

    def test_no_execution_function_is_called(self):
        forbidden = Mock()
        client = SimpleNamespace(
            get_order_by_id=Mock(return_value=order(status="accepted")),
            submit_order=forbidden,
            replace_order=forbidden,
            cancel_order=forbidden,
            close_position=forbidden,
        )
        self.register()
        self.tracker.reconcile(client.get_order_by_id)
        client.get_order_by_id.assert_called_once_with("broker-1")
        forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
