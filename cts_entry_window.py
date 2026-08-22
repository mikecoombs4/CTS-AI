"""Authoritative timezone-aware CTS entry windows."""

from datetime import datetime, time
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")
MORNING_ENTRY_START = time(9, 45)
MORNING_ENTRY_END = time(11, 30)
AFTERNOON_ENTRY_START = time(13, 0)
AFTERNOON_ENTRY_END = time(15, 30)
FORCED_0DTE_EXIT = time(15, 55)


def cts_entry_window_open(value: datetime) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("CTS entry-window time must be timezone-aware.")
    eastern = value.astimezone(MARKET_TIMEZONE)
    current = eastern.time().replace(tzinfo=None)
    return eastern.weekday() < 5 and (
        MORNING_ENTRY_START <= current < MORNING_ENTRY_END
        or AFTERNOON_ENTRY_START <= current < AFTERNOON_ENTRY_END
    )


def forced_0dte_exit_due(value: datetime) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Forced-exit time must be timezone-aware.")
    eastern = value.astimezone(MARKET_TIMEZONE)
    return eastern.weekday() < 5 and eastern.time().replace(tzinfo=None) >= FORCED_0DTE_EXIT
