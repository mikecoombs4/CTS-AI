"""POSIX runner lock and restart-safe offline scan/decision ledger."""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from core_candidate_selector import (
    TICKER_PATTERN,
    decision_identity,
    validate_completed_bar,
)


LEDGER_VERSION = 1
MARKET_TIMEZONE = ZoneInfo("America/New_York")
CYCLE_TERMINAL_OUTCOMES = {"SELECTED", "NO_SELECTION", "BLOCKED", "ERROR"}
DECISION_TERMINAL_OUTCOMES = {"ELIGIBLE", "EXCLUDED", "BLOCKED", "ERROR"}
DECISION_ID_PATTERN = re.compile(r"^core-cts-[a-z0-9.-]{1,10}-[0-9a-f]{24}$", re.ASCII)


class RunnerLockUnavailable(RuntimeError):
    pass


class SingleRunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    @property
    def owned(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> "SingleRunnerLock":
        if self.owned:
            raise RuntimeError("This runner lock instance already owns the lock.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            os.fchmod(descriptor, 0o600)
            _write_lock_metadata(descriptor)
        except OSError as error:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            if not locked and error.errno in {errno.EACCES, errno.EAGAIN}:
                raise RunnerLockUnavailable(
                    "Another paper runner currently owns the operating-system lock."
                ) from error
            raise
        except Exception:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "SingleRunnerLock":
        return self.acquire()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


def _write_lock_metadata(descriptor: int) -> None:
    metadata = f"pid={os.getpid()}\n".encode("ascii")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    written = os.write(descriptor, metadata)
    if written != len(metadata):
        raise OSError("Runner lock metadata write was incomplete.")
    os.fsync(descriptor)


@dataclass(frozen=True)
class CycleRecord:
    interval_start: str
    trading_date: str
    status: str
    claimed_at: str
    completed_at: str | None
    selected_decision_id: str | None


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    ticker: str
    interval_start: str
    status: str
    claimed_at: str
    completed_at: str | None


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")
    return value


def _canonical_timestamp(value: datetime, name: str) -> str:
    return _aware(value, name).astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is missing or malformed.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} is malformed.") from error
    return _aware(parsed, name)


def _strict_keys(value: object, expected: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} has an invalid schema.")
    return value


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key is not allowed: {key}.")
        result[key] = value
    return result


def _validate_cycle(data: object) -> CycleRecord:
    item = _strict_keys(
        data,
        {"interval_start", "trading_date", "status", "claimed_at", "completed_at", "selected_decision_id"},
        "Cycle record",
    )
    interval = _parse_timestamp(item["interval_start"], "Cycle interval")
    claimed = _parse_timestamp(item["claimed_at"], "Cycle claim timestamp")
    status = item["status"]
    if status not in {"CLAIMED"} | CYCLE_TERMINAL_OUTCOMES:
        raise ValueError("Cycle status is invalid.")
    trading_date = item["trading_date"]
    if not isinstance(trading_date, str) or trading_date != interval.astimezone(MARKET_TIMEZONE).date().isoformat():
        raise ValueError("Cycle trading date is invalid.")
    completed = item["completed_at"]
    selected = item["selected_decision_id"]
    if status == "CLAIMED":
        if completed is not None or selected is not None:
            raise ValueError("Claimed cycle contains terminal fields.")
    else:
        completion_time = _parse_timestamp(completed, "Cycle completion timestamp")
        if completion_time < claimed:
            raise ValueError("Cycle completion precedes its claim.")
        if status == "SELECTED":
            if not isinstance(selected, str) or not DECISION_ID_PATTERN.fullmatch(selected):
                raise ValueError("Selected cycle lacks a valid decision identity.")
        elif selected is not None:
            raise ValueError("Non-selected cycle contains a decision identity.")
    return CycleRecord(
        _canonical_timestamp(interval, "Cycle interval"),
        trading_date,
        status,
        _canonical_timestamp(claimed, "Cycle claim timestamp"),
        _canonical_timestamp(_parse_timestamp(completed, "Cycle completion timestamp"), "Cycle completion timestamp") if completed else None,
        selected,
    )


def _validate_decision(data: object) -> DecisionRecord:
    item = _strict_keys(
        data,
        {"decision_id", "ticker", "interval_start", "status", "claimed_at", "completed_at"},
        "Decision record",
    )
    ticker = str(item["ticker"] or "").strip().upper()
    interval = _parse_timestamp(item["interval_start"], "Decision interval")
    decision_id = item["decision_id"]
    if not TICKER_PATTERN.fullmatch(ticker) or not isinstance(decision_id, str) or (
        decision_id != decision_identity(ticker, interval)
    ):
        raise ValueError("Decision identity, ticker, or interval is inconsistent.")
    status = item["status"]
    if status not in {"CLAIMED"} | DECISION_TERMINAL_OUTCOMES:
        raise ValueError("Decision status is invalid.")
    claimed = _parse_timestamp(item["claimed_at"], "Decision claim timestamp")
    completed = item["completed_at"]
    if status == "CLAIMED" and completed is not None:
        raise ValueError("Claimed decision contains a completion timestamp.")
    if status != "CLAIMED":
        completion_time = _parse_timestamp(completed, "Decision completion timestamp")
        if completion_time < claimed:
            raise ValueError("Decision completion precedes its claim.")
    return DecisionRecord(
        decision_id,
        ticker,
        _canonical_timestamp(interval, "Decision interval"),
        status,
        _canonical_timestamp(claimed, "Decision claim timestamp"),
        _canonical_timestamp(_parse_timestamp(completed, "Decision completion timestamp"), "Decision completion timestamp") if completed else None,
    )


def _write_temporary(handle, serialized: str) -> None:
    handle.write(serialized)


def _flush_temporary(handle) -> None:
    handle.flush()


def _fsync_temporary(handle) -> None:
    os.fsync(handle.fileno())


class PaperRunnerLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.cycles: list[CycleRecord] = []
        self.decisions: list[DecisionRecord] = []
        self._reload()

    def _read(self) -> tuple[list[CycleRecord], list[DecisionRecord]]:
        if not self.path.exists():
            return [], []
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
            data = _strict_keys(raw, {"version", "cycles", "decisions"}, "Ledger")
            if data["version"] != LEDGER_VERSION:
                raise ValueError("Ledger version is unknown.")
            if not isinstance(data["cycles"], list) or not isinstance(data["decisions"], list):
                raise ValueError("Ledger audit collections are malformed.")
            cycles = [_validate_cycle(item) for item in data["cycles"]]
            decisions = [_validate_decision(item) for item in data["decisions"]]
            if len({item.interval_start for item in cycles}) != len(cycles):
                raise ValueError("Ledger contains duplicate cycle records.")
            if len({item.decision_id for item in decisions}) != len(decisions):
                raise ValueError("Ledger contains duplicate decision records.")
            cycle_intervals = {item.interval_start for item in cycles}
            if any(item.interval_start not in cycle_intervals for item in decisions):
                raise ValueError("Decision record has no matching cycle.")
            for cycle in cycles:
                related = [
                    item for item in decisions
                    if item.interval_start == cycle.interval_start
                ]
                unresolved = [item for item in related if item.status == "CLAIMED"]
                eligible = [item for item in related if item.status == "ELIGIBLE"]
                if cycle.status == "SELECTED":
                    matches = [
                        item for item in eligible
                        if item.decision_id == cycle.selected_decision_id
                    ]
                    if len(matches) != 1 or unresolved:
                        raise ValueError("Selected cycle decision relationship is invalid.")
                if cycle.status == "NO_SELECTION" and (eligible or unresolved):
                    raise ValueError("No-selection cycle conceals eligible or unresolved decisions.")
            return cycles, decisions
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Paper runner ledger is unreadable or invalid; runner must remain blocked."
            ) from error

    def _reload(self) -> None:
        self.cycles, self.decisions = self._read()

    def _save(self, cycles: list[CycleRecord], decisions: list[DecisionRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        serialized = json.dumps(
            {
                "version": LEDGER_VERSION,
                "cycles": [asdict(item) for item in cycles],
                "decisions": [asdict(item) for item in decisions],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                _write_temporary(handle, serialized)
                _flush_temporary(handle)
                _fsync_temporary(handle)
            temporary.replace(self.path)
        except Exception as error:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("Paper runner ledger atomic write failed.") from error

    def _mutate(self, operation: Callable[[list[CycleRecord], list[DecisionRecord]], object]):
        self._reload()
        cycles = copy.deepcopy(self.cycles)
        decisions = copy.deepcopy(self.decisions)
        result = operation(cycles, decisions)
        self._save(cycles, decisions)
        self.cycles, self.decisions = cycles, decisions
        return result

    def claim_cycle(self, interval_start: datetime, claimed_at: datetime) -> CycleRecord:
        errors = validate_completed_bar(interval_start, claimed_at)
        if errors:
            raise ValueError("; ".join(errors))
        interval = _canonical_timestamp(interval_start, "Cycle interval")
        claimed = _canonical_timestamp(claimed_at, "Cycle claim timestamp")
        trading_date = interval_start.astimezone(MARKET_TIMEZONE).date().isoformat()

        def operation(cycles, _decisions):
            if any(item.interval_start == interval for item in cycles):
                raise RuntimeError("Completed-bar cycle has already been consumed.")
            record = CycleRecord(interval, trading_date, "CLAIMED", claimed, None, None)
            cycles.append(record)
            return record

        return self._mutate(operation)

    def claim_decision(
        self,
        decision_id: str,
        ticker: str,
        interval_start: datetime,
        claimed_at: datetime,
    ) -> DecisionRecord:
        errors = validate_completed_bar(interval_start, claimed_at)
        if errors:
            raise ValueError("; ".join(errors))
        normalized_ticker = str(ticker or "").strip().upper()
        interval = _canonical_timestamp(interval_start, "Decision interval")
        claimed = _canonical_timestamp(claimed_at, "Decision claim timestamp")
        if not isinstance(decision_id, str) or not DECISION_ID_PATTERN.fullmatch(decision_id) or (
            decision_id != decision_identity(normalized_ticker, interval_start)
        ):
            raise ValueError("Decision identity does not match ticker and completed bar.")

        def operation(cycles, decisions):
            cycle = next((item for item in cycles if item.interval_start == interval), None)
            if cycle is None:
                raise RuntimeError("Decision cannot be claimed before its scan cycle.")
            if cycle.status != "CLAIMED":
                raise RuntimeError("No decision may be added after its cycle is terminal.")
            if any(item.decision_id == decision_id for item in decisions):
                raise RuntimeError("Candidate decision has already been consumed.")
            record = DecisionRecord(
                decision_id, normalized_ticker, interval, "CLAIMED", claimed, None
            )
            decisions.append(record)
            return record

        return self._mutate(operation)

    def complete_decision(
        self,
        decision_id: str,
        outcome: str,
        completed_at: datetime,
    ) -> DecisionRecord:
        if outcome not in DECISION_TERMINAL_OUTCOMES:
            raise ValueError("Decision terminal outcome is invalid.")
        completed = _canonical_timestamp(completed_at, "Decision completion timestamp")

        def operation(_cycles, decisions):
            index = next((i for i, item in enumerate(decisions) if item.decision_id == decision_id), None)
            if index is None:
                raise RuntimeError("Candidate decision claim was not found.")
            current = decisions[index]
            if current.status != "CLAIMED":
                if current.status == outcome and current.completed_at == completed:
                    return current
                raise RuntimeError("Terminal decision outcome cannot regress or change.")
            if datetime.fromisoformat(completed) < datetime.fromisoformat(current.claimed_at):
                raise ValueError("Decision completion cannot precede its claim.")
            updated = DecisionRecord(
                current.decision_id, current.ticker, current.interval_start,
                outcome, current.claimed_at, completed,
            )
            decisions[index] = updated
            return updated

        return self._mutate(operation)

    def complete_cycle(
        self,
        interval_start: datetime,
        outcome: str,
        completed_at: datetime,
        selected_decision_id: str | None = None,
    ) -> CycleRecord:
        if outcome not in CYCLE_TERMINAL_OUTCOMES:
            raise ValueError("Cycle terminal outcome is invalid.")
        interval = _canonical_timestamp(interval_start, "Cycle interval")
        completed = _canonical_timestamp(completed_at, "Cycle completion timestamp")
        if outcome == "SELECTED":
            if not isinstance(selected_decision_id, str) or not DECISION_ID_PATTERN.fullmatch(selected_decision_id):
                raise ValueError("SELECTED cycle requires a valid decision identity.")
        elif selected_decision_id is not None:
            raise ValueError("Only SELECTED cycles may store a decision identity.")

        def operation(cycles, decisions):
            index = next((i for i, item in enumerate(cycles) if item.interval_start == interval), None)
            if index is None:
                raise RuntimeError("Scan-cycle claim was not found.")
            current = cycles[index]
            if current.status != "CLAIMED":
                if (
                    current.status == outcome
                    and current.selected_decision_id == selected_decision_id
                    and current.completed_at == completed
                ):
                    return current
                raise RuntimeError("Terminal cycle outcome cannot regress or change.")
            if datetime.fromisoformat(completed) < datetime.fromisoformat(current.claimed_at):
                raise ValueError("Cycle completion cannot precede its claim.")
            related = [item for item in decisions if item.interval_start == interval]
            unresolved = [item for item in related if item.status == "CLAIMED"]
            eligible = [item for item in related if item.status == "ELIGIBLE"]
            if outcome in {"SELECTED", "NO_SELECTION"} and unresolved:
                raise RuntimeError(
                    "Cycle cannot finish selection while decisions remain CLAIMED."
                )
            if outcome == "NO_SELECTION" and eligible:
                raise RuntimeError("NO_SELECTION conflicts with an eligible decision.")
            if outcome == "SELECTED" and not any(
                item.decision_id == selected_decision_id
                and item.interval_start == interval
                and item.status == "ELIGIBLE"
                for item in decisions
            ):
                raise RuntimeError(
                    "Selected decision is not an eligible decision from this cycle."
                )
            updated = CycleRecord(
                current.interval_start, current.trading_date, outcome,
                current.claimed_at, completed, selected_decision_id,
            )
            cycles[index] = updated
            return updated

        return self._mutate(operation)
