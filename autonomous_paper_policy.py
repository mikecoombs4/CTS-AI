"""Paper-only policy for one narrowly defined ordinary-news review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from news_service import NEWS_LOOKBACK_HOURS, NewsRiskResult
from paper_trial_preflight import AutonomousPaperStartupPreflightResult


CORE_ORIGIN = "CORE_CTS"
SOFT_NEWS_VARIABLE = "CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW"
NEWS_QUERY_MAX_AGE_SECONDS = 300


@dataclass(frozen=True)
class AutonomousPaperPolicyResult:
    status: str
    allowed: bool
    live_execution_eligible: bool
    softened_gate: str | None
    audit_headlines: tuple[str, ...]
    reasons: tuple[str, ...]


def _status(item: Any) -> str | None:
    return getattr(item, "status", None) if item is not None else None


def _fresh_timestamp(value: Any, as_of: datetime, maximum_age: float) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    age = (as_of - value).total_seconds()
    return 0 <= age <= maximum_age


def evaluate_autonomous_paper_policy(
    *,
    readiness: Any,
    origin: str,
    startup_preflight: AutonomousPaperStartupPreflightResult,
    soft_news_configuration: Any,
    preview_created_at: datetime,
    as_of: datetime | None = None,
) -> AutonomousPaperPolicyResult:
    as_of = as_of or datetime.now(timezone.utc)
    reasons: list[str] = []
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("Paper-policy time must be timezone-aware.")
    if origin != CORE_ORIGIN:
        reasons.append("Only CORE_CTS origin is eligible for autonomous paper policy.")
    if (
        startup_preflight.status != "STARTUP_READY"
        or startup_preflight.paper_configuration_verified is not True
        or startup_preflight.autonomous_configuration_verified is not True
        or startup_preflight.execution_configuration_verified is not True
        or startup_preflight.broker_ready is not True
        or startup_preflight.entry_gate_open is not False
        or startup_preflight.submission_authorized is not False
    ):
        reasons.append("Verified autonomous Alpaca paper startup configuration is missing.")

    scanner = getattr(readiness, "scanner_candidate", None)
    try:
        technical_passed = scanner is not None and scanner.technical_candidate() is True
    except Exception:
        technical_passed = False
    if not technical_passed:
        reasons.append("Scanner/technical gate did not pass.")
    broker = getattr(readiness, "broker_readiness", None)
    if _status(broker) != "PASS" or getattr(broker, "paper_mode", None) is not True:
        reasons.append("Paper broker gate did not pass.")
    option = getattr(readiness, "option_liquidity", None)
    if option is None or getattr(option, "acceptable", None) is not True:
        reasons.append("Options/liquidity gate did not pass.")
    plan = getattr(readiness, "trade_plan", None)
    if plan is None or getattr(plan, "acceptable", None) is not True:
        reasons.append("Risk and reward/risk gate did not pass.")
    session = getattr(readiness, "market_session", None)
    if _status(session) != "PASS" or getattr(session, "entry_allowed", None) is not True:
        reasons.append("Market-session gate did not pass.")
    earnings = getattr(readiness, "earnings_risk", None)
    if _status(earnings) != "PASS":
        reasons.append("Earnings gate did not return PASS.")
    limits = getattr(readiness, "daily_limits", None)
    if _status(limits) != "PASS" or getattr(limits, "new_trade_allowed", None) is not True:
        reasons.append("Daily-limits gate did not pass.")
    if getattr(readiness, "state", None) is None:
        reasons.append("Managed paper state is missing.")
    if getattr(readiness, "duplicate_contract", None) is not False:
        reasons.append("Duplicate-contract gate is missing or blocking.")

    preview = getattr(readiness, "order_preview", None)
    if preview is None or not _fresh_timestamp(
        preview_created_at, as_of, NEWS_QUERY_MAX_AGE_SECONDS
    ):
        reasons.append("Paper preview is missing or stale.")
    elif (
        getattr(preview, "side", None) != "BUY"
        or getattr(preview, "order_type", None) != "LIMIT"
        or getattr(preview, "time_in_force", None) != "DAY"
        or getattr(preview, "quantity", None) != 1
        or not isinstance(getattr(preview, "limit_price", None), (int, float))
        or not math.isfinite(float(preview.limit_price))
        or preview.limit_price <= 0
        or getattr(preview, "estimated_cost", float("inf")) > 150
    ):
        reasons.append("Paper preview shape or price is invalid.")

    news = getattr(readiness, "news_risk", None)
    if not isinstance(news, NewsRiskResult):
        reasons.append("News result is missing or malformed.")
        news_status = None
    else:
        news_status = news.status
        if news.provider_query_succeeded is not True or not _fresh_timestamp(
            news.queried_at, as_of, NEWS_QUERY_MAX_AGE_SECONDS
        ):
            reasons.append("News provider success and freshness are not proven.")
        if news.blocking_matches:
            reasons.append("Adverse/BLOCK news cannot be softened.")
        if news.catalyst_matches:
            reasons.append("Catalyst-classified news remains observation-only.")
        if not isinstance(news.headlines, list):
            reasons.append("News headline collection is malformed.")
        elif any(item.blocking_matches for item in news.headlines):
            reasons.append("Headline-level adverse news cannot be softened.")
        elif any(item.catalyst_matches for item in news.headlines):
            reasons.append("Headline-level catalyst news remains observation-only.")

    final = getattr(readiness, "final_decision", None)
    headlines: tuple[str, ...] = ()
    softened_gate: str | None = None
    if (
        news_status == "REVIEW"
        and isinstance(news, NewsRiskResult)
        and isinstance(news.headlines, list)
    ):
        softened_gate = "news_risk"
        if soft_news_configuration != "true":
            reasons.append(f"{SOFT_NEWS_VARIABLE} must be explicitly configured as true.")
        headlines = tuple(item.headline for item in news.headlines)
        if not headlines:
            reasons.append("News REVIEW has no auditable headline.")
        for item in news.headlines:
            if not item.headline.strip() or not _fresh_timestamp(
                item.created_at, as_of, NEWS_LOOKBACK_HOURS * 3600
            ):
                reasons.append("News REVIEW contains a malformed or stale headline.")
                break
        if _status(final) != "REVIEW" or getattr(final, "automatic_paper_eligible", None) is not False:
            reasons.append("Core decision is not the expected news-only REVIEW.")
        elif tuple(getattr(final, "reasons", ())) != ("News requires human or AI review",):
            reasons.append("Core decision contains more than the one permitted news review.")
        preview_reasons = tuple(getattr(preview, "reasons", ())) if preview is not None else ()
        if getattr(preview, "eligible", None) is not False or preview_reasons != (
            "Final CTS decision is REVIEW, not PASS",
        ):
            reasons.append("Preview contains a blocker beyond the permitted news review.")
        if getattr(readiness, "allowed", None) is not False or getattr(
            readiness, "submission_allowed", None
        ) is not False:
            reasons.append("Underlying readiness was unexpectedly authorized.")
        permitted_readiness_reasons = (
            "News risk gate did not return PASS for the requested ticker.",
            "News requires human or AI review",
            "Final CTS decision is REVIEW, not PASS",
        )
        if (
            getattr(readiness, "status", None) != "BLOCK"
            or tuple(getattr(readiness, "reasons", ())) != permitted_readiness_reasons
        ):
            reasons.append("Readiness contains an unresolved gate beyond ordinary news REVIEW.")
    elif news_status == "PASS" and isinstance(news, NewsRiskResult):
        if news.headlines:
            reasons.append("News PASS unexpectedly contains review headlines.")
        if _status(final) != "PASS" or getattr(final, "automatic_paper_eligible", None) is not True:
            reasons.append("Final core decision did not pass.")
        if preview is None or getattr(preview, "eligible", None) is not True:
            reasons.append("Paper preview is not eligible.")
        if getattr(readiness, "status", None) != "PASS" or getattr(readiness, "allowed", None) is not True:
            reasons.append("Core readiness did not pass.")
    else:
        reasons.append("Only proven PASS or ordinary REVIEW news may pass paper policy.")

    unique_reasons = tuple(dict.fromkeys(reasons))
    allowed = not unique_reasons
    status = (
        "PAPER_SOFT_PASS" if allowed and softened_gate else
        "PAPER_POLICY_PASS" if allowed else "BLOCKED"
    )
    audit_reasons = unique_reasons or (
        "Ordinary recent-news REVIEW softened for verified autonomous paper trial only.",
    )
    return AutonomousPaperPolicyResult(
        status, allowed, False, softened_gate, headlines, audit_reasons
    )
