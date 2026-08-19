from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from news_service import BLOCKING_PHRASES, CATALYST_PHRASES
from watchlist_service import resolve_watchlist

BREAKING_MAX_AGE = timedelta(minutes=90)
RECENT_MAX_AGE = timedelta(hours=18)
NEWS_LOOKBACK = timedelta(hours=72)
MAX_HEADLINES = 20
MAX_DISPLAY_HEADLINES = 3
MANY_SYMBOLS_THRESHOLD = 4
EASTERN_TIMEZONE = ZoneInfo("America/New_York")

ADVERSE_CATALYST_PHRASES = {
    "downgrade",
    "lowers guidance",
    "price target cut",
    "withdraws guidance",
}

OFFERING_PHRASES = {
    "public offering",
    "secondary offering",
    "registered direct offering",
    "atm offering",
    "at-the-market offering",
    "prices offering",
    "share dilution",
}

MATERIAL_FAVORABLE_PHRASES = {
    "fda decision",
    "fda approves",
    "fda approval",
    "contract award",
    "wins contract",
    "earnings result",
    "quarterly results",
    "financial results",
    "merger",
    "acquisition",
}

ROUNDUP_PHRASES = {
    "stocks moving",
    "pre-market session",
    "stock market today",
    "what's going on with",
    "whats going on with",
    "biggest gainers",
    "biggest losers",
    "midday market update",
    "market recap",
}

SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass
class CatalystHeadline:
    ticker: str
    created_at: datetime
    age: timedelta
    freshness: str
    event_type: str
    classification: str
    source: str
    headline: str
    provider_symbols: list[str]
    relevance: str
    is_material: bool
    article_id: str | None = None


@dataclass
class CatalystWatchResult:
    ticker: str
    status: str
    headlines: list[CatalystHeadline]
    reason: str = ""

    @property
    def material_headlines(self) -> list[CatalystHeadline]:
        return [item for item in self.headlines if item.is_material]

    @property
    def suppressed_count(self) -> int:
        return len(self.headlines) - len(self.material_headlines)


def normalize_symbol(value) -> str | None:
    symbol = str(value or "").strip().upper()
    if symbol.startswith("$"):
        symbol = symbol[1:]
    return symbol if SYMBOL_PATTERN.fullmatch(symbol) else None


def _value(source, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _articles_from_response(response) -> list:
    if isinstance(response, dict):
        return list(response.get("news", []) or [])

    articles = _value(response, "news")
    if articles is not None:
        return list(articles)

    data = _value(response, "data", {})
    if isinstance(data, dict):
        return list(data.get("news", []) or [])

    try:
        return list(response)
    except TypeError:
        return []


def _normalize_symbols(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    try:
        symbols = [normalize_symbol(value) for value in (values or [])]
    except TypeError:
        return []
    return sorted({symbol for symbol in symbols if symbol})


def _normalize_timestamp(value, now: datetime) -> datetime | None:
    if isinstance(value, datetime):
        timestamp = value
    elif value:
        try:
            timestamp = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return None
    else:
        return None

    if timestamp.tzinfo is None:
        return None

    timestamp = timestamp.astimezone(timezone.utc)
    if timestamp > now:
        return None
    return timestamp


def _freshness(age: timedelta) -> str:
    if age <= BREAKING_MAX_AGE:
        return "BREAKING"
    if age <= RECENT_MAX_AGE:
        return "RECENT"
    return "STALE"


def _matches(text: str, phrases: set[str]) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    matches = set()
    for phrase in phrases:
        phrase_words = re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip()
        phrase_pattern = re.escape(phrase_words).replace("\\ ", r"\s+")
        if re.search(
            rf"(?<![a-z0-9]){phrase_pattern}(?![a-z0-9])",
            normalized,
        ):
            matches.add(phrase)
    return matches


def _event_type(text: str) -> str:
    categories = (
        ("earnings", {"earnings", "quarterly results", "financial results"}),
        ("fda/regulatory", {"fda", "regulatory approval", "clinical trial"}),
        ("contract/deal", {"contract", "partnership", "agreement", "deal"}),
        ("analyst action", {"upgrade", "downgrade", "price target"}),
        ("legal/investigation", {"lawsuit", "investigation", "subpoena", "fraud"}),
        ("offering/dilution", OFFERING_PHRASES),
        ("merger/acquisition", {"acquisition", "acquire", "merger"}),
        ("management", {"ceo", "cfo", "chief executive", "chief financial"}),
    )
    for event_type, phrases in categories:
        if _matches(text, phrases):
            return event_type
    return "general news"


def _classification(text: str) -> str:
    blocking = _matches(text, BLOCKING_PHRASES)
    adverse = _matches(text, ADVERSE_CATALYST_PHRASES)
    offering = _matches(text, OFFERING_PHRASES)
    favorable = (
        _matches(text, CATALYST_PHRASES)
        | _matches(text, MATERIAL_FAVORABLE_PHRASES)
    ) - adverse - offering

    if blocking or adverse or offering:
        return "ADVERSE"
    if favorable:
        return "FAVORABLE"
    return "INFORMATIONAL"


def _is_roundup(text: str) -> bool:
    return bool(_matches(text, ROUNDUP_PHRASES))


def _provider_symbols(article) -> list[str]:
    return _normalize_symbols(
        _value(article, "symbols", _value(article, "symbol", []))
    )


def _provider_article_id(article) -> str | None:
    for field in ("id", "article_id", "news_id"):
        value = _value(article, field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _relevance(ticker: str, provider_symbols: list[str]) -> str:
    if len(provider_symbols) >= MANY_SYMBOLS_THRESHOLD:
        return "BROAD/MULTI_SYMBOL"
    if provider_symbols and ticker not in provider_symbols:
        return "UNRELATED"
    return "DIRECT"


def _fetch_articles(ticker: str, start: datetime, end: datetime) -> list:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    from alpaca_service import get_alpaca_credentials

    api_key, secret_key = get_alpaca_credentials()
    client = NewsClient(api_key, secret_key)
    response = client.get_news(
        NewsRequest(
            symbols=ticker,
            start=start,
            end=end,
            limit=MAX_HEADLINES,
        )
    )
    return _articles_from_response(response)


def _build_result(
    ticker: str,
    articles: list,
    now: datetime,
) -> CatalystWatchResult:
    headlines: list[CatalystHeadline] = []
    seen: set[tuple] = set()

    for article in articles:
        headline = str(_value(article, "headline", "")).strip()
        timestamp = _normalize_timestamp(
            _value(article, "created_at"),
            now,
        )
        if not headline or timestamp is None:
            continue

        source = str(_value(article, "source", "Unknown")).strip() or "Unknown"
        dedupe_key = (
            timestamp,
            source.casefold(),
            headline.casefold(),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        age = now - timestamp
        text = f"{headline} {str(_value(article, 'summary', '')).strip()}"
        provider_symbols = _provider_symbols(article)
        relevance = _relevance(ticker, provider_symbols)
        event_type = _event_type(text)
        classification = (
            "INFORMATIONAL"
            if _is_roundup(text)
            else _classification(text)
        )
        headlines.append(
            CatalystHeadline(
                ticker=ticker,
                created_at=timestamp,
                age=age,
                freshness=_freshness(age),
                event_type=event_type,
                classification=classification,
                source=source,
                headline=headline,
                provider_symbols=provider_symbols,
                relevance=relevance,
                is_material=(
                    relevance == "DIRECT"
                    and not _is_roundup(text)
                    and event_type != "general news"
                    and classification in {"FAVORABLE", "ADVERSE"}
                ),
                article_id=_provider_article_id(article),
            )
        )

    headlines.sort(key=lambda item: item.created_at, reverse=True)
    if not headlines:
        return CatalystWatchResult(
            ticker=ticker,
            status="UNAVAILABLE",
            headlines=[],
            reason="No usable catalyst data was returned.",
        )

    material_headlines = [item for item in headlines if item.is_material]
    if material_headlines:
        newest_material = material_headlines[0]
        status = f"MATERIAL {newest_material.freshness}"
    else:
        status = "NO MATERIAL CATALYST"

    return CatalystWatchResult(
        ticker=ticker,
        status=status,
        headlines=headlines,
    )


def evaluate_catalyst_watch(
    tickers: Iterable[str] | str | None = None,
    now: datetime | None = None,
) -> list[CatalystWatchResult]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Catalyst-watch time must include a timezone.")
    now = now.astimezone(timezone.utc)
    end = now
    start = now - NEWS_LOOKBACK
    results = []
    seen_symbols = set()
    resolved_tickers = resolve_watchlist(tickers)

    for ticker in resolved_tickers:
        if ticker in seen_symbols:
            continue
        seen_symbols.add(ticker)
        try:
            articles = _fetch_articles(ticker, start, end)
            result = _build_result(ticker, articles, now)
        except Exception as error:
            result = CatalystWatchResult(
                ticker=ticker,
                status="UNAVAILABLE",
                headlines=[],
                reason=f"Catalyst provider unavailable: {error}",
            )
        results.append(result)

    return results


def show_catalyst_watch() -> None:
    print("\nCTS READ-ONLY CATALYST WATCH")
    raw_symbols = input(
        "Ticker symbols (comma-separated, blank = shared watchlist): "
    )
    results = evaluate_catalyst_watch(raw_symbols)

    for result in results:
        print(f"\n{result.ticker}: {result.status}")
        if not result.headlines:
            print(f"- {result.reason}")
            continue
        material_headlines = result.material_headlines[:MAX_DISPLAY_HEADLINES]
        print(f"Material articles: {len(result.material_headlines)}")
        for item in material_headlines:
            eastern_time = item.created_at.astimezone(EASTERN_TIMEZONE).strftime(
                "%Y-%m-%d %I:%M:%S %p %Z"
            )
            age_minutes = item.age.total_seconds() / 60
            print(
                f"- {eastern_time} | age {age_minutes:.0f} minutes | "
                f"{item.freshness} | {item.event_type} | "
                f"{item.classification} | {item.source}: {item.headline}"
            )
        print(
            f"Suppressed informational/broad articles: "
            f"{result.suppressed_count}"
        )

    print("\nCATALYST DATA IS INFORMATIONAL ONLY — NO ORDER WAS SUBMITTED.")
