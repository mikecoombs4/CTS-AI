from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_session_service import evaluate_market_session

DEFAULT_STATE_FILE = Path(__file__).with_name(
    "cts_catalyst_monitor_state.json"
)
EXPECTED_STATUS = "PAPER_ONLY_CANDIDATE"


@dataclass(frozen=True)
class EligibilityReview:
    candidate_id: str
    ticker: str
    evaluation_time: str
    overall_result: str
    gates: dict[str, dict[str, Any]]
    reasons: list[str]
    paper_only: bool = True


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _candidate_revision(candidate: dict[str, Any]) -> str:
    serialized = json.dumps(
        candidate,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _gate(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason}


def _candidate_id(candidate_id: str | None, candidate: dict[str, Any]) -> str:
    if candidate_id and str(candidate_id).strip():
        return str(candidate_id).strip()
    fingerprint = str(candidate.get("catalyst_fingerprint", "")).strip()
    ticker = str(candidate.get("ticker", "")).strip().upper()
    return f"{ticker}:{fingerprint}" if ticker and fingerprint else ""


def evaluate_candidate(
    candidate: dict[str, Any],
    candidate_id: str | None = None,
    now: datetime | None = None,
) -> EligibilityReview:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Eligibility review time must include a timezone.")
    current = current.astimezone(timezone.utc)
    evaluation_time = current.isoformat()
    resolved_id = _candidate_id(candidate_id, candidate)
    ticker = str(candidate.get("ticker", "")).strip().upper()
    reasons: list[str] = []
    gates: dict[str, dict[str, Any]] = {}

    valid_identity = bool(resolved_id and ticker)
    paper_only = candidate.get("paper_only") is True
    expected_status = candidate.get("candidate_status") == EXPECTED_STATUS
    if not valid_identity:
        reasons.append("Candidate identifier and ticker are required")
    if not paper_only:
        reasons.append("Candidate is not explicitly marked paper-only")
    if not expected_status:
        reasons.append(
            f"Candidate status must be {EXPECTED_STATUS}"
        )

    gates["candidate_validation"] = _gate(
        "PASS" if valid_identity and paper_only and expected_status else "FAIL",
        "Candidate schema is valid"
        if valid_identity and paper_only and expected_status
        else "; ".join(reasons),
    )

    technical = candidate.get("technical_confirmation")
    technical_passed = (
        isinstance(technical, dict)
        and technical.get("score") == 4
        and bool(technical.get("direction"))
        and technical.get("bar_timestamp") is not None
    )
    gates["technical_confirmation"] = _gate(
        "PASS" if technical_passed else "FAIL",
        "Scanner technical confirmation is present"
        if technical_passed
        else "Technical confirmation is missing or incomplete",
    )
    if not technical_passed:
        reasons.append("Technical confirmation is missing or incomplete")

    session_passed = False
    session_available = True
    try:
        session = evaluate_market_session(current)
        session_passed = session.entry_allowed
        gates["cts_entry_window"] = _gate(
            "PASS" if session_passed else "FAIL",
            session.reason,
        )
        if not session_passed:
            reasons.append(session.reason)
    except Exception as error:
        session_available = False
        gates["cts_entry_window"] = _gate(
            "NEEDS_DATA", f"CTS session data unavailable: {error}"
        )
        reasons.append("CTS session data is unavailable")

    for name, reason in {
        "options_contract": "Options contract is not supplied",
        "option_premium": "Option premium is not supplied",
        "options_liquidity": "Options liquidity data is not supplied",
        "risk_reward": "Reward/risk inputs are not supplied",
        "earnings_risk": "Earnings risk data is not supplied",
        "broker_readiness": "Broker readiness is not evaluated by this read-only review",
    }.items():
        gates[name] = _gate("NEEDS_DATA", reason)
        reasons.append(reason)

    if not valid_identity or not paper_only or not expected_status or not technical_passed:
        overall_result = "BLOCKED"
    elif not session_available:
        overall_result = "NEEDS_DATA"
    elif not session_passed:
        overall_result = "BLOCKED"
    else:
        overall_result = "NEEDS_DATA"

    return EligibilityReview(
        candidate_id=resolved_id,
        ticker=ticker,
        evaluation_time=evaluation_time,
        overall_result=overall_result,
        gates=gates,
        reasons=reasons,
        paper_only=True,
    )


def review_candidate(
    candidate_id: str,
    state_file: Path = DEFAULT_STATE_FILE,
    now: datetime | None = None,
) -> EligibilityReview:
    state = _read_state(state_file)
    candidates = state.get("paper_candidates", {})
    candidate = candidates.get(candidate_id) if isinstance(candidates, dict) else None
    if not isinstance(candidate, dict):
        review = evaluate_candidate(
            {"ticker": "", "candidate_status": "", "paper_only": False},
            candidate_id=candidate_id,
            now=now,
        )
    else:
        review = evaluate_candidate(candidate, candidate_id, now)

    reviews = state.setdefault("eligibility_reviews", {})
    existing = reviews.get(review.candidate_id)
    revision = _candidate_revision(candidate or {})
    if (
        isinstance(existing, dict)
        and existing.get("candidate_revision") == revision
        and existing.get("overall_result") == review.overall_result
        and existing.get("gates") == review.gates
    ):
        return EligibilityReview(
            candidate_id=existing["candidate_id"],
            ticker=existing["ticker"],
            evaluation_time=existing["evaluation_time"],
            overall_result=existing["overall_result"],
            gates=existing["gates"],
            reasons=existing["reasons"],
            paper_only=existing.get("paper_only") is True,
        )

    serialized = {
        "candidate_id": review.candidate_id,
        "ticker": review.ticker,
        "evaluation_time": review.evaluation_time,
        "overall_result": review.overall_result,
        "gates": review.gates,
        "reasons": review.reasons,
        "paper_only": review.paper_only,
        "candidate_revision": revision,
    }
    reviews[review.candidate_id] = serialized
    state["eligibility_reviews"] = reviews
    _write_state(state_file, state)
    return review
