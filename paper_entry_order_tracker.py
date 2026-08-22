"""Persistent, read-only reconciliation for submitted paper entry orders."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("America/New_York")
ACTIVE_STATUSES = {"accepted", "pending_new", "new", "partially_filled", "pending_cancel"}
TERMINAL_SUCCESS_STATUSES = {"filled"}
TERMINAL_FAILURE_STATUSES = {
    "rejected", "canceled", "expired", "done_for_day", "replaced", "suspended",
}
CONSERVATIVE_NONTERMINAL_STATUSES = {"pending_replace", "stopped", "calculated"}
TRACKER_VERSION = 3
LEGACY_TRACKER_VERSION = 2


@dataclass(frozen=True)
class StatusClassification:
    normalized_status: str
    terminal: bool
    outcome: str
    blocking_reason: str | None


@dataclass
class PaperEntryOrderRecord:
    paper_only: bool
    side: str
    order_type: str
    time_in_force: str
    position_intent: str
    order_shape_verified: bool
    trading_date: str
    broker_order_id: str
    client_order_id: str
    option_symbol: str
    underlying_ticker: str
    requested_quantity: float
    filled_quantity: float
    limit_price: float
    average_fill_price: float | None
    normalized_status: str
    submitted_at: str
    updated_at: str
    filled_at: str | None
    terminal: bool
    outcome: str
    blocking_reason: str | None
    position_exposure_exists: bool
    requires_exit_monitor_handoff: bool


@dataclass(frozen=True)
class ReconciliationResult:
    record: PaperEntryOrderRecord
    changed: bool
    reconciled: bool


def _value(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _required_text(value: Any, field_name: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"{field_name} cannot be blank.")
    return text


def _quantity(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be numeric.") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return number


def _optional_price(value: Any) -> float | None:
    if value is None or not _text(value):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Average fill price must be numeric.") from error
    if not math.isfinite(price) or price < 0:
        raise ValueError("Average fill price cannot be negative.")
    return price


def _timestamp(value: Any, field_name: str) -> tuple[str, datetime]:
    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} is invalid.") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return text, parsed


def _optional_timestamp(value: Any, field_name: str) -> tuple[str | None, datetime | None]:
    return (None, None) if not _text(value) else _timestamp(value, field_name)


def eastern_trading_date(value: datetime | date | None = None) -> date:
    value = value or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Allowance time must include a timezone.")
        return value.astimezone(MARKET_TIMEZONE).date()
    return value


def normalize_status(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def classify_status(value: Any) -> StatusClassification:
    status = normalize_status(value)
    if status in ACTIVE_STATUSES:
        return StatusClassification(status, False, "active", None)
    if status in TERMINAL_SUCCESS_STATUSES:
        return StatusClassification(status, True, "success", None)
    if status in TERMINAL_FAILURE_STATUSES:
        return StatusClassification(status, True, "failure", f"Entry order ended with broker status {status}.")
    if status in CONSERVATIVE_NONTERMINAL_STATUSES:
        return StatusClassification(status, False, "blocked", f"Entry order status {status} requires manual review.")
    label = status or "missing"
    return StatusClassification(status or "unknown", True, "failure", f"Unknown or ambiguous broker order status: {label}.")


class PaperEntryOrderTracker:
    """Tracks paper orders without exposing any order mutation operation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.records = self._load()

    @property
    def record(self) -> PaperEntryOrderRecord | None:
        return self.records[-1] if self.records else None

    def _load(self) -> list[PaperEntryOrderRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
            version = data.get("version")
            if version not in {LEGACY_TRACKER_VERSION, TRACKER_VERSION}:
                raise ValueError("invalid tracker state")
            if not isinstance(data.get("orders"), list):
                raise ValueError("tracker records are malformed")
            records = []
            for raw in data["orders"]:
                if not isinstance(raw, dict):
                    raise ValueError("tracker record is malformed")
                item = dict(raw)
                if version == LEGACY_TRACKER_VERSION:
                    for field in ("side", "order_type", "time_in_force", "position_intent"):
                        item[field] = "unknown"
                    item["order_shape_verified"] = False
                record = PaperEntryOrderRecord(**item)
                if record.order_shape_verified:
                    if (
                        record.side != "buy"
                        or record.order_type != "limit"
                        or record.time_in_force != "day"
                        or record.position_intent != "buy_to_open"
                    ):
                        raise ValueError("verified tracker order shape is invalid")
                elif any(
                    value != "unknown"
                    for value in (
                        record.side, record.order_type, record.time_in_force,
                        record.position_intent,
                    )
                ):
                    raise ValueError("unverified tracker order shape is ambiguous")
                records.append(record)
            if any(item.paper_only is not True for item in records):
                raise ValueError("non-paper order state")
            broker_ids = [item.broker_order_id for item in records]
            client_ids = [item.client_order_id for item in records]
            if len(set(broker_ids)) != len(broker_ids) or len(set(client_ids)) != len(client_ids):
                raise ValueError("duplicate tracker identity")
            return records
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise RuntimeError("Paper entry order state is unreadable; new entries must remain blocked.") from error

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        serialized = json.dumps(
            {"version": TRACKER_VERSION, "orders": [asdict(item) for item in self.records]},
            indent=2,
            sort_keys=True,
        ) + "\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except Exception as error:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("Paper entry order state atomic write failed.") from error

    def new_entry_allowed(self, on_date: datetime | date | None = None) -> bool:
        session_date = eastern_trading_date(on_date).isoformat()
        if any(not item.terminal for item in self.records):
            return False
        return not any(item.trading_date == session_date for item in self.records)

    def entry_blocking_reason(self, on_date: datetime | date | None = None) -> str | None:
        session_date = eastern_trading_date(on_date).isoformat()
        if any(not item.terminal for item in self.records):
            return "An entry order is pending or partially filled."
        same_day = next((item for item in reversed(self.records) if item.trading_date == session_date), None)
        if same_day is None:
            return None
        return "The paper-entry allowance for this trading date has already been consumed."

    def register_submitted(
        self,
        order: Any,
        underlying_ticker: str,
        expected_shape: Any | None = None,
    ) -> PaperEntryOrderRecord:
        submitted_at, submitted_time = _timestamp(_value(order, "submitted_at"), "Submitted timestamp")
        trading_date = submitted_time.astimezone(MARKET_TIMEZONE).date()
        if not self.new_entry_allowed(trading_date):
            raise RuntimeError(self.entry_blocking_reason(trading_date))
        classification = classify_status(_value(order, "status"))
        requested = _quantity(_value(order, "qty"), "Requested quantity")
        filled = _quantity(_value(order, "filled_qty", 0), "Filled quantity")
        if requested <= 0 or filled > requested:
            raise ValueError("Broker order quantities are inconsistent.")
        limit_price = _optional_price(_value(order, "limit_price"))
        if limit_price is None or limit_price <= 0:
            raise ValueError("Limit price must be positive.")
        shape_source = expected_shape if expected_shape is not None else order
        side = normalize_status(_value(shape_source, "side"))
        order_type = normalize_status(_value(shape_source, "order_type", _value(shape_source, "type")))
        time_in_force = normalize_status(_value(shape_source, "time_in_force"))
        position_intent = normalize_status(_value(shape_source, "position_intent"))
        if (side, order_type, time_in_force, position_intent) != (
            "buy", "limit", "day", "buy_to_open",
        ):
            raise ValueError("Order shape must be BUY/LIMIT/DAY/BUY_TO_OPEN.")
        for field, expected in (
            ("side", side), ("order_type", order_type),
            ("time_in_force", time_in_force), ("position_intent", position_intent),
        ):
            response_name = "type" if field == "order_type" else field
            response_value = _value(order, response_name)
            if response_value is not None and normalize_status(response_value) != expected:
                raise ValueError(f"Broker {field} conflicts with the verified order shape.")
        updated_at, _ = _timestamp(_value(order, "updated_at", submitted_at), "Updated timestamp")
        filled_at, _ = _optional_timestamp(_value(order, "filled_at"), "Filled timestamp")
        exposure = filled > 0
        outcome = classification.outcome
        reason = classification.blocking_reason
        if exposure and classification.terminal and outcome != "success":
            outcome = "failure_with_exposure"
            reason = f"{reason or 'Entry order ended unsuccessfully.'} A partial position exists and requires exit-monitor handoff."
        record = PaperEntryOrderRecord(
            True, side, order_type, time_in_force, position_intent, True,
            trading_date.isoformat(), _required_text(_value(order, "id"), "Broker order ID"),
            _required_text(_value(order, "client_order_id"), "Client order ID"),
            _required_text(_value(order, "symbol"), "Option symbol").upper(),
            _required_text(underlying_ticker, "Underlying ticker").upper(), requested, filled,
            limit_price, _optional_price(_value(order, "filled_avg_price")), classification.normalized_status,
            submitted_at, updated_at, filled_at, classification.terminal, outcome, reason,
            exposure, exposure and classification.terminal,
        )
        self.records.append(record)
        try:
            self._save()
        except Exception:
            self.records.pop()
            raise
        return record

    def verify_legacy_order_shape(
        self,
        record: PaperEntryOrderRecord,
        order: Any,
        underlying_ticker: str,
        expected_shape: Any,
    ) -> PaperEntryOrderRecord:
        """Persist broker-proven shape proof for one otherwise exact legacy record."""
        if record not in self.records or record.order_shape_verified:
            raise RuntimeError("Only an existing unverified tracker record may be verified.")
        side = normalize_status(_value(expected_shape, "side"))
        order_type = normalize_status(_value(expected_shape, "order_type"))
        time_in_force = normalize_status(_value(expected_shape, "time_in_force"))
        position_intent = normalize_status(_value(expected_shape, "position_intent"))
        if (side, order_type, time_in_force, position_intent) != (
            "buy", "limit", "day", "buy_to_open",
        ):
            raise ValueError("Verified legacy shape must be BUY/LIMIT/DAY/BUY_TO_OPEN.")
        if (
            _required_text(_value(order, "id"), "Broker order ID") != record.broker_order_id
            or _required_text(_value(order, "client_order_id"), "Client order ID") != record.client_order_id
            or _required_text(_value(order, "symbol"), "Option symbol").upper() != record.option_symbol
            or _required_text(underlying_ticker, "Underlying ticker").upper() != record.underlying_ticker
            or _quantity(_value(order, "qty"), "Requested quantity") != record.requested_quantity
            or _optional_price(_value(order, "limit_price")) != record.limit_price
        ):
            raise ValueError("Broker order does not match the legacy tracker record.")
        for name, expected in (
            ("side", side), ("type", order_type), ("time_in_force", time_in_force),
        ):
            if normalize_status(_value(order, name)) != expected:
                raise ValueError("Broker order shape does not verify the legacy record.")
        broker_position_intent = _value(order, "position_intent")
        if broker_position_intent is not None and normalize_status(broker_position_intent) != position_intent:
            raise ValueError("Broker position intent conflicts with the legacy record.")
        previous = asdict(record)
        record.side = side
        record.order_type = order_type
        record.time_in_force = time_in_force
        record.position_intent = position_intent
        record.order_shape_verified = True
        try:
            self._save()
        except Exception:
            for field, value in previous.items():
                setattr(record, field, value)
            raise
        return record

    def reconcile(self, retrieve_order: Callable[[str], Any]) -> ReconciliationResult:
        record = self.record
        if record is None:
            raise RuntimeError("No submitted paper entry order is registered.")
        previous = asdict(record)
        try:
            order = retrieve_order(record.broker_order_id)
            if _required_text(_value(order, "id"), "Broker order ID") != record.broker_order_id:
                raise ValueError("Retrieved broker order ID does not match.")
            classification = classify_status(_value(order, "status"))
            incoming_filled = _quantity(_value(order, "filled_qty", 0), "Filled quantity")
            if incoming_filled > record.requested_quantity:
                raise ValueError("Filled quantity exceeds requested quantity.")
            updated_text, updated_time = _timestamp(_value(order, "updated_at"), "Updated timestamp")
            _, current_updated = _timestamp(record.updated_at, "Stored updated timestamp")
            filled_text, filled_time = _optional_timestamp(_value(order, "filled_at"), "Filled timestamp")
            _, current_filled = _optional_timestamp(record.filled_at, "Stored filled timestamp")

            if incoming_filled > record.filled_quantity:
                record.filled_quantity = incoming_filled
                price = _optional_price(_value(order, "filled_avg_price"))
                if price is not None:
                    record.average_fill_price = price
            elif incoming_filled == record.filled_quantity and incoming_filled > 0 and updated_time >= current_updated:
                price = _optional_price(_value(order, "filled_avg_price"))
                if price is not None:
                    record.average_fill_price = price
            record.position_exposure_exists = record.filled_quantity > 0

            if updated_time >= current_updated:
                record.updated_at = updated_text
                if not record.terminal:
                    record.normalized_status = classification.normalized_status
                    record.terminal = classification.terminal
                    record.outcome = classification.outcome
                    record.blocking_reason = classification.blocking_reason
            if filled_time is not None and (current_filled is None or filled_time >= current_filled):
                record.filled_at = filled_text

            if record.position_exposure_exists and record.terminal and record.outcome != "success":
                record.outcome = "failure_with_exposure"
                record.requires_exit_monitor_handoff = True
                if not record.blocking_reason or "exit-monitor handoff" not in record.blocking_reason:
                    record.blocking_reason = f"{record.blocking_reason or 'Entry order ended unsuccessfully.'} A partial position exists and requires exit-monitor handoff."
            elif record.terminal and record.outcome == "success":
                record.requires_exit_monitor_handoff = record.position_exposure_exists
        except Exception:
            self.records[-1] = PaperEntryOrderRecord(**previous)
            return ReconciliationResult(self.records[-1], False, False)

        changed = asdict(record) != previous
        if changed:
            try:
                self._save()
            except Exception:
                self.records[-1] = PaperEntryOrderRecord(**previous)
                return ReconciliationResult(self.records[-1], False, False)
        return ReconciliationResult(record, changed, True)
