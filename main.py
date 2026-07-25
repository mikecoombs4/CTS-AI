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
            "Catalyst not checked": self.catalyst_checked,
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

test_setup = CTSSetup(
ticker="TEST",
potter_box_found=True,
trend_confirmed=True,
catalyst_checked=True,
earnings_clear=True,
volume_confirmed=True,
options_liquid=True,
breakout_confirmed=False,
)

print("CTS AI started")
print(f"Mode: {MODE.value}")
print(f"Ticker: {test_setup.ticker}")
print(f"CTS score: {test_setup.score()}/7")
print(f"Trade approved: {test_setup.approved()}")

if test_setup.approved():
    print("Setup passed every CTS check.")
else:
    print("Trade rejected because:")
    for reason in test_setup.failed_checks():
        print(f"- {reason}")

print("No real order was submitted.")