from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

NEWS_LOOKBACK_HOURS = 72
MAX_HEADLINES = 5

BLOCKING_PHRASES = {
    "bankruptcy",
    "chapter 11",
    "delisting",
    "delist",
    "trading halt",
    "halted",
    "public offering",
    "stock offering",
    "secondary offering",
    "dilution",
    "sec investigation",
    "justice department investigation",
    "fraud investigation",
    "accounting investigation",
    "data breach",
    "cyberattack",
    "product recall",
    "cuts guidance",
    "lowers guidance",
    "withdraws guidance",
    "ceo resigns",
    "cfo resigns",
}

CATALYST_PHRASES = {
    "beats estimates",
    "raises guidance",
    "increases guidance",
    "earnings beat",
    "contract award",
    "wins contract",
    "strategic partnership",
    "share buyback",
    "stock buyback",
    "fda approval",
    "regulatory approval",
    "price target raised",
    "price target cut",
    "upgrade",
    "downgrade",
    "acquisition",
    "merger",
}


@dataclass
class NewsHeadline:
    created_at: datetime | None
    source: str
    headline: str
    blocking_matches: list[str]
    catalyst_matches: list[str]


@dataclass
class NewsRiskResult:
    ticker: str
    status: str
    headlines: list[NewsHeadline]
    blocking_matches: list[str]
    catalyst_matches: list[str]
    provider_query_succeeded: bool = False
    queried_at: datetime | None = None


def _value(source, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)

    return getattr(source, name, default)


def _articles_from_response(response) -> list:
    if isinstance(response, dict):
        if "news" not in response or response["news"] is None:
            raise ValueError("News provider response does not contain a news collection.")
        return list(response["news"])

    articles = _value(response, "news")

    if articles is not None:
        return list(articles)

    data = _value(response, "data", None)

    if isinstance(data, dict):
        if "news" not in data or data["news"] is None:
            raise ValueError("News provider data does not contain a news collection.")
        return list(data["news"])
    raise ValueError("News provider response shape is unavailable or ambiguous.")


def _normalize_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None


def _matches(text: str, phrases: set[str]) -> list[str]:
    lowered = text.lower()
    return sorted(
        phrase
        for phrase in phrases
        if phrase in lowered
    )


def classify_news_articles(
    ticker: str,
    articles: list,
    *,
    provider_query_succeeded: bool = False,
    queried_at: datetime | None = None,
) -> NewsRiskResult:
    headlines = []

    for article in articles:
        headline = str(_value(article, "headline", "")).strip()

        if not headline:
            continue

        summary = str(_value(article, "summary", "")).strip()
        searchable_text = f"{headline} {summary}"
        blocking_matches = _matches(
            searchable_text,
            BLOCKING_PHRASES,
        )
        catalyst_matches = _matches(
            searchable_text,
            CATALYST_PHRASES,
        )
        headlines.append(
            NewsHeadline(
                created_at=_normalize_datetime(
                    _value(article, "created_at")
                ),
                source=str(_value(article, "source", "Unknown")),
                headline=headline,
                blocking_matches=blocking_matches,
                catalyst_matches=catalyst_matches,
            )
        )

    headlines.sort(
        key=lambda item: item.created_at
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    blocking_matches = sorted(
        {
            match
            for item in headlines
            for match in item.blocking_matches
        }
    )
    catalyst_matches = sorted(
        {
            match
            for item in headlines
            for match in item.catalyst_matches
        }
    )

    if blocking_matches:
        status = "BLOCK"
    elif headlines:
        status = "REVIEW"
    else:
        status = "PASS"

    return NewsRiskResult(
        ticker=ticker,
        status=status,
        headlines=headlines,
        blocking_matches=blocking_matches,
        catalyst_matches=catalyst_matches,
        provider_query_succeeded=provider_query_succeeded is True,
        queried_at=queried_at,
    )


def evaluate_news_risk(ticker: str) -> NewsRiskResult:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    from alpaca_service import get_alpaca_credentials

    api_key, secret_key = get_alpaca_credentials()
    client = NewsClient(api_key, secret_key)
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(
        hours=NEWS_LOOKBACK_HOURS
    )
    response = client.get_news(
        NewsRequest(
            symbols=ticker,
            start=start_time,
            end=end_time,
            limit=MAX_HEADLINES,
        )
    )

    return classify_news_articles(
        ticker=ticker,
        articles=_articles_from_response(response),
        provider_query_succeeded=True,
        queried_at=end_time,
    )


def show_news_risk(result: NewsRiskResult) -> None:
    print(f"\n{result.ticker} NEWS RISK: {result.status}")

    if not result.headlines:
        print(
            f"No ticker-specific headlines found in the last "
            f"{NEWS_LOOKBACK_HOURS} hours."
        )
        print("No headline risk was detected automatically.")
        return

    for item in result.headlines:
        if item.created_at is None:
            timestamp = "Unknown time"
        else:
            timestamp = item.created_at.astimezone().strftime(
                "%b %d %I:%M %p"
            )

        print(
            f"- [{timestamp}] {item.source}: "
            f"{item.headline}"
        )

    if result.blocking_matches:
        print(
            "Blocking phrases: "
            + ", ".join(result.blocking_matches)
        )

    if result.catalyst_matches:
        print(
            "Catalyst phrases: "
            + ", ".join(result.catalyst_matches)
        )

    if result.status == "REVIEW":
        print(
            "Recent news exists. Human or AI review is required "
            "before paper-order approval."
        )

    print("Read-only news check. No order was submitted.")
