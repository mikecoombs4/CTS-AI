"""Pure selection of fully approved core CTS candidates from completed bars."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from cts_entry_window import MARKET_TIMEZONE, cts_entry_window_open
from scanner_service import BAR_MINUTES, ScannerResult


CORE_ORIGIN = "CORE_CTS"
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$", re.ASCII)


@dataclass(frozen=True)
class CandidateEvaluation:
    origin: str
    scanner_result: ScannerResult | None
    readiness: Any


@dataclass(frozen=True)
class RankedCoreCandidate:
    ticker: str
    scanner_result: ScannerResult
    readiness: Any
    decision_id: str
    technical_score: float
    volume_ratio: float


@dataclass(frozen=True)
class CandidateExclusion:
    ticker: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateSelectionResult:
    ranked_eligible: tuple[RankedCoreCandidate, ...]
    selected: RankedCoreCandidate | None
    exclusions: tuple[CandidateExclusion, ...]


def _strict_entry_window(as_of: datetime) -> bool:
    return cts_entry_window_open(as_of)


def latest_completed_bar_start(as_of: datetime) -> datetime:
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("Selection time must be a timezone-aware datetime.")
    market_time = as_of.astimezone(MARKET_TIMEZONE)
    boundary = market_time.replace(
        minute=(market_time.minute // BAR_MINUTES) * BAR_MINUTES,
        second=0,
        microsecond=0,
    )
    return boundary - timedelta(minutes=BAR_MINUTES)


def validate_completed_bar(
    bar_timestamp: Any,
    as_of: datetime,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        return ("Selection time is missing, malformed, or timezone-naive.",)
    if not _strict_entry_window(as_of):
        reasons.append("Selection time is outside the strict CTS entry windows.")
    if not isinstance(bar_timestamp, datetime):
        reasons.append("Scanner bar timestamp is missing or malformed.")
        return tuple(reasons)
    if bar_timestamp.tzinfo is None:
        reasons.append("Scanner bar timestamp is timezone-naive.")
        return tuple(reasons)

    market_bar = bar_timestamp.astimezone(MARKET_TIMEZONE)
    if market_bar.second != 0 or market_bar.microsecond != 0 or (
        market_bar.minute % BAR_MINUTES != 0
    ):
        reasons.append("Scanner bar timestamp is not aligned to a 15-minute interval.")
    if bar_timestamp + timedelta(minutes=BAR_MINUTES) > as_of:
        reasons.append("Scanner bar is forming or in the future.")
    expected = latest_completed_bar_start(as_of)
    if market_bar != expected:
        if market_bar < expected:
            reasons.append("Scanner bar is stale; it is not the latest completed interval.")
        elif bar_timestamp + timedelta(minutes=BAR_MINUTES) <= as_of:
            reasons.append("Scanner bar is not the expected latest completed interval.")
    return tuple(reasons)


def decision_identity(ticker: str, bar_timestamp: datetime) -> str:
    normalized_ticker = str(ticker or "").strip().upper()
    if not TICKER_PATTERN.fullmatch(normalized_ticker):
        raise ValueError("Decision ticker is missing or invalid.")
    if not isinstance(bar_timestamp, datetime) or bar_timestamp.tzinfo is None:
        raise ValueError("Decision bar timestamp must be timezone-aware.")
    interval = bar_timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    canonical = f"{CORE_ORIGIN}|{normalized_ticker}|{interval}"
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:24]
    return f"core-cts-{normalized_ticker.lower()}-{digest}"


def _status_pass(item: Any) -> bool:
    return item is not None and getattr(item, "status", None) == "PASS"


def _eligibility_reasons(
    evaluation: CandidateEvaluation,
    as_of: datetime,
) -> tuple[tuple[str, ...], float | None, float | None]:
    reasons: list[str] = []
    scanner = evaluation.scanner_result
    readiness = evaluation.readiness
    if evaluation.origin != CORE_ORIGIN:
        reasons.append("Candidate origin is not CORE_CTS; catalyst candidates are excluded.")
    if not isinstance(scanner, ScannerResult):
        reasons.append("Real ScannerResult data is missing or malformed.")
        return tuple(reasons), None, None

    ticker = str(scanner.ticker or "").strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        reasons.append("Scanner ticker is missing or invalid.")
    reasons.extend(validate_completed_bar(scanner.bar_timestamp, as_of))
    start_is_aware = (
        isinstance(scanner.bar_timestamp, datetime)
        and scanner.bar_timestamp.tzinfo is not None
    )
    expected_bar_end = (
        scanner.bar_timestamp + timedelta(minutes=BAR_MINUTES)
        if start_is_aware else None
    )
    if (
        expected_bar_end is None
        or not isinstance(scanner.bar_end_timestamp, datetime)
        or scanner.bar_end_timestamp.tzinfo is None
        or scanner.bar_end_timestamp.astimezone(timezone.utc)
        != expected_bar_end.astimezone(timezone.utc)
    ):
        reasons.append("Scanner completed-bar end evidence is missing or malformed.")
    raw_score: Any = None
    try:
        raw_score = scanner.score()
        technical_score = float(raw_score)
    except (AttributeError, TypeError, ValueError, OverflowError):
        technical_score = math.nan
    if (
        isinstance(raw_score, bool)
        or not math.isfinite(technical_score)
        or not technical_score.is_integer()
        or not 0 <= technical_score <= 4
    ):
        reasons.append("Scanner technical score is missing or malformed.")
        technical_score = None
    evidence = (
        scanner.trend_confirmed,
        scanner.potter_box_found,
        scanner.volume_confirmed,
        scanner.breakout_confirmed,
    )
    if any(type(item) is not bool for item in evidence):
        reasons.append("Scanner technical evidence is missing or malformed.")
    if technical_score != 4 or evidence != (True, True, True, True):
        reasons.append("Scanner technical result did not pass every required check.")
    try:
        volume_ratio = float(scanner.volume_ratio)
    except (TypeError, ValueError):
        volume_ratio = math.nan
    if not math.isfinite(volume_ratio) or volume_ratio < 0:
        reasons.append("Scanner volume ratio is missing or malformed.")
        volume_ratio = None

    if readiness is None:
        reasons.append("Readiness result is missing.")
        return tuple(reasons), technical_score, volume_ratio
    if getattr(readiness, "status", None) != "PASS" or getattr(
        readiness, "allowed", None
    ) is not True:
        reasons.append("Final entry readiness is not PASS and allowed.")
    readiness_scanner = getattr(readiness, "scanner_candidate", None)
    if readiness_scanner is not scanner:
        reasons.append("Readiness is not bound to the supplied scanner result.")
    if str(getattr(readiness_scanner, "ticker", "")).strip().upper() != ticker:
        reasons.append("Readiness ticker does not match the scanner ticker.")

    broker = getattr(readiness, "broker_readiness", None)
    if not _status_pass(broker) or getattr(broker, "paper_mode", None) is not True:
        reasons.append("Paper broker-readiness gate did not pass.")
    option = getattr(readiness, "option_liquidity", None)
    if option is None or getattr(option, "acceptable", None) is not True:
        reasons.append("Option-liquidity gate did not pass.")
    plan = getattr(readiness, "trade_plan", None)
    if plan is None or getattr(plan, "acceptable", None) is not True:
        reasons.append("Risk-plan gate did not pass.")
    if not _status_pass(getattr(readiness, "news_risk", None)):
        reasons.append("News-risk gate did not return PASS.")
    if not _status_pass(getattr(readiness, "earnings_risk", None)):
        reasons.append("Earnings-risk gate did not return PASS.")
    decision = getattr(readiness, "final_decision", None)
    if not _status_pass(decision) or getattr(
        decision, "automatic_paper_eligible", None
    ) is not True:
        reasons.append("Final CTS decision did not pass.")
    limits = getattr(readiness, "daily_limits", None)
    if not _status_pass(limits) or getattr(limits, "new_trade_allowed", None) is not True:
        reasons.append("Daily-limits gate did not pass.")
    preview = getattr(readiness, "order_preview", None)
    if preview is None or getattr(preview, "eligible", None) is not True:
        reasons.append("Paper-order preview is missing or ineligible.")
    elif str(getattr(preview, "ticker", "")).strip().upper() != ticker:
        reasons.append("Paper-order preview ticker does not match.")
    session = getattr(readiness, "market_session", None)
    if not _status_pass(session) or getattr(session, "entry_allowed", None) is not True:
        reasons.append("Market-session gate did not pass.")
    if getattr(readiness, "state", None) is None:
        reasons.append("Managed paper state is missing.")
    if getattr(readiness, "duplicate_contract", None) is not False:
        reasons.append("Duplicate-contract state is missing or blocking.")
    return tuple(dict.fromkeys(reasons)), technical_score, volume_ratio


def _candidate_key(evaluation: CandidateEvaluation) -> tuple[str, str] | None:
    scanner = evaluation.scanner_result
    if not isinstance(scanner, ScannerResult):
        return None
    ticker = str(scanner.ticker or "").strip().upper()
    timestamp = scanner.bar_timestamp
    if not TICKER_PATTERN.fullmatch(ticker) or not isinstance(timestamp, datetime) or (
        timestamp.tzinfo is None
    ):
        return None
    interval = timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return ticker, interval


def select_core_candidates(
    evaluations: Iterable[CandidateEvaluation],
    as_of: datetime,
) -> CandidateSelectionResult:
    evaluations = list(evaluations)
    counts: dict[tuple[str, str], int] = {}
    for evaluation in evaluations:
        key = _candidate_key(evaluation)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    eligible: list[RankedCoreCandidate] = []
    exclusions: list[CandidateExclusion] = []
    for evaluation in evaluations:
        scanner = evaluation.scanner_result
        ticker = str(getattr(scanner, "ticker", "UNKNOWN") or "UNKNOWN").strip().upper()
        reasons, technical_score, volume_ratio = _eligibility_reasons(evaluation, as_of)
        key = _candidate_key(evaluation)
        if key is not None and counts[key] > 1:
            reasons = tuple(dict.fromkeys(
                reasons + ("Duplicate ticker/completed-bar evaluation fails closed.",)
            ))
        if reasons:
            exclusions.append(CandidateExclusion(ticker, reasons))
            continue
        eligible.append(
            RankedCoreCandidate(
                ticker=ticker,
                scanner_result=scanner,
                readiness=evaluation.readiness,
                decision_id=decision_identity(ticker, scanner.bar_timestamp),
                technical_score=technical_score,
                volume_ratio=volume_ratio,
            )
        )

    eligible.sort(
        key=lambda item: (
            -item.technical_score,
            -int(item.scanner_result.breakout_confirmed),
            -int(item.scanner_result.volume_confirmed),
            -item.volume_ratio,
            item.ticker,
        )
    )
    exclusions.sort(key=lambda item: (item.ticker, item.reasons))
    ranked = tuple(eligible)
    return CandidateSelectionResult(ranked, ranked[0] if ranked else None, tuple(exclusions))
