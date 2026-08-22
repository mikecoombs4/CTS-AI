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
    LEGACY_UNKNOWN_ORIGIN,
    SubmissionIntent,
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
            side="buy",
            type="limit",
            time_in_force="day",
            position_intent="buy_to_open",
            submitted_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            filled_at=None,
        )

    def legacy_record(self, **overrides):
        record = {
            "paper_only": True,
            "trading_date": "2026-08-24",
            "client_order_id": deterministic_client_order_id(
                self.preview, "2026-08-24"
            ),
            "ticker": "AAPL",
            "option_symbol": self.preview.contract_symbol,
            "quantity": 1,
            "limit_price": 1.0,
            "status": "SUBMISSION_UNCERTAIN",
            "created_at": "2026-08-24T09:59:00-04:00",
            "updated_at": "2026-08-24T10:00:00-04:00",
            "broker_order_id": None,
            "blocking_reason": "legacy audit reason",
        }
        record.update(overrides)
        return record

    def write_journal(self, version, records, path=None):
        target = path or self.intent_path
        target.write_text(
            json.dumps({"version": version, "intents": records}, sort_keys=True) + "\n",
            encoding="utf-8",
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
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["intents"][0]["origin"], CORE_ORIGIN)
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
        reloaded = self.journal()
        self.assertEqual(len(reloaded.intents), 1)
        self.assertEqual(reloaded.intents[0].origin, CORE_ORIGIN)

    def test_new_core_intent_round_trip_preserves_origin(self):
        result = self.call()
        self.assertEqual(result.status, "SUBMITTED")
        saved = json.loads(self.intent_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["intents"][0]["origin"], CORE_ORIGIN)
        self.assertEqual(self.journal().intents[0].origin, CORE_ORIGIN)

    def test_version_two_invalid_origins_fail_closed(self):
        for index, origin in enumerate(
            (None, "", "CATALYST", "core_cts", 7, [CORE_ORIGIN], {"x": "y"})
        ):
            with self.subTest(origin=origin):
                path = Path(self.temp_dir.name) / f"invalid-origin-{index}.json"
                record = self.legacy_record()
                if origin is not None:
                    record["origin"] = origin
                self.write_journal(2, [record], path)
                original = path.read_bytes()
                with self.assertRaises(RuntimeError):
                    SubmissionIntentJournal(path, now=NOW)
                self.assertEqual(path.read_bytes(), original)

    def test_version_one_loads_legacy_unknown_without_rewrite(self):
        record = self.legacy_record()
        self.write_journal(1, [record])
        original = self.intent_path.read_bytes()
        journal = self.journal()
        self.assertEqual(journal.intents[0].origin, LEGACY_UNKNOWN_ORIGIN)
        self.assertNotEqual(journal.intents[0].origin, CORE_ORIGIN)
        self.assertEqual(self.intent_path.read_bytes(), original)

    def test_version_one_distrusts_inserted_origin(self):
        for claimed_origin in (CORE_ORIGIN, "CATALYST", "", 9):
            with self.subTest(claimed_origin=claimed_origin):
                record = self.legacy_record(origin=claimed_origin)
                self.write_journal(1, [record])
                original = self.intent_path.read_bytes()
                journal = self.journal()
                self.assertEqual(journal.intents[0].origin, LEGACY_UNKNOWN_ORIGIN)
                self.assertEqual(self.intent_path.read_bytes(), original)

    def test_version_one_uncertain_intent_remains_consumed(self):
        self.write_journal(1, [self.legacy_record()])
        submit = Mock()
        result = self.call(submitter=submit, journal=self.journal())
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()
        intent = self.journal().intents[0]
        self.assertEqual(intent.status, "SUBMISSION_UNCERTAIN")
        self.assertEqual(intent.origin, LEGACY_UNKNOWN_ORIGIN)

    def test_version_one_terminal_intent_retains_exact_audit_fields(self):
        record = self.legacy_record(
            status="BROKER_RECORDED",
            broker_order_id="legacy-broker-7",
            blocking_reason="historical terminal audit",
        )
        self.write_journal(1, [record])
        intent = self.journal().intents[0]
        for field, value in record.items():
            self.assertEqual(getattr(intent, field), value)
        self.assertEqual(intent.origin, LEGACY_UNKNOWN_ORIGIN)
        submit = Mock()
        result = self.call(submitter=submit, journal=self.journal())
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()

    def test_version_two_write_preserves_migrated_legacy_record(self):
        legacy = self.legacy_record(
            trading_date="2026-08-23", status="BROKER_RECORDED"
        )
        self.write_journal(1, [legacy])
        journal = self.journal()
        journal.persist(SubmissionIntent(
            paper_only=True,
            origin=CORE_ORIGIN,
            trading_date="2026-08-24",
            client_order_id="cts-paper-new-core-record",
            ticker="MSFT",
            option_symbol="MSFT260918C00400000",
            quantity=1,
            limit_price=1.25,
            status="INTENT_PERSISTED",
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        ))
        saved = json.loads(self.intent_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(len(saved["intents"]), 2)
        migrated = saved["intents"][0]
        self.assertEqual(migrated["origin"], LEGACY_UNKNOWN_ORIGIN)
        for field, value in legacy.items():
            self.assertEqual(migrated[field], value)

    def test_origin_conflict_fails_before_submitter(self):
        record = self.legacy_record()
        record["origin"] = LEGACY_UNKNOWN_ORIGIN
        self.write_journal(2, [record])
        journal = self.journal()
        conflicting = SubmissionIntent(
            paper_only=True,
            origin=CORE_ORIGIN,
            trading_date=record["trading_date"],
            client_order_id=record["client_order_id"],
            ticker=record["ticker"],
            option_symbol=record["option_symbol"],
            quantity=record["quantity"],
            limit_price=record["limit_price"],
            status="INTENT_PERSISTED",
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        with self.assertRaisesRegex(RuntimeError, "origin conflicts"):
            journal.persist(conflicting)
        submit = Mock()
        result = self.call(submitter=submit, journal=journal)
        self.assertEqual(result.status, "BLOCKED")
        submit.assert_not_called()
        self.assertEqual(journal.intents[0].origin, LEGACY_UNKNOWN_ORIGIN)

    def test_update_preserves_origin_and_unrelated_audit_fields(self):
        result = self.call()
        intent = result.intent
        self.assertIsNotNone(intent)
        preserved = {
            "paper_only": intent.paper_only,
            "origin": intent.origin,
            "trading_date": intent.trading_date,
            "client_order_id": intent.client_order_id,
            "ticker": intent.ticker,
            "option_symbol": intent.option_symbol,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "created_at": intent.created_at,
        }
        journal = self.journal()
        loaded = journal.intents[0]
        journal.update(
            loaded,
            "SUBMISSION_UNCERTAIN",
            (NOW + timedelta(minutes=1)).isoformat(),
            broker_order_id="broker-order-1",
            blocking_reason="audit update",
        )
        twice_reloaded = SubmissionIntentJournal(self.intent_path, now=NOW)
        final = twice_reloaded.intents[0]
        for field, value in preserved.items():
            self.assertEqual(getattr(final, field), value)
        self.assertEqual(final.origin, CORE_ORIGIN)

    def test_update_rejects_and_restores_mutated_origin(self):
        self.call()
        journal = self.journal()
        intent = journal.intents[0]
        intent.origin = LEGACY_UNKNOWN_ORIGIN
        with self.assertRaisesRegex(RuntimeError, "origin cannot change"):
            journal.update(intent, "SUBMISSION_UNCERTAIN", NOW.isoformat())
        self.assertEqual(intent.origin, CORE_ORIGIN)
        self.assertEqual(self.journal().intents[0].origin, CORE_ORIGIN)

    def test_duplicate_json_keys_and_client_ids_fail_without_rewrite(self):
        duplicate_key_path = Path(self.temp_dir.name) / "duplicate-key.json"
        record_json = json.dumps({**self.legacy_record(), "origin": CORE_ORIGIN})
        duplicate_key_path.write_text(
            '{"version":2,"version":2,"intents":[' + record_json + "]}\n",
            encoding="utf-8",
        )
        duplicate_id_path = Path(self.temp_dir.name) / "duplicate-id.json"
        record = {**self.legacy_record(), "origin": CORE_ORIGIN}
        self.write_journal(2, [record, record], duplicate_id_path)
        for path in (duplicate_key_path, duplicate_id_path):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                with self.assertRaises(RuntimeError):
                    SubmissionIntentJournal(path, now=NOW)
                self.assertEqual(path.read_bytes(), original)

    def test_atomic_write_failures_preserve_valid_journal_and_clean_temp(self):
        self.call()
        before = self.intent_path.read_bytes()
        stages = (
            "supervised_paper_entry_handoff._write_temporary",
            "supervised_paper_entry_handoff._flush_temporary",
            "supervised_paper_entry_handoff._fsync_temporary",
            "pathlib.Path.replace",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                journal = self.journal()
                intent = journal.intents[0]
                with patch(stage, side_effect=OSError("atomic stage failure")):
                    with self.assertRaises(RuntimeError):
                        journal.update(
                            intent,
                            "SUBMISSION_UNCERTAIN",
                            (NOW + timedelta(minutes=1)).isoformat(),
                        )
                self.assertEqual(self.intent_path.read_bytes(), before)
                self.assertFalse(
                    self.intent_path.with_suffix(self.intent_path.suffix + ".tmp").exists()
                )
                self.assertEqual(intent.status, "BROKER_RECORDED")

    def test_failed_migration_write_preserves_original_version_one_file(self):
        self.write_journal(1, [self.legacy_record(status="BROKER_RECORDED")])
        before = self.intent_path.read_bytes()
        journal = self.journal()
        with patch("pathlib.Path.replace", side_effect=OSError("replace failure")):
            with self.assertRaises(RuntimeError):
                journal.update(
                    journal.intents[0],
                    "SUBMISSION_UNCERTAIN",
                    (NOW + timedelta(minutes=1)).isoformat(),
                )
        self.assertEqual(self.intent_path.read_bytes(), before)
        self.assertFalse(
            self.intent_path.with_suffix(self.intent_path.suffix + ".tmp").exists()
        )

    def test_new_runtime_intent_rejects_every_non_core_origin(self):
        invalid_origins = (
            LEGACY_UNKNOWN_ORIGIN,
            "CATALYST",
            "",
            "core_cts",
            4,
            [CORE_ORIGIN],
            {"origin": CORE_ORIGIN},
        )
        for index, origin in enumerate(invalid_origins):
            with self.subTest(origin=origin):
                intent_path = Path(self.temp_dir.name) / f"runtime-origin-{index}.json"
                submit = Mock()
                result = self.call(
                    origin=origin,
                    submitter=submit,
                    journal=SubmissionIntentJournal(intent_path, now=NOW),
                )
                self.assertEqual(result.status, "BLOCKED")
                submit.assert_not_called()
                self.assertFalse(intent_path.exists())

    def test_journal_cannot_persist_new_legacy_unknown_intent(self):
        intent = SubmissionIntent(
            paper_only=True,
            origin=LEGACY_UNKNOWN_ORIGIN,
            trading_date="2026-08-24",
            client_order_id="new-legacy-is-forbidden",
            ticker="AAPL",
            option_symbol=self.preview.contract_symbol,
            quantity=1,
            limit_price=1.0,
            status="INTENT_PERSISTED",
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        with self.assertRaisesRegex(RuntimeError, "Only CORE_CTS"):
            self.journal().persist(intent)
        self.assertFalse(self.intent_path.exists())

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
        self.assertEqual(saved["intents"][0]["origin"], CORE_ORIGIN)

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
        self.assertEqual(first.intent.origin, CORE_ORIGIN)
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
        journal.persist(SubmissionIntent(
            paper_only=True,
            origin=CORE_ORIGIN,
            trading_date="2026-08-24",
            client_order_id=client_id,
            ticker="AAPL",
            option_symbol=self.preview.contract_symbol,
            quantity=1,
            limit_price=1.0,
            status="INTENT_PERSISTED",
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
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
        self.assertFalse(self.intent_path.exists())

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

    def test_verified_autonomous_soft_news_handoff_preserves_core_review(self):
        soft_preview = build_paper_order_preview(
            "AAPL", self.preview.contract_symbol, "REVIEW", 1.0, True, True
        )
        news = SimpleNamespace(
            status="REVIEW", provider_query_succeeded=True,
            blocking_matches=[], catalyst_matches=[],
            queried_at=NOW,
            headlines=[SimpleNamespace(
                headline="ordinary current headline", created_at=NOW,
                blocking_matches=[], catalyst_matches=[],
            )],
        )
        readiness = SimpleNamespace(
            **{
                **self.readiness.__dict__, "status": "BLOCK", "allowed": False,
                "news_risk": news,
                "final_decision": SimpleNamespace(
                    status="REVIEW", automatic_paper_eligible=False,
                    reasons=["News requires human or AI review"],
                ),
                "order_preview": soft_preview,
            }
        )
        autonomous = SimpleNamespace(
            status="STARTUP_READY", paper_configuration_verified=True,
            autonomous_configuration_verified=True,
            execution_configuration_verified=True, submission_authorized=False,
            broker_ready=True, broker_readiness=self.broker,
            trial_limits=SimpleNamespace(max_trades_per_day=1, max_open_positions=1),
        )
        policy = SimpleNamespace(
            status="PAPER_SOFT_PASS", allowed=True,
            live_execution_eligible=False, softened_gate="news_risk",
        )
        submit = Mock(side_effect=lambda **kwargs: self.broker_response(kwargs["client_order_id"]))
        result = self.call(
            readiness=readiness, preview=soft_preview, preflight=autonomous,
            autonomous_policy=policy, autonomous_startup_preflight=autonomous,
            submitter=submit,
        )
        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(readiness.final_decision.status, "REVIEW")
        self.assertFalse(soft_preview.eligible)
        self.assertTrue(submit.call_args.kwargs["preview"].eligible)


if __name__ == "__main__":
    unittest.main()
