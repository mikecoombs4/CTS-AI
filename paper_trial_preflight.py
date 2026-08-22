from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from broker_readiness_service import (
    BrokerReadinessResult,
    evaluate_broker_readiness,
)
from paper_execution_service import ENABLE_VALUE, paper_execution_enabled
from paper_state_service import state_file_path

DEFAULT_MAX_TRADES_PER_DAY = 2
DEFAULT_MAX_OPEN_POSITIONS = 2
MAX_SAFE_TRADES_PER_DAY = 2
MAX_SAFE_OPEN_POSITIONS = 2
MAX_CONTRACT_COST = 150.0
DAILY_LOSS_LIMIT = 50.0
TRIAL_TRADES_VARIABLE = "CTS_TRIAL_MAX_TRADES_PER_DAY"
TRIAL_POSITIONS_VARIABLE = "CTS_TRIAL_MAX_OPEN_POSITIONS"
PAPER_MODE_VARIABLE = "ALPACA_PAPER"
AUTONOMOUS_PAPER_VARIABLE = "CTS_AUTONOMOUS_PAPER_ENABLED"
PAPER_EXECUTION_VARIABLE = "CTS_PAPER_EXECUTION_ENABLED"


@dataclass(frozen=True)
class TrialLimits:
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS


@dataclass(frozen=True)
class PaperTrialPreflightResult:
    status: str
    paper_configuration_verified: bool
    paper_mode_verified: bool
    account_ready: bool
    options_ready: bool
    trial_limits: TrialLimits | None
    state_write_ready: bool
    log_write_ready: bool
    execution_kill_switch_enabled: bool
    reasons: list[str]
    broker_readiness: BrokerReadinessResult | None = None


@dataclass(frozen=True)
class AutonomousPaperStartupPreflightResult:
    status: str
    paper_configuration_verified: bool
    autonomous_configuration_verified: bool
    execution_configuration_verified: bool
    broker_ready: bool
    trial_limits: TrialLimits | None
    state_write_ready: bool
    log_write_ready: bool
    entry_gate_open: bool
    submission_authorized: bool
    reasons: list[str]
    broker_readiness: BrokerReadinessResult | None = None


def _parse_safe_limit(value: Any, name: str, maximum: int) -> int:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{name} is missing")
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(text)
    if parsed < 1 or parsed > maximum:
        raise ValueError(
            f"{name} must be between 1 and {maximum}"
        )
    return parsed


def resolve_trial_limits(config: dict[str, Any] | None = None) -> TrialLimits:
    config = config or {}
    trades_value = config.get(
        TRIAL_TRADES_VARIABLE,
        DEFAULT_MAX_TRADES_PER_DAY,
    )
    positions_value = config.get(
        TRIAL_POSITIONS_VARIABLE,
        DEFAULT_MAX_OPEN_POSITIONS,
    )
    return TrialLimits(
        max_trades_per_day=_parse_safe_limit(
            trades_value,
            TRIAL_TRADES_VARIABLE,
            MAX_SAFE_TRADES_PER_DAY,
        ),
        max_open_positions=_parse_safe_limit(
            positions_value,
            TRIAL_POSITIONS_VARIABLE,
            MAX_SAFE_OPEN_POSITIONS,
        ),
    )


def load_trial_limits() -> TrialLimits:
    from dotenv import dotenv_values

    config = dotenv_values(Path(__file__).with_name(".env"))
    return resolve_trial_limits(config)


def resolve_paper_mode(config: dict[str, Any] | None = None) -> bool:
    config = config or {}
    configured = config.get(PAPER_MODE_VARIABLE)
    if configured is None or str(configured).strip().lower() != "true":
        raise ValueError(
            f"{PAPER_MODE_VARIABLE} must be explicitly true"
        )
    return True


def load_paper_mode() -> bool:
    from dotenv import dotenv_values

    config = dotenv_values(Path(__file__).with_name(".env"))
    return resolve_paper_mode(config)


def _load_configuration() -> dict[str, Any]:
    from dotenv import dotenv_values

    return dict(dotenv_values(Path(__file__).with_name(".env")))


def run_autonomous_paper_startup_preflight(
    *,
    config: dict[str, Any] | None = None,
    limits: TrialLimits | None = None,
    broker_readiness: BrokerReadinessResult | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
) -> AutonomousPaperStartupPreflightResult:
    """Validate startup configuration only; never authorize an entry."""
    reasons: list[str] = []
    try:
        resolved_config = _load_configuration() if config is None else dict(config)
    except Exception as error:
        resolved_config = {}
        reasons.append(f"Autonomous paper configuration is unavailable: {type(error).__name__}")

    paper_verified = resolved_config.get(PAPER_MODE_VARIABLE) == "true"
    autonomous_verified = resolved_config.get(AUTONOMOUS_PAPER_VARIABLE) == "true"
    execution_verified = resolved_config.get(PAPER_EXECUTION_VARIABLE) == ENABLE_VALUE
    if not paper_verified:
        reasons.append("ALPACA_PAPER must be explicitly configured as true.")
    if not autonomous_verified:
        reasons.append("CTS_AUTONOMOUS_PAPER_ENABLED must be explicitly configured as true.")
    if not execution_verified:
        reasons.append("The exact paper-execution enable value is not configured.")

    resolved_limits = limits
    try:
        resolved_limits = resolved_limits or resolve_trial_limits(resolved_config)
    except (TypeError, ValueError) as error:
        reasons.append(f"Trial limit configuration is invalid: {error}")
    if resolved_limits != TrialLimits(1, 1):
        reasons.append("Autonomous paper trial limits must be exactly one trade and one position.")

    broker = broker_readiness
    if broker is None:
        try:
            broker = _safe_broker_readiness()
        except Exception as error:
            reasons.append(f"Paper broker readiness is unavailable: {type(error).__name__}")
    broker_ready = bool(
        broker
        and broker.status == "PASS"
        and broker.paper_mode is True
        and broker.account_status == "ACTIVE"
        and broker.options_trading_level is not None
        and broker.options_trading_level >= 2
        and broker.options_buying_power >= MAX_CONTRACT_COST
        and not broker.reasons
    )
    if not broker_ready:
        reasons.append("Broker account/options/buying-power readiness did not pass in paper mode.")

    state_path = state_path or state_file_path()
    log_path = log_path or Path(__file__).with_name("cts_autonomous_paper.log")
    state_ready = _path_write_ready(state_path)
    log_ready = _path_write_ready(log_path)
    if not state_ready:
        reasons.append("Autonomous state directory is not writable or does not exist.")
    if not log_ready:
        reasons.append("Autonomous log directory is not writable or does not exist.")

    ready = bool(
        paper_verified and autonomous_verified and execution_verified and broker_ready
        and resolved_limits == TrialLimits(1, 1) and state_ready and log_ready and not reasons
    )
    if ready:
        reasons = [
            "Startup configuration is ready; entry remains gated on runner lock/state, "
            "reconciliation, exit health, session, completed bar, and candidate checks."
        ]
    return AutonomousPaperStartupPreflightResult(
        "STARTUP_READY" if ready else "BLOCKED",
        paper_verified,
        autonomous_verified,
        execution_verified,
        broker_ready,
        resolved_limits,
        state_ready,
        log_ready,
        False,
        False,
        reasons,
        broker,
    )


def _path_write_ready(path: Path) -> bool:
    parent = path.parent
    return parent.exists() and os.access(parent, os.W_OK)


def _safe_broker_readiness() -> BrokerReadinessResult:
    return evaluate_broker_readiness()


def run_paper_trial_preflight(
    limits: TrialLimits | None = None,
    broker_readiness: BrokerReadinessResult | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    execution_enabled: bool | None = None,
    paper_configuration: bool | None = None,
) -> PaperTrialPreflightResult:
    reasons: list[str] = []
    paper_configuration_verified = False
    try:
        paper_configuration_verified = (
            load_paper_mode()
            if paper_configuration is None
            else paper_configuration is True
        )
    except (OSError, TypeError, ValueError) as error:
        reasons.append(f"Paper configuration is invalid or missing: {error}")

    resolved_limits = limits
    try:
        resolved_limits = resolved_limits or load_trial_limits()
    except (OSError, TypeError, ValueError) as error:
        reasons.append(f"Trial limit configuration is invalid: {error}")

    broker = broker_readiness
    if broker is None:
        try:
            broker = _safe_broker_readiness()
        except Exception as error:
            reasons.append(f"Paper broker readiness is unavailable: {error}")

    paper_mode_verified = bool(
        paper_configuration_verified
        and broker
        and broker.paper_mode is True
    )
    account_ready = bool(
        broker
        and broker.account_status == "ACTIVE"
        and not broker.reasons
    )
    options_ready = bool(
        broker
        and broker.options_trading_level is not None
        and broker.options_trading_level >= 2
        and broker.options_buying_power >= MAX_CONTRACT_COST
    )

    if not paper_mode_verified:
        reasons.append(
            "Paper mode requires explicit ALPACA_PAPER=true and broker confirmation"
        )
    if broker is not None and not account_ready:
        reasons.extend(broker.reasons or ["Paper account is not ready"])
    if broker is not None and not options_ready:
        reasons.append("Options readiness is unavailable or below CTS requirements")

    try:
        kill_switch = (
            paper_execution_enabled()
            if execution_enabled is None
            else execution_enabled
        )
    except Exception as error:
        kill_switch = False
        reasons.append(f"Paper execution kill-switch status is unavailable: {error}")

    if kill_switch:
        reasons.append(
            "Paper execution kill switch is enabled; preflight requires it locked"
        )

    state_path = state_path or state_file_path()
    log_path = log_path or Path(__file__).with_name("cts_catalyst_monitor.log")
    state_ready = _path_write_ready(state_path)
    log_ready = _path_write_ready(log_path)
    if not state_ready:
        reasons.append("Paper state directory is not writable or does not exist")
    if not log_ready:
        reasons.append("Monitor log directory is not writable or does not exist")

    status = (
        "READY"
        if (
            resolved_limits is not None
            and paper_configuration_verified
            and paper_mode_verified
            and account_ready
            and options_ready
            and state_ready
            and log_ready
            and not kill_switch
            and not reasons
        )
        else "BLOCKED"
    )
    return PaperTrialPreflightResult(
        status=status,
        paper_configuration_verified=paper_configuration_verified,
        paper_mode_verified=paper_mode_verified,
        account_ready=account_ready,
        options_ready=options_ready,
        trial_limits=resolved_limits,
        state_write_ready=state_ready,
        log_write_ready=log_ready,
        execution_kill_switch_enabled=kill_switch,
        reasons=reasons,
        broker_readiness=broker,
    )
