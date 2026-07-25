import csv
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


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
JOURNAL_FILE = Path("cts_trade_journal.csv")


def ask_yes_no(question: str) -> bool:
    while True:
        answer = input(f"{question} (y/n): ").strip().lower()

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")


def save_review(setup: CTSSetup) -> None:
    file_already_exists = JOURNAL_FILE.exists()

    fieldnames = [
        "timestamp",
        "mode",
        "ticker",
        "score",
        "approved",
        "failed_checks",
        "potter_box_found",
        "trend_confirmed",
        "catalyst_checked",
        "earnings_clear",
        "volume_confirmed",
        "options_liquid",
        "breakout_confirmed",
    ]

    row = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": MODE.value,
        "ticker": setup.ticker,
        "score": setup.score(),
        "approved": setup.approved(),
        "failed_checks": "; ".join(setup.failed_checks()),
        "potter_box_found": setup.potter_box_found,
        "trend_confirmed": setup.trend_confirmed,
        "catalyst_checked": setup.catalyst_checked,
        "earnings_clear": setup.earnings_clear,
        "volume_confirmed": setup.volume_confirmed,
        "options_liquid": setup.options_liquid,
        "breakout_confirmed": setup.breakout_confirmed,
    }

    with JOURNAL_FILE.open("a", newline="", encoding="utf-8") as journal:
        writer = csv.DictWriter(journal, fieldnames=fieldnames)

        if not file_already_exists:
            writer.writeheader()

        writer.writerow(row)


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

    save_review(setup)

    print(f"\nReview saved to: {JOURNAL_FILE}")
    print("No real order was submitted.")


if __name__ == "__main__":
    main()