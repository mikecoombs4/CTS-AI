from dataclasses import dataclass
from typing import Any


MIN_OPTIONS_LEVEL = 2
MIN_OPTIONS_BUYING_POWER = 150.0
MAX_OPEN_POSITIONS = 2


@dataclass
class BrokerReadinessResult:
    status: str
    paper_mode: bool
    account_status: str
    options_trading_level: int | None
    options_buying_power: float
    market_open: bool
    open_orders: int
    open_positions: int
    reasons: list[str]


def _value(source: Any, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)

    return getattr(source, name, default)


def _enum_text(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value or "UNKNOWN").upper()


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def classify_broker_readiness(
    account: Any,
    clock: Any,
    open_orders: list,
    positions: list,
    paper_mode: bool = True,
) -> BrokerReadinessResult:
    account_status = _enum_text(_value(account, "status"))
    options_level = _value(account, "options_trading_level")
    options_buying_power = _float_value(
        _value(account, "options_buying_power")
    )
    market_open = bool(_value(clock, "is_open", False))
    reasons = []

    if not paper_mode:
        reasons.append("Trading client is not locked to paper mode")

    if account_status != "ACTIVE":
        reasons.append(f"Account status is {account_status}, not ACTIVE")

    if bool(_value(account, "trading_blocked", False)):
        reasons.append("Account trading is blocked")

    if bool(_value(account, "account_blocked", False)):
        reasons.append("Account is blocked")

    if bool(_value(account, "trade_suspended_by_user", False)):
        reasons.append("Trading is suspended by the user")

    if options_level is None or int(options_level) < MIN_OPTIONS_LEVEL:
        reasons.append("Effective options trading level is below 2")

    if options_buying_power < MIN_OPTIONS_BUYING_POWER:
        reasons.append("Options buying power is below the $150 CTS cap")

    if not market_open:
        reasons.append("Alpaca market clock reports closed")

    if len(open_orders) > 0:
        reasons.append("Existing open broker orders require review")

    if len(positions) >= MAX_OPEN_POSITIONS:
        reasons.append("Maximum of 2 open positions is already reached")

    return BrokerReadinessResult(
        status="PASS" if not reasons else "BLOCK",
        paper_mode=paper_mode,
        account_status=account_status,
        options_trading_level=(
            int(options_level) if options_level is not None else None
        ),
        options_buying_power=options_buying_power,
        market_open=market_open,
        open_orders=len(open_orders),
        open_positions=len(positions),
        reasons=reasons or ["Paper broker readiness checks passed"],
    )


def evaluate_broker_readiness() -> BrokerReadinessResult:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from alpaca_service import get_alpaca_credentials

    api_key, secret_key = get_alpaca_credentials()
    client = TradingClient(api_key, secret_key, paper=True)
    account = client.get_account()
    clock = client.get_clock()
    open_orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
    )
    positions = client.get_all_positions()

    return classify_broker_readiness(
        account=account,
        clock=clock,
        open_orders=list(open_orders),
        positions=list(positions),
        paper_mode=True,
    )


def show_broker_readiness() -> None:
    print("\nCTS READ-ONLY ALPACA READINESS CHECK")
    print("No order-submission method is imported or called.")

    try:
        result = evaluate_broker_readiness()
    except Exception as error:
        print("\nBROKER READINESS: BLOCK")
        print(f"Unable to complete readiness check: {error}")
        print("No order was submitted.")
        return

    print(f"\nBROKER READINESS: {result.status}")
    print(f"Paper mode locked: {result.paper_mode}")
    print(f"Account status: {result.account_status}")
    print(
        "Effective options level: "
        f"{result.options_trading_level} (2+ required)"
    )
    print(
        "Options buying power: "
        f"${result.options_buying_power:,.2f}"
    )
    print(f"Alpaca market clock open: {result.market_open}")
    print(f"Open broker orders: {result.open_orders}")
    print(f"Open positions: {result.open_positions}")

    for reason in result.reasons:
        print(f"- {reason}")

    print("Read-only broker inspection. No order was submitted.")
