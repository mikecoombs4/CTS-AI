from dataclasses import dataclass


MAX_TRADES_PER_DAY = 2
MAX_OPEN_POSITIONS = 2
DAILY_LOSS_LIMIT = 50.0


@dataclass
class DailyLimitsResult:
    status: str
    new_trade_allowed: bool
    trades_opened_today: int
    open_positions: int
    losing_trades_today: int
    realized_pnl_today: float
    reasons: list[str]


def evaluate_daily_limits(
    trades_opened_today: int,
    open_positions: int,
    losing_trades_today: int,
    realized_pnl_today: float,
) -> DailyLimitsResult:
    if min(
        trades_opened_today,
        open_positions,
        losing_trades_today,
    ) < 0:
        raise ValueError("Daily trade counts cannot be negative.")

    reasons = []
    realized_loss = max(0.0, -realized_pnl_today)

    if trades_opened_today >= MAX_TRADES_PER_DAY:
        reasons.append(
            f"Daily maximum of {MAX_TRADES_PER_DAY} trades reached"
        )

    if open_positions >= MAX_OPEN_POSITIONS:
        reasons.append(
            f"Maximum of {MAX_OPEN_POSITIONS} open positions reached"
        )

    if losing_trades_today >= 1:
        reasons.append("A realized losing trade ended entries for the day")

    if realized_loss >= DAILY_LOSS_LIMIT:
        reasons.append(
            f"Daily realized-loss limit of ${DAILY_LOSS_LIMIT:.0f} reached"
        )

    return DailyLimitsResult(
        status="BLOCK" if reasons else "PASS",
        new_trade_allowed=not reasons,
        trades_opened_today=trades_opened_today,
        open_positions=open_positions,
        losing_trades_today=losing_trades_today,
        realized_pnl_today=realized_pnl_today,
        reasons=reasons or ["Daily anti-overtrading limits are available"],
    )


def record_closed_trade(
    losing_trades_today: int,
    realized_pnl_today: float,
    trade_pnl: float,
) -> tuple[int, float]:
    if trade_pnl < 0:
        losing_trades_today += 1

    return (
        losing_trades_today,
        realized_pnl_today + trade_pnl,
    )


def _show_case(
    name: str,
    trades: int,
    open_positions: int,
    losses: int,
    pnl: float,
) -> None:
    result = evaluate_daily_limits(
        trades_opened_today=trades,
        open_positions=open_positions,
        losing_trades_today=losses,
        realized_pnl_today=pnl,
    )

    print(f"\n{name}: {result.status}")
    print(
        f"Trades: {trades}/2 | Open: {open_positions}/2 | "
        f"Losing trades: {losses} | P/L: ${pnl:+.2f}"
    )

    for reason in result.reasons:
        print(f"- {reason}")


def show_daily_limits_simulation() -> None:
    print("\nCTS READ-ONLY DAILY LIMITS SIMULATOR")
    print("No broker connection is used. No order can be submitted.")

    _show_case("START OF DAY", 0, 0, 0, 0.0)
    _show_case("ONE OPEN TRADE", 1, 1, 0, 0.0)
    _show_case("TWO OPEN POSITIONS", 2, 2, 0, 0.0)
    _show_case("PROFITABLE TRAILING EXIT", 1, 0, 0, 6.0)
    _show_case("FIRST REALIZED LOSS", 1, 0, 1, -9.0)
    _show_case("TWO TRADES USED", 2, 0, 0, 15.0)

    print("\nSimulation completed. No order was submitted.")
