"""Fail-closed paper exit-monitor health supervision for a future runner."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from exit_monitor import is_option_position
from paper_entry_order_tracker import PaperEntryOrderTracker
from supervised_paper_entry_handoff import SubmissionIntentJournal


HEALTH_VERSION = 1
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 60.0


@dataclass(frozen=True)
class ExitMonitorHealthRecord:
    paper_only: bool
    cycle_started_at: str
    cycle_ended_at: str
    cycle_succeeded: bool
    monitored_symbols: list[str]
    blocking_reasons: list[str]
    heartbeat_at: str | None


@dataclass(frozen=True)
class ExitMonitorSupervisionResult:
    status: str
    ready: bool
    reasons: list[str]
    record: ExitMonitorHealthRecord | None


def _value(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime.")
    return value


def _timestamp(value: Any, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing or malformed.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} is malformed.") from error
    return value.strip(), _aware(parsed, field)


def _strict_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} contains a malformed value.")
        result.append(item.strip())
    return result


def _symbols(value: Any, field: str) -> list[str]:
    symbols = [item.upper() for item in _strict_strings(value, field)]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"{field} contains duplicate symbols.")
    return sorted(symbols)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_health(path: Path) -> ExitMonitorHealthRecord | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(data, dict) or set(data) != {"version", "record"}:
            raise ValueError("Health state schema is malformed.")
        if data["version"] != HEALTH_VERSION or not isinstance(data["record"], dict):
            raise ValueError("Health state version or record is invalid.")
        expected = {
            "paper_only", "cycle_started_at", "cycle_ended_at", "cycle_succeeded",
            "monitored_symbols", "blocking_reasons", "heartbeat_at",
        }
        raw = data["record"]
        if set(raw) != expected or raw["paper_only"] is not True or not isinstance(raw["cycle_succeeded"], bool):
            raise ValueError("Health record schema is malformed.")
        started_text, started = _timestamp(raw["cycle_started_at"], "Cycle start")
        ended_text, ended = _timestamp(raw["cycle_ended_at"], "Cycle end")
        if ended < started:
            raise ValueError("Cycle end precedes cycle start.")
        heartbeat = raw["heartbeat_at"]
        heartbeat_text = None
        if heartbeat is not None:
            heartbeat_text, heartbeat_time = _timestamp(heartbeat, "Heartbeat")
            if heartbeat_time > ended:
                raise ValueError("Heartbeat is later than its recorded cycle.")
        return ExitMonitorHealthRecord(
            True, started_text, ended_text, raw["cycle_succeeded"],
            _symbols(raw["monitored_symbols"], "Monitored symbols"),
            _strict_strings(raw["blocking_reasons"], "Blocking reasons"),
            heartbeat_text,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError("Exit-monitor health state is unreadable; entry must remain blocked.") from error


def load_exit_monitor_health(path: Path) -> ExitMonitorHealthRecord | None:
    """Strict public loader for startup diagnostics; never repairs state."""
    return _load_health(Path(path))


def _write_temporary(handle: Any, serialized: str) -> None:
    handle.write(serialized)


def _flush_temporary(handle: Any) -> None:
    handle.flush()


def _fsync_temporary(handle: Any) -> None:
    os.fsync(handle.fileno())


def _save_health(path: Path, record: ExitMonitorHealthRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(
        {"version": HEALTH_VERSION, "record": asdict(record)}, indent=2, sort_keys=True
    ) + "\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            _write_temporary(handle, serialized)
            _flush_temporary(handle)
            _fsync_temporary(handle)
        temporary.replace(path)
    except Exception as error:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("Exit-monitor health persistence failed.") from error


def _broker_option_positions(raw_positions: Any) -> dict[str, float]:
    if isinstance(raw_positions, (str, bytes, dict)):
        raise ValueError("Broker positions response is malformed.")
    positions: dict[str, float] = {}
    for position in list(raw_positions):
        if not is_option_position(position):
            continue
        symbol = str(_value(position, "symbol", "")).strip().upper()
        side = str(_value(position, "side", "long")).strip().lower()
        try:
            quantity = abs(float(_value(position, "qty")))
        except (TypeError, ValueError) as error:
            raise ValueError("Broker option quantity is malformed.") from error
        if not symbol or side != "long" or not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Broker option position is malformed.")
        if symbol in positions:
            raise ValueError("Broker returned duplicate option positions.")
        positions[symbol] = quantity
    return positions


def supervise_exit_monitor(
    *,
    health_path: Path,
    journal_path: Path,
    tracker_path: Path,
    retrieve_broker_positions: Callable[[], Any],
    run_monitor_cycle: Callable[..., Any],
    now: datetime | None = None,
    heartbeat_max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
) -> ExitMonitorSupervisionResult:
    """Run one fresh paper monitor cycle and persist its fail-closed health result."""
    current = _aware(now or datetime.now(timezone.utc), "Supervisor time")
    health_path = Path(health_path)
    _load_health(health_path)  # Validate history, but never use it as startup readiness.
    started = current.isoformat()
    reasons: list[str] = []
    monitored_symbols: list[str] = []
    heartbeat_text: str | None = None
    heartbeat_time: datetime | None = None
    cycle_succeeded = False

    try:
        journal = SubmissionIntentJournal(Path(journal_path), now=current)
        tracker = PaperEntryOrderTracker(Path(tracker_path))
        if any(intent.status == "SUBMISSION_UNCERTAIN" for intent in journal.intents):
            reasons.append("An unresolved SUBMISSION_UNCERTAIN entry exists.")
        if any(not record.terminal for record in tracker.records):
            reasons.append("A pending or partially submitted entry order exists.")

        raw_positions = list(retrieve_broker_positions())
        broker_positions = _broker_option_positions(raw_positions)
        cycle_result = run_monitor_cycle(
            now=current, positions_snapshot=raw_positions
        )
        if _value(cycle_result, "success") is not True:
            reasons.append("The fresh exit-monitor cycle did not report success.")
        else:
            cycle_succeeded = True
        monitored_symbols = _symbols(
            _value(cycle_result, "monitored_symbols"), "Cycle monitored symbols"
        )
        failed_actions = _strict_strings(
            _value(cycle_result, "failed_actions", []), "Failed exit actions"
        )
        cycle_reasons = _strict_strings(
            _value(cycle_result, "blocking_reasons", []), "Cycle blocking reasons"
        )
        if failed_actions:
            reasons.append("The exit-monitor cycle reported failed exit actions.")
        reasons.extend(cycle_reasons)
        heartbeat_text, heartbeat_time = _timestamp(
            _value(cycle_result, "heartbeat_at"), "Cycle heartbeat"
        )
        observed_end = max(current, datetime.now(timezone.utc))
        age = (observed_end - heartbeat_time).total_seconds()
        if age < 0 or age > heartbeat_max_age_seconds:
            reasons.append("The exit-monitor heartbeat is stale or future-dated.")

        exposure_records = [
            record for record in tracker.records
            if record.position_exposure_exists or record.requires_exit_monitor_handoff
        ]
        exposure_symbols: dict[str, float] = {}
        for record in exposure_records:
            symbol = record.option_symbol.strip().upper()
            if not symbol or symbol in exposure_symbols:
                reasons.append("Paper tracker contains duplicate or malformed exposure records.")
                continue
            exposure_symbols[symbol] = record.filled_quantity
        for symbol, quantity in exposure_symbols.items():
            if symbol not in broker_positions:
                reasons.append(f"Tracked exposure {symbol} is missing from broker positions.")
            elif broker_positions[symbol] != quantity:
                reasons.append(f"Tracked and broker quantities disagree for {symbol}.")
            if symbol not in monitored_symbols:
                reasons.append(f"Tracked exposure {symbol} was not handled by the monitor cycle.")
        for symbol in broker_positions:
            if symbol not in exposure_symbols:
                reasons.append(f"Broker option position {symbol} is outside CTS paper tracking.")
            if symbol not in monitored_symbols:
                reasons.append(f"Broker option position {symbol} was not handled by the monitor cycle.")
        for symbol in monitored_symbols:
            if symbol not in broker_positions:
                reasons.append(f"Monitor reported symbol {symbol} without a broker option position.")
    except Exception as error:
        reasons.append(f"Exit-monitor supervision failed closed ({type(error).__name__}).")

    ended_time = max(
        current,
        datetime.now(timezone.utc),
        heartbeat_time or current,
    )
    ended = ended_time.isoformat()
    record = ExitMonitorHealthRecord(
        paper_only=True,
        cycle_started_at=started,
        cycle_ended_at=ended,
        cycle_succeeded=cycle_succeeded,
        monitored_symbols=monitored_symbols,
        blocking_reasons=reasons,
        heartbeat_at=heartbeat_text,
    )
    try:
        _save_health(health_path, record)
    except RuntimeError as error:
        return ExitMonitorSupervisionResult("BLOCKED", False, [str(error)], None)
    ready = not reasons and record.cycle_succeeded
    return ExitMonitorSupervisionResult("READY" if ready else "BLOCKED", ready, reasons, record)
