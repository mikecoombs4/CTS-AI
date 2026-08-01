from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from alpaca_service import get_alpaca_credentials


MARKET_TIMEZONE = ZoneInfo("America/New_York")
MAX_DAYS_TO_EXPIRATION = 7
MIN_OPEN_INTEREST = 100
MAX_SPREAD_PERCENT = 15.0
MAX_CONTRACT_COST = 150.0
STRIKE_RANGE_PERCENT = 5.0


@dataclass
class OptionLiquidityResult:
    ticker: str
    direction: str
    contract_symbol: str
    expiration_date: str
    strike_price: float
    bid_price: float
    ask_price: float
    midpoint_price: float
    contract_cost: float
    spread_percent: float
    open_interest: int
    acceptable: bool
    failed_checks: list[str]


def _value(source, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)

    return getattr(source, name, default)


def _contracts_from_response(response) -> list:
    contracts = _value(response, "option_contracts", [])
    return list(contracts or [])


def _snapshot_for_symbol(snapshots, symbol: str):
    if isinstance(snapshots, dict):
        return snapshots.get(symbol)

    data = _value(snapshots, "data", {})

    if isinstance(data, dict):
        return data.get(symbol)

    return None


def evaluate_option_liquidity(
    ticker: str,
    direction: str,
    underlying_price: float,
) -> OptionLiquidityResult | None:
    api_key, secret_key = get_alpaca_credentials()
    trading_client = TradingClient(
        api_key,
        secret_key,
        paper=True,
    )
    data_client = OptionHistoricalDataClient(
        api_key,
        secret_key,
    )

    contract_type = (
        ContractType.CALL
        if direction == "CALL"
        else ContractType.PUT
    )
    today = datetime.now(MARKET_TIMEZONE).date()
    last_expiration = today + timedelta(
        days=MAX_DAYS_TO_EXPIRATION
    )
    strike_offset = STRIKE_RANGE_PERCENT / 100

    response = trading_client.get_option_contracts(
        GetOptionContractsRequest(
            underlying_symbols=[ticker],
            expiration_date_gte=today,
            expiration_date_lte=last_expiration,
            type=contract_type,
            strike_price_gte=(
                f"{underlying_price * (1 - strike_offset):.2f}"
            ),
            strike_price_lte=(
                f"{underlying_price * (1 + strike_offset):.2f}"
            ),
            limit=1000,
        )
    )
    contracts = [
        contract
        for contract in _contracts_from_response(response)
        if bool(_value(contract, "tradable", False))
    ]

    if not contracts:
        return None

    contracts.sort(
        key=lambda contract: (
            _value(contract, "expiration_date"),
            abs(
                float(_value(contract, "strike_price", 0))
                - underlying_price
            ),
        )
    )
    nearest_expiration = _value(
        contracts[0],
        "expiration_date",
    )
    nearest_contracts = [
        contract
        for contract in contracts
        if _value(contract, "expiration_date")
        == nearest_expiration
    ][:20]
    symbols = [
        str(_value(contract, "symbol", ""))
        for contract in nearest_contracts
        if _value(contract, "symbol", "")
    ]

    if not symbols:
        return None

    snapshots = data_client.get_option_snapshot(
        OptionSnapshotRequest(
            symbol_or_symbols=symbols,
        )
    )
    evaluated = []

    for contract in nearest_contracts:
        symbol = str(_value(contract, "symbol", ""))
        snapshot = _snapshot_for_symbol(snapshots, symbol)
        quote = _value(snapshot, "latest_quote")

        if quote is None:
            continue

        bid_price = float(_value(quote, "bid_price", 0) or 0)
        ask_price = float(_value(quote, "ask_price", 0) or 0)

        if bid_price <= 0 or ask_price <= 0:
            continue

        midpoint_price = (bid_price + ask_price) / 2
        spread_percent = (
            (ask_price - bid_price)
            / midpoint_price
            * 100
        )
        contract_cost = midpoint_price * 100
        open_interest = int(
            float(_value(contract, "open_interest", 0) or 0)
        )
        failures = []

        if open_interest < MIN_OPEN_INTEREST:
            failures.append(
                f"Open interest below {MIN_OPEN_INTEREST}"
            )

        if spread_percent > MAX_SPREAD_PERCENT:
            failures.append(
                f"Bid/ask spread above {MAX_SPREAD_PERCENT:.0f}%"
            )

        if contract_cost > MAX_CONTRACT_COST:
            failures.append(
                f"Estimated contract cost above ${MAX_CONTRACT_COST:.0f}"
            )

        evaluated.append(
            OptionLiquidityResult(
                ticker=ticker,
                direction=direction,
                contract_symbol=symbol,
                expiration_date=str(
                    _value(contract, "expiration_date", "")
                ),
                strike_price=float(
                    _value(contract, "strike_price", 0)
                ),
                bid_price=bid_price,
                ask_price=ask_price,
                midpoint_price=midpoint_price,
                contract_cost=contract_cost,
                spread_percent=spread_percent,
                open_interest=open_interest,
                acceptable=not failures,
                failed_checks=failures,
            )
        )

    if not evaluated:
        return None

    evaluated.sort(
        key=lambda result: (
            not result.acceptable,
            abs(result.strike_price - underlying_price),
            result.spread_percent,
            -result.open_interest,
        )
    )

    return evaluated[0]


def show_option_liquidity(result: OptionLiquidityResult) -> None:
    status = "PASS" if result.acceptable else "FAIL"

    print(
        f"\n{result.ticker} {result.direction} "
        f"OPTIONS LIQUIDITY: {status}"
    )
    print(f"Contract: {result.contract_symbol}")
    print(
        f"Expiration: {result.expiration_date} | "
        f"Strike: ${result.strike_price:,.2f}"
    )
    print(
        f"Bid/ask: ${result.bid_price:.2f} / "
        f"${result.ask_price:.2f} | "
        f"Spread: {result.spread_percent:.1f}%"
    )
    print(
        f"Estimated contract cost: "
        f"${result.contract_cost:,.2f}"
    )
    print(f"Open interest: {result.open_interest:,}")

    for failure in result.failed_checks:
        print(f"- {failure}")

    print("Read-only options check. No order was submitted.")
