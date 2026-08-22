"""Crash-safe supervised handoff from an approved CTS preview to paper submission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
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
from paper_entry_service import P_L_TIMESTAMP_AGE_SECONDS, PaperEntryReadinessReport
from paper_execution_service import ENABLE_VALUE, build_paper_entry_payload
from paper_trial_preflight import PaperTrialPreflightResult


CORE_ORIGIN = "CORE_CTS"
MAX_CONTRACT_COST = 150.0


@dataclass
class SubmissionIntent:
    paper_only: bool
    trading_date: str
    client_order_id: str
    ticker: str
    option_symbol: str
    quantity: int
    limit_price: float
    status: str
    created_at: str
    updated_at: str
    broker_order_id: str | None = None
    blocking_reason: str | None = None


@dataclass(frozen=True)
class HandoffResult:
    status: str
    submitted: bool
    client_order_id: str | None
    reasons: list[str]
    intent: SubmissionIntent | None = None
    order_record: PaperEntryOrderRecord | None = None


def _value(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone.")
    return value


def deterministic_client_order_id(
    preview: PaperOrderPreview,
    trading_date: str,
) -> str:
    try:
        normalized_date = date.fromisoformat(trading_date).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError("Trading date must be an ISO calendar date.") from error
    canonical = "|".join(
        (
            "cts-supervised-paper-v1",
            normalized_date,
            preview.ticker.strip().upper(),
            preview.contract_symbol.strip().upper(),
            preview.side.strip().upper(),
            str(preview.quantity),
            preview.order_type.strip().upper(),
            preview.time_in_force.strip().upper(),
            f"{preview.limit_price:.8f}",
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"cts-paper-{normalized_date.replace('-', '')}-{digest}"


class SubmissionIntentJournal:
    def __init__(self, path: Path, now: datetime | None = None) -> None:
        self.path = Path(path)
        self.intents = self._load()
        interrupted = False
        recovery_time = _aware(now or datetime.now(timezone.utc), "Recovery time").isoformat()
        for intent in self.intents:
            if intent.status == "INTENT_PERSISTED":
                intent.status = "SUBMISSION_UNCERTAIN"
                intent.updated_at = recovery_time
                intent.blocking_reason = (
                    "A persisted submission intent was recovered after interruption; "
                    "read-only broker reconciliation is required."
                )
                interrupted = True
        if interrupted:
            self._save()

    def _load(self) -> list[SubmissionIntent]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError("invalid intent journal version")
            intents = [SubmissionIntent(**item) for item in data["intents"]]
            if any(item.paper_only is not True for item in intents):
                raise ValueError("non-paper intent")
            return intents
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise RuntimeError(
                "Submission intent journal is unreadable; submission must remain blocked."
            ) from error

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "intents": [asdict(item) for item in self.intents]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def blocking_reason(self, trading_date: str, client_order_id: str) -> str | None:
        if any(item.status == "SUBMISSION_UNCERTAIN" for item in self.intents):
            return "An unresolved submission intent requires read-only reconciliation."
        if any(item.client_order_id == client_order_id for item in self.intents):
            return "This deterministic submission request already exists."
        if any(item.trading_date == trading_date for item in self.intents):
            return "The supervised paper-entry allowance is consumed for this trading date."
        return None

    def persist(self, intent: SubmissionIntent) -> None:
        reason = self.blocking_reason(intent.trading_date, intent.client_order_id)
        if reason:
            raise RuntimeError(reason)
        self.intents.append(intent)
        self._save()

    def update(
        self,
        intent: SubmissionIntent,
        status: str,
        updated_at: str,
        broker_order_id: str | None = None,
        blocking_reason: str | None = None,
    ) -> None:
        intent.status = status
        intent.updated_at = updated_at
        intent.broker_order_id = broker_order_id
        intent.blocking_reason = blocking_reason
        self._save()


def _gate_failures(
    readiness: PaperEntryReadinessReport,
    preview: PaperOrderPreview,
    preview_created_at: datetime,
    preflight: PaperTrialPreflightResult,
    execution_enable_value: str,
    paper_configuration_confirmed: bool,
    origin: str,
    tracker: PaperEntryOrderTracker,
    journal: SubmissionIntentJournal,
    now: datetime,
) -> tuple[list[str], str, str]:
    reasons: list[str] = []
    now = _aware(now, "Handoff time")
    preview_created_at = _aware(preview_created_at, "Preview timestamp")
    trading_date = eastern_trading_date(now).isoformat()
    client_order_id = deterministic_client_order_id(preview, trading_date)

    if origin != CORE_ORIGIN:
        reasons.append("Only approved core CTS entries are accepted; catalyst origin is refused.")
    if preflight.status != "READY":
        reasons.append("Paper trial preflight is not READY.")
    if not paper_configuration_confirmed or not preflight.paper_configuration_verified:
        reasons.append("ALPACA_PAPER is not independently confirmed true.")
    broker = preflight.broker_readiness
    if broker is None or broker.paper_mode is not True or broker.status != "PASS":
        reasons.append("Broker readiness does not independently confirm paper mode.")
    if not preflight.paper_mode_verified:
        reasons.append("Preflight did not verify paper mode.")
    if execution_enable_value != ENABLE_VALUE or not readiness.execution_enabled:
        reasons.append("The exact paper-only execution enable value is not active.")

    if readiness.status != "PASS" or not readiness.allowed:
        reasons.append("Core CTS entry readiness did not pass.")
    readiness_broker = getattr(readiness, "broker_readiness", None)
    if readiness_broker is None or (
        readiness_broker.status != "PASS" or readiness_broker.paper_mode is not True
    ):
        reasons.append("Entry readiness broker gate did not confirm paper readiness.")
    scanner = getattr(readiness, "scanner_candidate", None)
    if scanner is None or not callable(getattr(scanner, "technical_candidate", None)) or (
        not scanner.technical_candidate()
    ):
        reasons.append("CTS scanner gate did not pass.")
    option_liquidity = getattr(readiness, "option_liquidity", None)
    if option_liquidity is None or not option_liquidity.acceptable:
        reasons.append("Option-liquidity gate did not pass.")
    trade_plan = getattr(readiness, "trade_plan", None)
    if trade_plan is None or not trade_plan.acceptable:
        reasons.append("Risk-plan gate did not pass.")
    news_risk = getattr(readiness, "news_risk", None)
    if news_risk is None or news_risk.status != "PASS":
        reasons.append("News-risk gate did not pass.")
    earnings_risk = getattr(readiness, "earnings_risk", None)
    if earnings_risk is None or earnings_risk.status != "PASS":
        reasons.append("Earnings-risk gate did not pass.")
    if getattr(readiness, "duplicate_contract", True):
        reasons.append("Entry readiness reports a duplicate contract.")
    if readiness.order_preview is None or readiness.order_preview != preview:
        reasons.append("Preview is not the approved readiness preview.")
    if readiness.final_decision is None or (
        readiness.final_decision.status != "PASS"
        or not readiness.final_decision.automatic_paper_eligible
    ):
        reasons.append("Final CTS decision did not pass.")
    if readiness.market_session is None or (
        readiness.market_session.status != "PASS"
        or not readiness.market_session.entry_allowed
    ):
        reasons.append("Market-session gate did not pass.")
    if readiness.daily_limits is None or (
        readiness.daily_limits.status != "PASS"
        or not readiness.daily_limits.new_trade_allowed
        or readiness.daily_limits.trades_opened_today != 0
        or readiness.daily_limits.open_positions != 0
    ):
        reasons.append("One-trade/one-position handoff limits did not pass.")
    if preflight.trial_limits is None or (
        preflight.trial_limits.max_trades_per_day != 1
        or preflight.trial_limits.max_open_positions != 1
    ):
        reasons.append("Preflight trial limits are not exactly one trade and one position.")
    if broker is not None and (broker.open_positions != 0 or broker.open_orders != 0):
        reasons.append("Broker reports an existing order or position.")

    age = (now - preview_created_at).total_seconds()
    if eastern_trading_date(preview_created_at) != eastern_trading_date(now) or not (
        0 <= age <= P_L_TIMESTAMP_AGE_SECONDS
    ):
        reasons.append("Approved preview is stale or from another Eastern trading date.")
    if not preview.eligible:
        reasons.append("Paper-order preview is not eligible.")
    if preview.side != "BUY" or preview.order_type != "LIMIT" or preview.time_in_force != "DAY":
        reasons.append("Preview must be BUY, LIMIT, and DAY.")
    if isinstance(preview.quantity, bool) or not isinstance(preview.quantity, int) or preview.quantity <= 0:
        reasons.append("Preview quantity must be a positive whole number.")
    if preview.limit_price <= 0 or preview.estimated_cost > MAX_CONTRACT_COST or (
        preview.limit_price * preview.quantity * 100 > MAX_CONTRACT_COST
    ):
        reasons.append("Preview exceeds the $150 contract-cost safety cap.")
    try:
        payload = build_paper_entry_payload(preview, client_order_id)
        if (
            payload.side != "buy"
            or payload.order_type != "limit"
            or payload.time_in_force != "day"
            or payload.position_intent != "buy_to_open"
            or payload.extended_hours
        ):
            reasons.append("Execution payload is not BUY/LIMIT/DAY/BUY_TO_OPEN paper-safe.")
    except (RuntimeError, TypeError, ValueError) as error:
        reasons.append(f"Execution payload validation failed: {error}")

    if not tracker.new_entry_allowed(now):
        reasons.append(tracker.entry_blocking_reason(now) or "Entry tracker blocks submission.")
    journal_reason = journal.blocking_reason(trading_date, client_order_id)
    if journal_reason:
        reasons.append(journal_reason)
    return reasons, trading_date, client_order_id


def submit_supervised_paper_entry(
    *,
    readiness: PaperEntryReadinessReport,
    preview: PaperOrderPreview,
    preview_created_at: datetime,
    preflight: PaperTrialPreflightResult,
    execution_enable_value: str,
    paper_configuration_confirmed: bool,
    origin: str,
    tracker: PaperEntryOrderTracker,
    journal: SubmissionIntentJournal,
    submitter: Callable[..., Any],
    now: datetime | None = None,
) -> HandoffResult:
    now = _aware(now or datetime.now(timezone.utc), "Handoff time")
    reasons, trading_date, client_order_id = _gate_failures(
        readiness, preview, preview_created_at, preflight, execution_enable_value,
        paper_configuration_confirmed, origin, tracker, journal, now,
    )
    if reasons:
        return HandoffResult("BLOCKED", False, client_order_id, reasons)

    timestamp = now.isoformat()
    intent = SubmissionIntent(
        paper_only=True,
        trading_date=trading_date,
        client_order_id=client_order_id,
        ticker=preview.ticker,
        option_symbol=preview.contract_symbol,
        quantity=preview.quantity,
        limit_price=preview.limit_price,
        status="INTENT_PERSISTED",
        created_at=timestamp,
        updated_at=timestamp,
    )
    journal.persist(intent)

    try:
        response = submitter(preview=preview, client_order_id=client_order_id)
        broker_id = str(_value(response, "id", "") or "").strip()
        response_client_id = str(_value(response, "client_order_id", "") or "").strip()
        response_symbol = str(_value(response, "symbol", "") or "").strip().upper()
        response_status = normalize_status(_value(response, "status", ""))
        usable_statuses = (
            ACTIVE_STATUSES
            | TERMINAL_SUCCESS_STATUSES
            | TERMINAL_FAILURE_STATUSES
            | CONSERVATIVE_NONTERMINAL_STATUSES
        )
        if (
            not broker_id
            or response_client_id != client_order_id
            or response_symbol != preview.contract_symbol.upper()
            or response_status not in usable_statuses
        ):
            raise ValueError("Broker submission response is incomplete or mismatched.")
        record = tracker.register_submitted(response, preview.ticker)
    except Exception as error:
        journal.update(
            intent,
            "SUBMISSION_UNCERTAIN",
            datetime.now(timezone.utc).isoformat(),
            blocking_reason=(
                "Paper submission outcome is uncertain; read-only reconciliation by "
                f"client order ID {client_order_id} is required ({type(error).__name__})."
            ),
        )
        return HandoffResult(
            "SUBMISSION_UNCERTAIN", False, client_order_id,
            [intent.blocking_reason or "Submission outcome is uncertain."], intent,
        )

    journal.update(
        intent,
        "BROKER_RECORDED",
        datetime.now(timezone.utc).isoformat(),
        broker_order_id=record.broker_order_id,
    )
    return HandoffResult("SUBMITTED", True, client_order_id, [], intent, record)
