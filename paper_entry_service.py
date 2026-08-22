from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from broker_readiness_service import (
    BrokerReadinessResult,
    evaluate_broker_readiness,
)
from cts_entry_window import MARKET_TIMEZONE, cts_entry_window_open
from daily_limits_service import (
    DailyLimitsResult,
    evaluate_daily_limits,
)
from decision_service import (
    FinalDecision,
    evaluate_final_decision,
)
from earnings_service import (
    EarningsRiskResult,
    evaluate_earnings_risk,
)
from market_session_service import (
    MarketSessionResult,
    evaluate_market_session,
)
from news_service import (
    NewsRiskResult,
    evaluate_news_risk,
)
from options_service import (
    OptionLiquidityResult,
    evaluate_option_liquidity,
)
from order_preview_service import (
    PaperOrderPreview,
    build_paper_order_preview,
)
from paper_execution_service import paper_execution_enabled
from paper_state_service import (
    PaperSessionState,
    load_state,
)
from risk_service import (
    TradePlan,
    build_trade_plan,
)
from scanner_service import (
    ScannerResult,
    fetch_scanner_results,
)
from exit_monitor import OPTION_SYMBOL

MAX_DAILY_REALIZED_LOSS = -50.0
MAX_CONTRACT_COST = 150.0
P_L_TIMESTAMP_AGE_SECONDS = 300


@dataclass
class PaperEntryRequest:
    ticker: str
    contract_symbol: str
    side: str
    quantity: int
    limit_price: float
    now: datetime | None = None


@dataclass
class PaperEntryReadinessReport:
    status: str
    allowed: bool
    submission_allowed: bool
    execution_enabled: bool
    reasons: list[str]
    market_session: Optional[MarketSessionResult]
    broker_readiness: Optional[BrokerReadinessResult]
    scanner_candidate: Optional[ScannerResult]
    option_liquidity: Optional[OptionLiquidityResult]
    trade_plan: Optional[TradePlan]
    news_risk: Optional[NewsRiskResult]
    earnings_risk: Optional[EarningsRiskResult]
    final_decision: Optional[FinalDecision]
    daily_limits: Optional[DailyLimitsResult]
    order_preview: Optional[PaperOrderPreview]
    state: Optional[PaperSessionState]
    duplicate_contract: bool


def _normalize_side(side: str) -> str:
    return str(side or "").strip().upper()


def _normalize_contract_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _contract_direction(symbol: str) -> Optional[str]:
    match = OPTION_SYMBOL.fullmatch(str(symbol or "").upper())
    if not match:
        return None

    normalized = str(symbol).upper()
    root_length = len(match.group("root"))
    contract_type = normalized[root_length + 6 : root_length + 7]

    return "CALL" if contract_type == "C" else "PUT"


def _contract_root_matches(ticker: str, contract_symbol: str) -> bool:
    symbol = str(contract_symbol or "").upper()
    ticker = str(ticker or "").upper()
    match = OPTION_SYMBOL.fullmatch(symbol)
    if not match:
        return False

    return match.group("root") == ticker


def _parse_verified_at(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None

    if timestamp.tzinfo is None:
        return None

    return timestamp


def _pnl_is_fresh(state: PaperSessionState, now: datetime | None = None) -> bool:
    if not state.realized_pnl_verified_at:
        return False

    verified_at = _parse_verified_at(state.realized_pnl_verified_at)
    if verified_at is None:
        return False

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Entry readiness time must include a timezone.")

    market_time = now.astimezone(MARKET_TIMEZONE)
    verified_market_time = verified_at.astimezone(MARKET_TIMEZONE)

    if verified_market_time.date() != market_time.date():
        return False

    age_seconds = (market_time - verified_market_time).total_seconds()
    return 0 <= age_seconds <= P_L_TIMESTAMP_AGE_SECONDS


def is_exact_entry_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Entry readiness time must include a timezone.")

    return cts_entry_window_open(now)


def _load_state(now: datetime | None = None) -> PaperSessionState:
    return load_state(today=(now or datetime.now(timezone.utc)).astimezone(MARKET_TIMEZONE).date())


def evaluate_paper_entry_readiness(
    request: PaperEntryRequest,
) -> PaperEntryReadinessReport:
    reasons: list[str] = []
    market_session = None
    broker_readiness = None
    scanner_candidate = None
    option_liquidity = None
    trade_plan = None
    news_risk = None
    earnings_risk = None
    final_decision = None
    daily_limits = None
    order_preview = None
    state = None
    duplicate_contract = False
    submission_allowed = False
    execution_enabled = False

    if not request.ticker.strip():
        reasons.append("Ticker is required.")

    if not request.contract_symbol.strip():
        reasons.append("Option contract symbol is required.")

    if _normalize_side(request.side) != "BUY":
        reasons.append("Only BUY entries are supported for automatic paper entry.")

    if request.quantity != 1:
        reasons.append("Quantity must be exactly 1 contract under current CTS rules.")

    if request.limit_price <= 0:
        reasons.append("A positive limit price is required.")

    contract_direction = _contract_direction(request.contract_symbol)
    if contract_direction is None:
        reasons.append("Option contract symbol is invalid.")

    if request.limit_price * request.quantity * 100 > MAX_CONTRACT_COST:
        reasons.append(
            f"Contract cost exceeds the ${MAX_CONTRACT_COST:.0f} cap."
        )

    try:
        state = _load_state(request.now)
    except Exception as error:
        reasons.append(
            f"Paper state is unavailable or unreadable: {error}"
        )
        state = None

    request_contract_symbol = _normalize_contract_symbol(request.contract_symbol)
    request_cost = request.limit_price * request.quantity * 100

    if state is not None:
        if not _pnl_is_fresh(state, request.now):
            reasons.append(
                "Broker realized P/L synchronization is unavailable or stale; readiness cannot be determined."
            )
        elif state.realized_pnl <= MAX_DAILY_REALIZED_LOSS:
            reasons.append(
                "Daily realized loss limit has been reached; no new entries are allowed."
            )

        if request_contract_symbol in state.submitted_contracts:
            duplicate_contract = True
            reasons.append(
                "The requested contract has already been submitted today."
            )

        if any(
            _normalize_contract_symbol(position.contract_symbol) == request_contract_symbol
            for position in state.positions
        ):
            duplicate_contract = True
            reasons.append(
                "The requested contract is already open in the managed paper state."
            )

    if request_cost <= MAX_CONTRACT_COST:
        if contract_direction and request.ticker.strip():
            if not _contract_root_matches(request.ticker, request_contract_symbol):
                reasons.append(
                    "Requested contract root does not match the requested ticker."
                )

            try:
                market_session = evaluate_market_session(
                    request.now or datetime.now(timezone.utc)
                )
            except Exception as error:
                reasons.append(
                    f"Market session information is unavailable: {error}"
                )
                market_session = None

            if market_session is not None:
                if not is_exact_entry_window(request.now):
                    reasons.append(
                        "The request time is outside the exact allowed entry windows."
                    )
    
    if state is not None:
        try:
            broker_readiness = evaluate_broker_readiness()
        except Exception as error:
            reasons.append(
                f"Broker readiness information is unavailable: {error}"
            )
            broker_readiness = None

    if state is not None and broker_readiness is not None:
        if broker_readiness.status != "PASS":
            reasons.extend(broker_readiness.reasons)

    if state is not None and contract_direction is not None:
        try:
            results, _ = fetch_scanner_results()
        except Exception as error:
            reasons.append(
                f"CTS scanner information is unavailable: {error}"
            )
            results = []

        if request.ticker.strip():
            candidate = next(
                (
                    result
                    for result in results
                    if result.ticker == request.ticker.strip().upper()
                ),
                None,
            )
            scanner_candidate = candidate

            if candidate is None:
                reasons.append(
                    "CTS scanner did not identify a technical candidate for the requested ticker."
                )
            else:
                if not candidate.technical_candidate():
                    reasons.append(
                        "CTS technical setup is not A/A+ for the requested ticker."
                    )
                if candidate.direction != contract_direction:
                    reasons.append(
                        "The requested contract direction does not match the CTS scan signal."
                    )

        if scanner_candidate is not None and not reasons:
            try:
                option_liquidity = evaluate_option_liquidity(
                    ticker=request.ticker.strip().upper(),
                    direction=scanner_candidate.direction,
                    underlying_price=scanner_candidate.last_price,
                )
            except Exception as error:
                reasons.append(
                    f"Options liquidity information is unavailable: {error}"
                )
                option_liquidity = None

            if option_liquidity is None:
                reasons.append(
                    "Options liquidity gate did not find a suitable contract for the requested ticker and direction."
                )
            elif not option_liquidity.acceptable:
                reasons.extend(option_liquidity.failed_checks)
            else:
                if _normalize_contract_symbol(option_liquidity.contract_symbol) != request_contract_symbol:
                    reasons.append(
                        "Requested contract symbol does not match the option liquidity contract symbol."
                    )
                if option_liquidity.direction != contract_direction:
                    reasons.append(
                        "Requested contract direction does not match the option liquidity result direction."
                    )
                conservative_cost = max(request_cost, option_liquidity.contract_cost)
                if conservative_cost > MAX_CONTRACT_COST:
                    reasons.append(
                        f"Conservative contract cost exceeds the ${MAX_CONTRACT_COST:.0f} cap."
                    )

    if state is not None and broker_readiness is not None and scanner_candidate is not None:
        if broker_readiness.open_positions != len(state.positions):
            reasons.append(
                "Broker open position count does not match managed paper state positions."
            )

    if state is not None and scanner_candidate is not None and option_liquidity is not None:
        try:
            trade_plan = build_trade_plan(
                ticker=request.ticker.strip().upper(),
                contract_symbol=request.contract_symbol.strip().upper(),
                entry_price=request.limit_price,
                realized_pnl_today=state.realized_pnl or 0.0,
            )
        except Exception as error:
            reasons.append(
                f"Risk plan information is unavailable: {error}"
            )
            trade_plan = None

        if trade_plan is not None and not trade_plan.acceptable:
            reasons.extend(trade_plan.failed_checks)

    if state is not None and broker_readiness is not None and scanner_candidate is not None:
        try:
            news_risk = evaluate_news_risk(request.ticker.strip().upper())
        except Exception as error:
            reasons.append(
                f"News risk information is unavailable: {error}"
            )
            news_risk = None

        try:
            earnings_risk = evaluate_earnings_risk(
                request.ticker.strip().upper()
            )
        except Exception as error:
            reasons.append(
                f"Earnings risk information is unavailable: {error}"
            )
            earnings_risk = None

        if news_risk is not None and news_risk.status != "PASS":
            reasons.append(
                "News risk gate did not return PASS for the requested ticker."
            )

        if earnings_risk is not None and earnings_risk.status != "PASS":
            reasons.append(
                "Earnings risk gate did not return PASS for the requested ticker."
            )

    if (
        scanner_candidate is not None
        and trade_plan is not None
        and news_risk is not None
        and earnings_risk is not None
        and market_session is not None
    ):
        try:
            final_decision = evaluate_final_decision(
                ticker=request.ticker.strip().upper(),
                technical_passed=scanner_candidate.technical_candidate(),
                options_passed=bool(option_liquidity and option_liquidity.acceptable),
                risk_plan_passed=trade_plan.acceptable,
                news_status=news_risk.status,
                earnings_status=earnings_risk.status,
                market_session_passed=market_session.entry_allowed,
            )
        except Exception as error:
            reasons.append(
                f"Final CTS decision is unavailable: {error}"
            )
            final_decision = None

        if final_decision is not None and not final_decision.automatic_paper_eligible:
            reasons.extend(final_decision.reasons)

    if (
        state is not None
        and broker_readiness is not None
        and scanner_candidate is not None
        and trade_plan is not None
        and final_decision is not None
    ):
        try:
            daily_limits = evaluate_daily_limits(
                trades_opened_today=state.trades_opened,
                open_positions=broker_readiness.open_positions,
                losing_trades_today=state.losing_trades,
                realized_pnl_today=state.realized_pnl or 0.0,
            )
        except Exception as error:
            reasons.append(
                f"Daily limits information is unavailable: {error}"
            )
            daily_limits = None

        if daily_limits is not None and daily_limits.status != "PASS":
            reasons.extend(daily_limits.reasons)

    if (
        final_decision is not None
        and daily_limits is not None
        and state is not None
        and scanner_candidate is not None
    ):
        order_preview = build_paper_order_preview(
            ticker=request.ticker.strip().upper(),
            contract_symbol=request.contract_symbol.strip().upper(),
            final_decision_status=final_decision.status,
            limit_price=request.limit_price,
            daily_limits_passed=(daily_limits.status == "PASS"),
            market_session_passed=market_session.entry_allowed,
        )

        if not order_preview.eligible:
            reasons.extend(order_preview.reasons)

    try:
        execution_enabled = paper_execution_enabled()
    except Exception:
        execution_enabled = False

    status = "PASS" if not reasons else "BLOCK"
    allowed = status == "PASS"
    submission_allowed = False

    return PaperEntryReadinessReport(
        status=status,
        allowed=allowed,
        submission_allowed=submission_allowed,
        execution_enabled=execution_enabled,
        reasons=reasons,
        market_session=market_session,
        broker_readiness=broker_readiness,
        scanner_candidate=scanner_candidate,
        option_liquidity=option_liquidity,
        trade_plan=trade_plan,
        news_risk=news_risk,
        earnings_risk=earnings_risk,
        final_decision=final_decision,
        daily_limits=daily_limits,
        order_preview=order_preview,
        state=state,
        duplicate_contract=duplicate_contract,
    )


def show_paper_entry_readiness() -> None:
    print("\nCTS READ-ONLY PAPER ENTRY READINESS (LOCKED)")
    print("No Alpaca order submission will occur.")

    ticker = input("Ticker: ").strip().upper()
    contract_symbol = input("Option contract symbol: ").strip().upper()
    side = input("Side (BUY): ").strip().upper() or "BUY"
    quantity_text = input("Quantity: ").strip()
    limit_price_text = input("Limit price: ").strip()

    try:
        quantity = int(quantity_text)
    except ValueError:
        print("Quantity must be a whole number.")
        return

    try:
        limit_price = float(limit_price_text)
    except ValueError:
        print("Limit price must be a number.")
        return

    request = PaperEntryRequest(
        ticker=ticker,
        contract_symbol=contract_symbol,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
    )

    result = evaluate_paper_entry_readiness(request)

    print(f"\nPAPER ENTRY READINESS: {result.status}")
    for reason in result.reasons:
        print(f"- {reason}")

    if result.allowed:
        execution_status = (
            "enabled" if result.execution_enabled else "locked"
        )
        print(
            f"All CTS gates passed. Paper execution kill switch is {execution_status}."
        )
        print(
            "This assessment is LOCKED and no order was submitted."
        )
    else:
        print(
            "The request is not ready for automatic paper entry. "
            "No order was submitted."
        )
