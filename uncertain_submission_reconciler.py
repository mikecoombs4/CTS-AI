"""Read-only broker reconciliation for uncertain supervised paper entries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from order_preview_service import PaperOrderPreview
from paper_entry_order_tracker import (
    ACTIVE_STATUSES,
    CONSERVATIVE_NONTERMINAL_STATUSES,
    TERMINAL_FAILURE_STATUSES,
    TERMINAL_SUCCESS_STATUSES,
    PaperEntryOrderRecord,
    PaperEntryOrderTracker,
    eastern_trading_date,
    normalize_status,
)
from paper_execution_service import build_paper_entry_payload
from supervised_paper_entry_handoff import (
    CORE_ORIGIN,
    SubmissionIntent,
    SubmissionIntentJournal,
    deterministic_client_order_id,
)


RECOGNIZED_STATUSES = (
    ACTIVE_STATUSES
    | CONSERVATIVE_NONTERMINAL_STATUSES
    | TERMINAL_FAILURE_STATUSES
    | TERMINAL_SUCCESS_STATUSES
)


@dataclass(frozen=True)
class UncertainReconciliationResult:
    status: str
    reconciled: bool
    lookup_performed: bool
    reasons: list[str]
    intent: SubmissionIntent | None = None
    record: PaperEntryOrderRecord | None = None

    @property
    def requires_exit_monitor_handoff(self) -> bool:
        return bool(self.record and self.record.requires_exit_monitor_handoff)


def _value(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(_text(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} is malformed.") from error
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field} is invalid.")
    return number.normalize()


def _expected_payload(intent: SubmissionIntent) -> Any:
    if (
        intent.paper_only is not True
        or intent.origin != CORE_ORIGIN
        or not isinstance(intent.client_order_id, str)
        or not intent.client_order_id.strip()
        or not isinstance(intent.ticker, str)
        or not intent.ticker.strip()
        or not isinstance(intent.option_symbol, str)
        or not intent.option_symbol.strip()
        or isinstance(intent.quantity, bool)
        or intent.quantity != 1
        or not isinstance(intent.limit_price, (int, float))
        or not math.isfinite(float(intent.limit_price))
        or intent.limit_price <= 0
    ):
        raise ValueError("Persisted uncertain intent is malformed or non-paper.")
    preview = PaperOrderPreview(
        ticker=intent.ticker.strip().upper(),
        contract_symbol=intent.option_symbol.strip().upper(),
        side="BUY",
        quantity=1,
        order_type="LIMIT",
        time_in_force="DAY",
        limit_price=float(intent.limit_price),
        estimated_cost=float(intent.limit_price) * 100,
        eligible=True,
        reasons=["Reconstructed solely from persisted supervised intent."],
    )
    expected_id = deterministic_client_order_id(preview, intent.trading_date)
    if expected_id != intent.client_order_id:
        raise ValueError("Persisted deterministic client order ID does not match its intent.")
    return build_paper_entry_payload(preview, intent.client_order_id)


def _tracker_conflict(
    record: PaperEntryOrderRecord,
    intent: SubmissionIntent,
    payload: Any,
) -> str | None:
    if (
        record.paper_only is not True
        or record.client_order_id != intent.client_order_id
        or record.option_symbol != intent.option_symbol.strip().upper()
        or record.underlying_ticker != intent.ticker.strip().upper()
        or record.requested_quantity != 1
        or _decimal(record.limit_price, "Tracker limit price")
        != _decimal(intent.limit_price, "Intent limit price")
        or not record.broker_order_id.strip()
        or (
            intent.broker_order_id is not None
            and intent.broker_order_id != record.broker_order_id
        )
    ):
        return "Existing tracker record conflicts with the uncertain intent."
    if record.order_shape_verified and (
        record.side != payload.side
        or record.order_type != payload.order_type
        or record.time_in_force != payload.time_in_force
        or record.position_intent != payload.position_intent
    ):
        return "Existing verified tracker order shape conflicts with the uncertain intent."
    return None


def _validate_broker_order(order: Any, intent: SubmissionIntent, payload: Any) -> None:
    if _text(_value(order, "client_order_id")) != intent.client_order_id:
        raise ValueError("Broker client order ID does not match.")
    if not _text(_value(order, "id")):
        raise ValueError("Broker order ID is missing.")
    if _text(_value(order, "symbol")).upper() != intent.option_symbol.strip().upper():
        raise ValueError("Broker option symbol does not match.")
    if normalize_status(_value(order, "side")) != "buy":
        raise ValueError("Broker order side is not BUY.")
    try:
        quantity = float(_text(_value(order, "qty")))
    except ValueError as error:
        raise ValueError("Broker quantity is malformed.") from error
    if not math.isfinite(quantity) or quantity != 1:
        raise ValueError("Broker quantity is not exactly one.")
    if normalize_status(_value(order, "type", _value(order, "order_type"))) != "limit":
        raise ValueError("Broker order type is not LIMIT.")
    if normalize_status(_value(order, "time_in_force")) != "day":
        raise ValueError("Broker time in force is not DAY.")
    position_intent = _value(order, "position_intent")
    if position_intent is not None and normalize_status(position_intent) != "buy_to_open":
        raise ValueError("Broker position intent conflicts with BUY_TO_OPEN.")
    if _decimal(_value(order, "limit_price"), "Broker limit price") != _decimal(
        payload.limit_price, "Expected limit price"
    ):
        raise ValueError("Broker limit price does not match.")
    if normalize_status(_value(order, "status")) not in RECOGNIZED_STATUSES:
        raise ValueError("Broker order status is missing or unrecognized.")
    submitted = _text(_value(order, "submitted_at"))
    try:
        submitted_at = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Broker submitted timestamp is malformed.") from error
    if submitted_at.tzinfo is None or eastern_trading_date(submitted_at).isoformat() != intent.trading_date:
        raise ValueError("Broker submitted timestamp does not match the intent trading date.")


def reconcile_uncertain_submission(
    *,
    journal_path: Path,
    tracker_path: Path,
    client_order_id: str,
    lookup_by_client_order_id: Callable[[str], Any],
    now: datetime | None = None,
) -> UncertainReconciliationResult:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Reconciliation time must include a timezone.")
    journal = SubmissionIntentJournal(Path(journal_path), now=current)
    tracker = PaperEntryOrderTracker(Path(tracker_path))
    matches = [item for item in journal.intents if item.client_order_id == client_order_id]
    if len(matches) != 1:
        return UncertainReconciliationResult(
            "REFUSED", False, False, ["Exact persisted submission intent was not found."]
        )
    intent = matches[0]
    if intent.status != "SUBMISSION_UNCERTAIN" or intent.origin != CORE_ORIGIN:
        return UncertainReconciliationResult(
            "REFUSED", False, False,
            ["Only exact CORE_CTS SUBMISSION_UNCERTAIN intents may be reconciled."], intent,
        )
    try:
        payload = _expected_payload(intent)
    except Exception as error:
        return UncertainReconciliationResult(
            "CONFLICT", False, False, [f"Persisted intent failed validation ({type(error).__name__})."], intent,
        )

    tracker_matches = [item for item in tracker.records if item.client_order_id == client_order_id]
    if len(tracker_matches) > 1:
        return UncertainReconciliationResult(
            "CONFLICT", False, False, ["Duplicate tracker client order identity exists."], intent,
        )
    existing = tracker_matches[0] if tracker_matches else None
    if existing is not None:
        try:
            conflict = _tracker_conflict(existing, intent, payload)
        except Exception:
            conflict = "Existing tracker record is malformed or conflicts with the intent."
        if conflict:
            return UncertainReconciliationResult("CONFLICT", False, False, [conflict], intent, existing)
        if existing.order_shape_verified:
            try:
                journal.update(
                    intent, "BROKER_RECORDED", current.isoformat(),
                    broker_order_id=existing.broker_order_id,
                )
            except Exception as error:
                return UncertainReconciliationResult(
                    "BLOCKED", False, False,
                    [f"Tracker proof exists but journal repair failed ({type(error).__name__})."],
                    intent, existing,
                )
            return UncertainReconciliationResult("RECONCILED", True, False, [], intent, existing)

    try:
        order = lookup_by_client_order_id(intent.client_order_id)
    except Exception as error:
        return UncertainReconciliationResult(
            "UNCERTAIN", False, True,
            [f"Read-only broker lookup remains uncertain ({type(error).__name__})."], intent, existing,
        )
    if order is None:
        return UncertainReconciliationResult(
            "UNCERTAIN", False, True, ["Broker order was not found; intent remains consumed and uncertain."],
            intent, existing,
        )
    try:
        _validate_broker_order(order, intent, payload)
    except Exception as error:
        return UncertainReconciliationResult(
            "CONFLICT", False, True,
            [f"Broker order failed exact validation ({type(error).__name__})."], intent, existing,
        )

    try:
        if existing is None:
            record = tracker.register_submitted(order, intent.ticker, expected_shape=payload)
        else:
            if tracker.record is not existing:
                raise RuntimeError("Unverified legacy tracker record is not the current record.")
            lifecycle = tracker.reconcile(lambda _broker_id: order)
            if not lifecycle.reconciled:
                raise RuntimeError("Legacy tracker lifecycle reconciliation failed.")
            record = tracker.verify_legacy_order_shape(existing, order, intent.ticker, payload)
    except Exception as error:
        return UncertainReconciliationResult(
            "BLOCKED", False, True,
            [f"Tracker persistence failed; journal remains uncertain ({type(error).__name__})."],
            intent, existing,
        )
    try:
        journal.update(
            intent, "BROKER_RECORDED", current.isoformat(),
            broker_order_id=record.broker_order_id,
        )
    except Exception as error:
        return UncertainReconciliationResult(
            "BLOCKED", False, True,
            [f"Tracker persisted but journal repair failed ({type(error).__name__})."], intent, record,
        )
    return UncertainReconciliationResult("RECONCILED", True, True, [], intent, record)
