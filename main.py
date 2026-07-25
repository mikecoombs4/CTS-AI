from dataclasses import dataclass
from enum import Enum


class TradeMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class CTSSetup:
    ticker: str
    potter_box_found: bool
    trend_confirmed: bool
    catalyst_checked: bool
    earnings_clear: bool
    volume_confirmed: bool
    options_liquid: bool
    breakout_confirmed: bool

    def score(self) -> int:
        checks = [
            self.potter_box_found,
            self.trend_confirmed,
            self.catalyst_checked,
            self.earnings_clear,
            self.volume_confirmed,
            self.options_liquid,
            self.breakout_confirmed,
        ]
        return sum(checks)

    def approved(self) -> bool:
        return self.score() == 7

    def failed_checks(self) -> list[str]:
        checks = {
            "Potter Box not found": self.potter_box_found,
            "Trend not confirmed": self.trend_confirmed,
            "Catalyst/news not checked": self.catalyst_checked,
            "Upcoming earnings conflict": self.earnings_clear,
            "Volume not confirmed": self.volume_confirmed,
            "Options are not liquid": self.options_liquid,
            "Breakout not confirmed": self.breakout_confirmed,
        }

        return [
            reason
            for reason, passed in checks.items()
            if not passed
        ]


MODE = TradeMode.PAPER


def ask_yes_no(question: str) -> bool:
    while True:
        answer = input(f"{question} (y/n): ").strip().lower()

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")


def main() -> None:
    print("\nCTS AI Trade Review")
    print(f"Mode: {MODE.value}")
    print("No real orders can be submitted.\n")

    ticker = input("Ticker symbol: ").strip().upper()

    while not ticker:
        print("Ticker cannot be blank.")
        ticker = input("Ticker symbol: ").strip().upper()

    setup = CTSSetup(
        ticker=ticker,
        potter_box_found=ask_yes_no("Potter Box/consolidation found"),
        trend_confirmed=ask_yes_no("Trend confirmed"),
        catalyst_checked=ask_yes_no("News and catalyst check completed"),
        earnings_clear=ask_yes_no("No dangerous earnings conflict"),
        volume_confirmed=ask_yes_no("Volume confirmed"),
        options_liquid=ask_yes_no("Options liquidity acceptable"),
        breakout_confirmed=ask_yes_no("Breakout confirmed"),
    )

    print("\nCTS REVIEW RESULT")
    print(f"Ticker: {setup.ticker}")
    print(f"CTS score: {setup.score()}/7")
    print(f"Trade approved: {setup.approved()}")

    if setup.approved():
        print("Setup passed every CTS check.")
    else:
        print("Trade rejected because:")
        for reason in setup.failed_checks():
            print(f"- {reason}")

    print("\nNo real order was submitted.")


if __name__ == "__main__":
    main()