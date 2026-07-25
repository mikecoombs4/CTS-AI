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


MODE = TradeMode.PAPER

test_setup = CTSSetup(
    ticker="TEST",
    potter_box_found=True,
    trend_confirmed=True,
    catalyst_checked=True,
    earnings_clear=True,
    volume_confirmed=True,
    options_liquid=True,
    breakout_confirmed=True,
)

print("CTS AI started")
print(f"Mode: {MODE.value}")
print(f"Ticker: {test_setup.ticker}")
print(f"CTS score: {test_setup.score()}/7")
print(f"Trade approved: {test_setup.approved()}")
print("No real order was submitted.")