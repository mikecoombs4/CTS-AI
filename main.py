import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from alpaca_service import show_paper_account_status
from broker_readiness_service import show_broker_readiness
from catalyst_service import show_catalyst_watch
from catalyst_monitor import run_catalyst_monitor
from daily_limits_service import show_daily_limits_simulation
from exit_monitor import run_paper_exit_monitor
from order_preview_service import show_order_preview_simulation
from paper_execution_service import show_paper_execution_lock
from paper_entry_service import show_paper_entry_readiness
from paper_state_service import show_state_recovery_status
from position_tracker import show_exit_simulation
from scanner_service import show_cts_scanner

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


@dataclass
class TradeRisk:
    account_balance: float
    max_risk_dollars: float
    option_price: float
    stop_loss_percent: float
    contracts: int

    def position_cost(self) -> float:
        return self.option_price * 100 * self.contracts

    def estimated_stop_loss(self) -> float:
        return self.position_cost() * (self.stop_loss_percent / 100)

    def worst_case_loss(self) -> float:
        return self.position_cost()

    def account_risk_percent(self) -> float:
        return (
            self.estimated_stop_loss()
            / self.account_balance
            * 100
        )

    def failed_checks(self) -> list[str]:
        failures = []

        if self.position_cost() > self.account_balance:
            failures.append(
                "Option position costs more than the account balance"
            )

        if self.max_risk_dollars > self.account_balance:
            failures.append(
                "Selected risk limit exceeds the account balance"
            )

        if self.estimated_stop_loss() > self.max_risk_dollars:
            failures.append(
                "Estimated stop-loss amount exceeds the selected risk limit"
            )

        return failures

    def approved(self) -> bool:
        return len(self.failed_checks()) == 0


MODE = TradeMode.PAPER
JOURNAL_FILE = Path(__file__).with_name(
    "cts_trade_journal.csv"
)


JOURNAL_FIELDS = [
    "timestamp",
    "mode",
    "ticker",
    "cts_score",
    "cts_approved",
    "final_approved",
    "failed_checks",
    "potter_box_found",
    "trend_confirmed",
    "catalyst_checked",
    "earnings_clear",
    "volume_confirmed",
    "options_liquid",
    "breakout_confirmed",
    "account_balance",
    "max_risk_dollars",
    "option_price",
    "contracts",
    "stop_loss_percent",
    "position_cost",
    "estimated_stop_loss",
    "worst_case_loss",
    "account_risk_percent",
]


def ask_yes_no(question: str) -> bool:
    while True:
        answer = input(
            f"{question} (y/n): "
        ).strip().lower()

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")


def ask_positive_float(question: str) -> float:
    while True:
        answer = input(question).strip().replace(
            "$", ""
        ).replace(",", "")

        try:
            value = float(answer)
        except ValueError:
            print("Please enter a valid number.")
            continue

        if value <= 0:
            print("The number must be greater than zero.")
            continue

        return value


def ask_percentage(question: str) -> float:
    while True:
        value = ask_positive_float(question)

        if value <= 100:
            return value

        print("Enter a percentage from 1 through 100.")


def ask_positive_int(question: str) -> int:
    while True:
        answer = input(question).strip()

        try:
            value = int(answer)
        except ValueError:
            print("Please enter a whole number.")
            continue

        if value <= 0:
            print("The number must be at least 1.")
            continue

        return value


def migrate_journal() -> None:
    if not JOURNAL_FILE.exists():
        return

    if JOURNAL_FILE.stat().st_size == 0:
        return

    with JOURNAL_FILE.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as journal:
        reader = csv.DictReader(journal)
        old_fields = reader.fieldnames or []
        old_rows = list(reader)

    if old_fields == JOURNAL_FIELDS:
        return

    migrated_rows = []

    for old_row in old_rows:
        new_row = {
            field: old_row.get(field, "")
            for field in JOURNAL_FIELDS
        }

        if not new_row["cts_score"]:
            new_row["cts_score"] = old_row.get(
                "score",
                "",
            )

        old_approved = old_row.get(
            "approved",
            "",
        )

        if not new_row["cts_approved"]:
            new_row["cts_approved"] = old_approved

        if not new_row["final_approved"]:
            new_row["final_approved"] = old_approved

        migrated_rows.append(new_row)

    with JOURNAL_FILE.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as journal:
        writer = csv.DictWriter(
            journal,
            fieldnames=JOURNAL_FIELDS,
        )
        writer.writeheader()
        writer.writerows(migrated_rows)


def save_review(
    setup: CTSSetup,
    risk: TradeRisk | None,
    final_approved: bool,
) -> None:
    migrate_journal()

    file_has_data = (
        JOURNAL_FILE.exists()
        and JOURNAL_FILE.stat().st_size > 0
    )

    failures = setup.failed_checks()

    if risk is not None:
        failures.extend(risk.failed_checks())

    row = {
        "timestamp": (
            datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        ),
        "mode": MODE.value,
        "ticker": setup.ticker,
        "cts_score": setup.score(),
        "cts_approved": setup.approved(),
        "final_approved": final_approved,
        "failed_checks": "; ".join(failures),
        "potter_box_found": setup.potter_box_found,
        "trend_confirmed": setup.trend_confirmed,
        "catalyst_checked": setup.catalyst_checked,
        "earnings_clear": setup.earnings_clear,
        "volume_confirmed": setup.volume_confirmed,
        "options_liquid": setup.options_liquid,
        "breakout_confirmed": (
            setup.breakout_confirmed
        ),
        "account_balance": "",
        "max_risk_dollars": "",
        "option_price": "",
        "contracts": "",
        "stop_loss_percent": "",
        "position_cost": "",
        "estimated_stop_loss": "",
        "worst_case_loss": "",
        "account_risk_percent": "",
    }

    if risk is not None:
        row.update(
            {
                "account_balance": (
                    f"{risk.account_balance:.2f}"
                ),
                "max_risk_dollars": (
                    f"{risk.max_risk_dollars:.2f}"
                ),
                "option_price": (
                    f"{risk.option_price:.2f}"
                ),
                "contracts": risk.contracts,
                "stop_loss_percent": (
                    f"{risk.stop_loss_percent:.2f}"
                ),
                "position_cost": (
                    f"{risk.position_cost():.2f}"
                ),
                "estimated_stop_loss": (
                    f"{risk.estimated_stop_loss():.2f}"
                ),
                "worst_case_loss": (
                    f"{risk.worst_case_loss():.2f}"
                ),
                "account_risk_percent": (
                    f"{risk.account_risk_percent():.2f}"
                ),
            }
        )

    with JOURNAL_FILE.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as journal:
        writer = csv.DictWriter(
            journal,
            fieldnames=JOURNAL_FIELDS,
        )

        if not file_has_data:
            writer.writeheader()

        writer.writerow(row)


def collect_cts_setup(ticker: str) -> CTSSetup:
    return CTSSetup(
        ticker=ticker,
        potter_box_found=ask_yes_no(
            "Potter Box/consolidation found"
        ),
        trend_confirmed=ask_yes_no(
            "Trend confirmed"
        ),
        catalyst_checked=ask_yes_no(
            "News and catalyst check completed"
        ),
        earnings_clear=ask_yes_no(
            "No dangerous earnings conflict"
        ),
        volume_confirmed=ask_yes_no(
            "Volume confirmed"
        ),
        options_liquid=ask_yes_no(
            "Options liquidity acceptable"
        ),
        breakout_confirmed=ask_yes_no(
            "Breakout confirmed"
        ),
    )


def collect_trade_risk() -> TradeRisk:
    print("\nCTS PAPER-TRADE RISK GATE")

    account_balance = ask_positive_float(
        "Trading account balance: $"
    )

    max_risk_dollars = ask_positive_float(
        "Maximum estimated loss allowed: $"
    )

    option_price = ask_positive_float(
        "Option contract price shown per share: $"
    )

    stop_loss_percent = ask_percentage(
        "Planned stop-loss percentage: "
    )

    contracts = ask_positive_int(
        "Number of contracts: "
    )

    return TradeRisk(
        account_balance=account_balance,
        max_risk_dollars=max_risk_dollars,
        option_price=option_price,
        stop_loss_percent=stop_loss_percent,
        contracts=contracts,
    )


def review_new_setup() -> None:
    print("\nCTS AI TRADE REVIEW")
    print(f"Mode: {MODE.value}")
    print("No real orders can be submitted.\n")

    ticker = input(
        "Ticker symbol: "
    ).strip().upper()

    while not ticker:
        print("Ticker cannot be blank.")
        ticker = input(
            "Ticker symbol: "
        ).strip().upper()

    setup = collect_cts_setup(ticker)

    print("\nCTS CHECKLIST RESULT")
    print(f"Ticker: {setup.ticker}")
    print(f"CTS score: {setup.score()}/7")

    if not setup.approved():
        print("CTS checklist rejected the setup:")

        for reason in setup.failed_checks():
            print(f"- {reason}")

        save_review(
            setup=setup,
            risk=None,
            final_approved=False,
        )

        print(
            f"\nReview saved to: "
            f"{JOURNAL_FILE.name}"
        )
        print("No real order was submitted.")
        return

    print("CTS checklist passed 7/7.")

    risk = collect_trade_risk()

    print("\nRISK CALCULATION")
    print(
        f"Position cost: "
        f"${risk.position_cost():,.2f}"
    )
    print(
        f"Estimated loss at stop: "
        f"${risk.estimated_stop_loss():,.2f}"
    )
    print(
        f"Maximum selected risk: "
        f"${risk.max_risk_dollars:,.2f}"
    )
    print(
        f"Estimated account risk: "
        f"{risk.account_risk_percent():.2f}%"
    )
    print(
        f"Worst-case loss: "
        f"${risk.worst_case_loss():,.2f}"
    )

    final_approved = risk.approved()

    if final_approved:
        print("\nPAPER TRADE APPROVED")
        print("CTS and risk checks both passed.")
    else:
        print("\nPAPER TRADE REJECTED")

        for reason in risk.failed_checks():
            print(f"- {reason}")

    print(
        "\nWarning: the stop-loss amount is only "
        "an estimate."
    )
    print(
        "An option can gap or fill below the "
        "planned stop."
    )
    print(
        "The full premium paid remains the "
        "worst-case loss."
    )

    save_review(
        setup=setup,
        risk=risk,
        final_approved=final_approved,
    )

    print(
        f"\nReview saved to: "
        f"{JOURNAL_FILE.name}"
    )
    print("No real order was submitted.")


def load_reviews() -> list[dict[str, str]]:
    if not JOURNAL_FILE.exists():
        return []

    with JOURNAL_FILE.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as journal:
        return list(csv.DictReader(journal))


def review_was_approved(
    review: dict[str, str],
) -> bool:
    approved = (
        review.get("final_approved")
        or review.get("approved")
        or ""
    )

    return approved.lower() == "true"


def review_score(
    review: dict[str, str],
) -> int:
    score = (
        review.get("cts_score")
        or review.get("score")
        or "0"
    )

    try:
        return int(score)
    except ValueError:
        return 0


def show_journal() -> None:
    reviews = load_reviews()

    if not reviews:
        print(
            "\nNo CTS reviews have been saved yet."
        )
        return

    approved_reviews = [
        review
        for review in reviews
        if review_was_approved(review)
    ]

    rejected_reviews = [
        review
        for review in reviews
        if not review_was_approved(review)
    ]

    scores = [
        review_score(review)
        for review in reviews
    ]

    ticker_counts = Counter(
        review.get("ticker", "UNKNOWN")
        for review in reviews
    )

    average_score = sum(scores) / len(scores)

    print("\nCTS JOURNAL STATISTICS")
    print(f"Total reviews: {len(reviews)}")
    print(
        f"Approved paper setups: "
        f"{len(approved_reviews)}"
    )
    print(
        f"Rejected paper setups: "
        f"{len(rejected_reviews)}"
    )
    print(
        f"Average CTS score: "
        f"{average_score:.2f}/7"
    )

    print("\nMost reviewed tickers:")

    for ticker, count in ticker_counts.most_common(5):
        print(f"- {ticker}: {count}")

    print("\nMOST RECENT REVIEWS")

    for review in reviews[-10:][::-1]:
        timestamp = review.get(
            "timestamp",
            "",
        )
        ticker = review.get(
            "ticker",
            "UNKNOWN",
        )
        score = review_score(review)

        status = (
            "APPROVED"
            if review_was_approved(review)
            else "REJECTED"
        )

        print(f"\n{timestamp}")
        print(
            f"{ticker} | "
            f"CTS: {score}/7 | "
            f"{status}"
        )

        position_cost = review.get(
            "position_cost",
            "",
        )

        estimated_loss = review.get(
            "estimated_stop_loss",
            "",
        )

        if position_cost:
            print(
                f"Position cost: ${position_cost}"
            )

        if estimated_loss:
            print(
                f"Estimated stop risk: "
                f"${estimated_loss}"
            )

        failed = review.get(
            "failed_checks",
            "",
        )

        if failed:
            print(f"Failed checks: {failed}")


def main() -> None:
    while True:
        print("\n==============================")
        print("CTS AI")
        print(f"MODE: {MODE.value}")
        print("==============================")
        print("1. Review a new setup")
        print("2. Show journal and statistics")
        print("3. Show Alpaca paper account")
        print("4. Run read-only CTS scanner")
        print("5. Run read-only exit simulation")
        print("6. Run read-only daily-limits simulation")
        print("7. Run read-only paper-order preview")
        print("8. Check Alpaca paper readiness")
        print("9. Check paper-execution kill switch")
        print("10. Check paper crash-recovery state")
        print("11. Run automatic PAPER exit monitor")
        print("12. Evaluate PAPER entry readiness (LOCKED)")
        print("13. Exit")
        print("14. Run read-only catalyst watch")
        print("15. Run continuous read-only catalyst monitor")

        choice = input(
            "\nChoose 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, or 15: "
        ).strip()

        if choice == "1":
            review_new_setup()
        elif choice == "2":
            show_journal()
        elif choice == "3":
            show_paper_account_status()
        elif choice == "4":
            show_cts_scanner()
        elif choice == "5":
            show_exit_simulation()
        elif choice == "6":
            show_daily_limits_simulation()
        elif choice == "7":
            show_order_preview_simulation()
        elif choice == "8":
            show_broker_readiness()
        elif choice == "9":
            show_paper_execution_lock()
        elif choice == "10":
            show_state_recovery_status()
        elif choice == "11":
            print(
                "\nStarting PAPER exit monitor. "
                "Press Control+C to stop it safely."
            )
            run_paper_exit_monitor()
        elif choice == "12":
            show_paper_entry_readiness()
        elif choice == "14":
            show_catalyst_watch()
        elif choice == "15":
            run_catalyst_monitor()
        elif choice == "13":
            print("\nCTS AI closed safely.")
            print("No real order was submitted.")
            break
        else:
            print(
                "\nInvalid selection. "
                "Please choose 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, or 15."
            )


if __name__ == "__main__":
    main()
