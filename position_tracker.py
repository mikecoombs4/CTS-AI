from dataclasses import dataclass

from exit_service import ExitDecision, evaluate_exit


EXIT_ACTIONS = {
    "EXIT_INITIAL_STOP",
    "EXIT_TRAILING_STOP",
    "EXIT_TARGET",
}


@dataclass
class SimulatedPosition:
    entry_price: float
    peak_price: float
    trailing_active: bool = False
    closed: bool = False
    exit_action: str | None = None
    exit_price: float | None = None


def update_position(
    position: SimulatedPosition,
    current_price: float,
) -> ExitDecision:
    if position.closed:
        raise RuntimeError("The simulated position is already closed.")

    decision = evaluate_exit(
        entry_price=position.entry_price,
        current_price=current_price,
        peak_price=position.peak_price,
        trailing_active=position.trailing_active,
    )
    position.peak_price = decision.peak_price
    position.trailing_active = decision.trailing_active

    if decision.action in EXIT_ACTIONS:
        position.closed = True
        position.exit_action = decision.action
        position.exit_price = current_price

    return decision


def simulate_price_path(
    entry_price: float,
    prices: list[float],
) -> tuple[SimulatedPosition, list[tuple[float, ExitDecision]]]:
    position = SimulatedPosition(
        entry_price=entry_price,
        peak_price=entry_price,
    )
    steps = []

    for price in prices:
        decision = update_position(position, price)
        steps.append((price, decision))

        if position.closed:
            break

    return position, steps


def _show_scenario(
    name: str,
    entry_price: float,
    prices: list[float],
) -> None:
    position, steps = simulate_price_path(
        entry_price=entry_price,
        prices=prices,
    )

    print(f"\n{name}")
    print(f"Simulated entry: ${entry_price:.2f}")

    for price, decision in steps:
        line = f"${price:.2f} -> {decision.action}"

        if decision.trailing_stop_price is not None:
            line += (
                f" | Trail: "
                f"${decision.trailing_stop_price:.2f}"
            )

        print(line)

    if position.closed:
        profit = (position.exit_price - entry_price) * 100
        print(
            f"Simulated exit: ${position.exit_price:.2f} | "
            f"P/L: ${profit:+.2f}"
        )
    else:
        print("Simulation ended with the position still open.")


def show_exit_simulation() -> None:
    print("\nCTS READ-ONLY EXIT SIMULATOR")
    print("No broker connection is used. No order can be submitted.")

    _show_scenario(
        name="SCENARIO 1: +35% TARGET EXIT",
        entry_price=0.35,
        prices=[0.35, 0.38, 0.42, 0.45, 0.48],
    )
    _show_scenario(
        name="SCENARIO 2: 10% TRAILING-STOP EXIT",
        entry_price=0.35,
        prices=[0.35, 0.42, 0.46, 0.41],
    )
    _show_scenario(
        name="SCENARIO 3: 25% INITIAL-STOP EXIT",
        entry_price=0.35,
        prices=[0.35, 0.31, 0.26],
    )

    print("\nSimulation completed. No order was submitted.")
