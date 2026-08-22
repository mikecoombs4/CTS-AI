from dataclasses import dataclass
from datetime import datetime, time, timezone
from cts_entry_window import (
    AFTERNOON_ENTRY_START as AFTERNOON_START,
    AFTERNOON_ENTRY_END as AFTERNOON_END,
    MARKET_TIMEZONE,
    MORNING_ENTRY_START as MORNING_START,
    MORNING_ENTRY_END as MORNING_END,
    cts_entry_window_open,
)


MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass
class MarketSessionResult:
    status: str
    market_time: datetime
    entry_allowed: bool
    reason: str


def evaluate_market_session(
    now: datetime | None = None,
) -> MarketSessionResult:
    now = now or datetime.now(timezone.utc)

    if now.tzinfo is None:
        raise ValueError("Market-session time must include a timezone.")

    market_time = now.astimezone(MARKET_TIMEZONE)
    current_time = market_time.time().replace(tzinfo=None)

    if market_time.weekday() >= 5:
        reason = "Weekend: the stock market is closed."
    elif current_time < MARKET_OPEN:
        reason = "Premarket: new CTS entries are disabled."
    elif current_time < MORNING_START:
        reason = "First 15 minutes: new CTS entries are disabled."
    elif cts_entry_window_open(now) and current_time < MORNING_END:
        return MarketSessionResult(
            status="PASS",
            market_time=market_time,
            entry_allowed=True,
            reason="Morning CTS entry window is open.",
        )
    elif current_time < AFTERNOON_START:
        reason = "Lunch window: new CTS entries are disabled."
    elif cts_entry_window_open(now):
        return MarketSessionResult(
            status="PASS",
            market_time=market_time,
            entry_allowed=True,
            reason="Afternoon CTS entry window is open.",
        )
    elif current_time < MARKET_CLOSE:
        reason = "After 3:30 PM: no new CTS positions."
    else:
        reason = "The regular market session is closed."

    return MarketSessionResult(
        status="BLOCK",
        market_time=market_time,
        entry_allowed=False,
        reason=reason,
    )


def show_market_session(result: MarketSessionResult) -> None:
    print(f"\nMARKET SESSION: {result.status}")
    print(
        "Current market time: "
        + result.market_time.strftime("%a %b %d, %Y %I:%M %p ET")
    )
    print(result.reason)
    print(
        "Allowed entry windows: 9:45-11:30 AM and "
        "1:00-3:30 PM ET."
    )
    print("Time check only. No order was submitted.")
