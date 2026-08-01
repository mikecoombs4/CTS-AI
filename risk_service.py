from dataclasses import dataclass

from exit_service import round_up_to_cent


MAX_CONTRACT_COST = 150.0
STOP_LOSS_PERCENT = 25.0
DAILY_LOSS_LIMIT = 50.0
MAX_CONTRACTS = 1
TP1_GAIN_PERCENT = 20.0
TP2_GAIN_PERCENT = 35.0
TRAILING_STOP_PERCENT = 10.0


@dataclass
class TradePlan:
    ticker: str
    contract_symbol: str
    contracts: int
    entry_price: float
    position_cost: float
    stop_price: float
    estimated_stop_loss: float
    target_1_price: float
    target_2_price: float
    realized_loss_today: float
    remaining_daily_loss_budget: float
    acceptable: bool
    failed_checks: list[str]


def build_trade_plan(
    ticker: str,
    contract_symbol: str,
    entry_price: float,
    realized_pnl_today: float = 0.0,
) -> TradePlan:
    contracts = MAX_CONTRACTS
    position_cost = entry_price * 100 * contracts
    stop_price = round_up_to_cent(
        entry_price * (1 - STOP_LOSS_PERCENT / 100)
    )
    estimated_stop_loss = position_cost * (
        STOP_LOSS_PERCENT / 100
    )
    target_1_price = round_up_to_cent(
        entry_price * (1 + TP1_GAIN_PERCENT / 100)
    )
    target_2_price = round_up_to_cent(
        entry_price * (1 + TP2_GAIN_PERCENT / 100)
    )
    realized_loss_today = max(
        0.0,
        -realized_pnl_today,
    )
    remaining_daily_loss_budget = max(
        0.0,
        DAILY_LOSS_LIMIT - realized_loss_today,
    )
    failures = []

    if entry_price <= 0:
        failures.append("Option entry price is not usable")

    if position_cost > MAX_CONTRACT_COST:
        failures.append(
            f"Position cost exceeds ${MAX_CONTRACT_COST:.0f} cap"
        )

    if realized_loss_today >= DAILY_LOSS_LIMIT:
        failures.append(
            f"Daily realized-loss limit of ${DAILY_LOSS_LIMIT:.0f} reached"
        )
    elif estimated_stop_loss > remaining_daily_loss_budget:
        failures.append(
            "Planned stop could exceed the remaining daily-loss budget"
        )

    return TradePlan(
        ticker=ticker,
        contract_symbol=contract_symbol,
        contracts=contracts,
        entry_price=entry_price,
        position_cost=position_cost,
        stop_price=max(0.01, stop_price),
        estimated_stop_loss=estimated_stop_loss,
        target_1_price=target_1_price,
        target_2_price=target_2_price,
        realized_loss_today=realized_loss_today,
        remaining_daily_loss_budget=remaining_daily_loss_budget,
        acceptable=not failures,
        failed_checks=failures,
    )


def show_trade_plan(plan: TradePlan) -> None:
    status = "PASS" if plan.acceptable else "FAIL"

    print(f"\n{plan.ticker} RISK PLAN: {status}")
    print(f"Contract: {plan.contract_symbol}")
    print(
        f"Quantity: {plan.contracts} | "
        f"Estimated entry: ${plan.entry_price:.2f}"
    )
    print(f"Position cost: ${plan.position_cost:,.2f}")
    print(
        f"25% stop: ${plan.stop_price:.2f} | "
        f"Estimated loss: ${plan.estimated_stop_loss:,.2f}"
    )
    print(
        f"Target 1 (+20%): ${plan.target_1_price:.2f} | "
        f"Target 2 (+35%): ${plan.target_2_price:.2f}"
    )
    print(
        f"At Target 1: activate a {TRAILING_STOP_PERCENT:.0f}% "
        "trailing stop; force exit at Target 2."
    )
    print(
        f"Daily loss used: ${plan.realized_loss_today:,.2f} / "
        f"${DAILY_LOSS_LIMIT:,.2f}"
    )
    print(
        "Remaining daily-loss capacity: "
        f"${plan.remaining_daily_loss_budget:,.2f}"
    )

    for failure in plan.failed_checks:
        print(f"- {failure}")

    print(
        "Planning estimate only. Stops can slip and the full "
        "premium remains at risk."
    )
    print("Read-only risk check. No order was submitted.")
