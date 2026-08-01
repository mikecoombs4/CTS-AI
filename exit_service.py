from dataclasses import dataclass
from math import ceil


INITIAL_STOP_PERCENT = 25.0
TRAIL_ACTIVATION_GAIN_PERCENT = 20.0
TRAILING_STOP_PERCENT = 10.0
HARD_TARGET_GAIN_PERCENT = 35.0


@dataclass
class ExitDecision:
    action: str
    trailing_active: bool
    peak_price: float
    trailing_stop_price: float | None
    reason: str


def round_up_to_cent(price: float) -> float:
    return ceil(price * 100 - 1e-9) / 100


def evaluate_exit(
    entry_price: float,
    current_price: float,
    peak_price: float | None = None,
    trailing_active: bool = False,
) -> ExitDecision:
    if entry_price <= 0 or current_price < 0:
        raise ValueError("Entry and current prices must be usable.")

    peak_price = max(
        entry_price,
        peak_price or entry_price,
        current_price,
    )
    initial_stop_price = round_up_to_cent(
        entry_price * (1 - INITIAL_STOP_PERCENT / 100)
    )
    activation_price = round_up_to_cent(
        entry_price * (1 + TRAIL_ACTIVATION_GAIN_PERCENT / 100)
    )
    hard_target_price = round_up_to_cent(
        entry_price * (1 + HARD_TARGET_GAIN_PERCENT / 100)
    )

    if current_price >= hard_target_price:
        return ExitDecision(
            action="EXIT_TARGET",
            trailing_active=trailing_active,
            peak_price=peak_price,
            trailing_stop_price=(
                round_up_to_cent(
                    peak_price * (1 - TRAILING_STOP_PERCENT / 100)
                )
                if trailing_active
                else None
            ),
            reason="The +35% hard profit target was reached.",
        )

    if not trailing_active and current_price <= initial_stop_price:
        return ExitDecision(
            action="EXIT_INITIAL_STOP",
            trailing_active=False,
            peak_price=peak_price,
            trailing_stop_price=None,
            reason="The initial 25% stop level was reached.",
        )

    if not trailing_active and current_price >= activation_price:
        trailing_stop_price = round_up_to_cent(
            current_price * (1 - TRAILING_STOP_PERCENT / 100)
        )

        return ExitDecision(
            action="ARM_TRAILING_STOP",
            trailing_active=True,
            peak_price=current_price,
            trailing_stop_price=trailing_stop_price,
            reason=(
                "The contract gained 20%; the 10% trailing "
                "stop is now active."
            ),
        )

    if trailing_active:
        trailing_stop_price = round_up_to_cent(
            peak_price * (1 - TRAILING_STOP_PERCENT / 100)
        )

        if current_price <= trailing_stop_price:
            return ExitDecision(
                action="EXIT_TRAILING_STOP",
                trailing_active=True,
                peak_price=peak_price,
                trailing_stop_price=trailing_stop_price,
                reason="Price reached the active trailing stop.",
            )

        return ExitDecision(
            action="HOLD_TRAILING",
            trailing_active=True,
            peak_price=peak_price,
            trailing_stop_price=trailing_stop_price,
            reason="Trailing the highest price toward the +35% target.",
        )

    return ExitDecision(
        action="HOLD_INITIAL",
        trailing_active=False,
        peak_price=peak_price,
        trailing_stop_price=None,
        reason="Waiting for the initial stop or +20% activation level.",
    )
