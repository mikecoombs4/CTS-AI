from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MORNING_START = time(9, 45)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(15, 30)
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
    elif MORNING_START <= current_time <= MORNING_END:
        return MarketSessionResult(
            status="PASS",
            market_time=market_time,
            entry_allowed=True,
            reason="Morning CTS entry window is open.",
        )
    elif current_time < AFTERNOON_START:
        reason = "Lunch window: new CTS entries are disabled."
    elif AFTERNOON_START <= current_time <= AFTERNOON_END:
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
