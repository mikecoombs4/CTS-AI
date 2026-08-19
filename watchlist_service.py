from pathlib import Path
import re
from typing import Iterable

from dotenv import dotenv_values

ENV_FILE = Path(__file__).with_name(".env")
WATCHLIST_VARIABLE = "CTS_WATCHLIST"

DEFAULT_WATCHLIST = [
    "QQQ",
    "IWM",
    "SPY",
    "NVDA",
    "AMD",
    "SMCI",
    "AVGO",
    "MU",
    "ARM",
    "INTC",
    "PLTR",
    "SOFI",
    "RIVN",
    "SOUN",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "NFLX",
]

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


def normalize_symbol(value) -> str | None:
    symbol = str(value or "").strip().upper()
    if symbol.startswith("$"):
        symbol = symbol[1:]
    if not SYMBOL_PATTERN.fullmatch(symbol):
        return None
    return symbol


def normalize_watchlist(values: Iterable[str] | str | None) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")

    normalized = []
    seen = set()
    try:
        values = values or []
        for value in values:
            symbol = normalize_symbol(value)
            if symbol is not None and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
    except TypeError:
        return []

    return normalized


def load_watchlist() -> list[str]:
    configured = normalize_watchlist(
        dotenv_values(ENV_FILE).get(WATCHLIST_VARIABLE)
    )
    if configured:
        return configured
    return list(DEFAULT_WATCHLIST)


def resolve_watchlist(
    override: Iterable[str] | str | None = None,
) -> list[str]:
    explicit = normalize_watchlist(override)
    if explicit:
        return explicit

    configured = load_watchlist()
    if configured:
        return list(configured)

    return list(DEFAULT_WATCHLIST)
