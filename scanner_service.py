from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

WATCHLIST = [
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

BAR_MINUTES = 15
LOOKBACK_DAYS = 14
BOX_BARS = 8
MAX_BOX_WIDTH_PERCENT = 3.0
VOLUME_SURGE_MULTIPLIER = 1.5
MARKET_TIMEZONE = ZoneInfo("America/New_York")


@dataclass
class ScannerResult:
    ticker: str
    bar_timestamp: datetime
    direction: str
    last_price: float
    ema_9: float
    ema_20: float
    box_high: float
    box_low: float
    box_width_percent: float
    volume_ratio: float
    trend_confirmed: bool
    potter_box_found: bool
    volume_confirmed: bool
    breakout_confirmed: bool

    def score(self) -> int:
        checks = [
            self.trend_confirmed,
            self.potter_box_found,
            self.volume_confirmed,
            self.breakout_confirmed,
        ]
        return sum(checks)

    def technical_candidate(self) -> bool:
        return self.score() == 4


def calculate_ema(
    values: list[float],
    period: int,
) -> float:
    if not values:
        raise ValueError("EMA requires at least one value.")

    multiplier = 2 / (period + 1)
    ema = values[0]

    for value in values[1:]:
        ema = (
            value * multiplier
            + ema * (1 - multiplier)
        )

    return ema


def analyze_bars(
    ticker: str,
    bars: list[Any],
) -> ScannerResult | None:
    minimum_bars = max(20, BOX_BARS) + 1

    if len(bars) < minimum_bars:
        return None

    latest = bars[-1]
    previous_bars = bars[:-1]
    box_bars = previous_bars[-BOX_BARS:]
    latest_market_time = latest.timestamp.astimezone(
        MARKET_TIMEZONE
    )
    comparable_volume_bars = [
        bar
        for bar in previous_bars
        if (
            bar.timestamp.astimezone(
                MARKET_TIMEZONE
            ).hour
            == latest_market_time.hour
            and bar.timestamp.astimezone(
                MARKET_TIMEZONE
            ).minute
            == latest_market_time.minute
        )
    ][-10:]

    closes = [float(bar.close) for bar in bars]
    box_high = max(float(bar.high) for bar in box_bars)
    box_low = min(float(bar.low) for bar in box_bars)
    box_midpoint = (box_high + box_low) / 2

    if box_midpoint <= 0:
        return None

    box_width_percent = (
        (box_high - box_low)
        / box_midpoint
        * 100
    )

    if comparable_volume_bars:
        average_volume = (
            sum(
                float(bar.volume)
                for bar in comparable_volume_bars
            )
            / len(comparable_volume_bars)
        )
    else:
        average_volume = 0.0

    if average_volume <= 0:
        volume_ratio = 0.0
    else:
        volume_ratio = (
            float(latest.volume)
            / average_volume
        )

    ema_9 = calculate_ema(closes, 9)
    ema_20 = calculate_ema(closes, 20)
    last_price = float(latest.close)

    bullish_trend = (
        ema_9 > ema_20
        and last_price > ema_9
    )
    bearish_trend = (
        ema_9 < ema_20
        and last_price < ema_9
    )

    if bullish_trend:
        direction = "CALL"
        trend_confirmed = True
        breakout_confirmed = last_price > box_high
    elif bearish_trend:
        direction = "PUT"
        trend_confirmed = True
        breakout_confirmed = last_price < box_low
    else:
        direction = "NEUTRAL"
        trend_confirmed = False
        breakout_confirmed = False

    return ScannerResult(
        ticker=ticker,
        bar_timestamp=latest.timestamp,
        direction=direction,
        last_price=last_price,
        ema_9=ema_9,
        ema_20=ema_20,
        box_high=box_high,
        box_low=box_low,
        box_width_percent=box_width_percent,
        volume_ratio=volume_ratio,
        trend_confirmed=trend_confirmed,
        potter_box_found=(
            box_width_percent
            <= MAX_BOX_WIDTH_PERCENT
        ),
        volume_confirmed=(
            volume_ratio
            >= VOLUME_SURGE_MULTIPLIER
        ),
        breakout_confirmed=breakout_confirmed,
    )


def is_regular_market_bar(bar: Any) -> bool:
    market_time = bar.timestamp.astimezone(
        MARKET_TIMEZONE
    )
    minutes_after_midnight = (
        market_time.hour * 60
        + market_time.minute
    )

    return (
        market_time.weekday() < 5
        and minutes_after_midnight >= 9 * 60 + 45
        and minutes_after_midnight < 16 * 60
    )


def fetch_scanner_results() -> tuple[
    list[ScannerResult],
    list[str],
]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    from alpaca_service import get_alpaca_credentials

    api_key, secret_key = get_alpaca_credentials()
    client = StockHistoricalDataClient(
        api_key,
        secret_key,
    )

    now = datetime.now(timezone.utc)
    end_time = now.replace(
        minute=(now.minute // BAR_MINUTES) * BAR_MINUTES,
        second=0,
        microsecond=0,
    )
    start_time = end_time - timedelta(
        days=LOOKBACK_DAYS
    )

    request = StockBarsRequest(
        symbol_or_symbols=WATCHLIST,
        timeframe=TimeFrame(
            BAR_MINUTES,
            TimeFrameUnit.Minute,
        ),
        start=start_time,
        end=end_time,
        feed=DataFeed.IEX,
        limit=10_000,
    )

    bar_set = client.get_stock_bars(request)
    results = []
    skipped = []

    for ticker in WATCHLIST:
        bars = [
            bar
            for bar in bar_set.data.get(ticker, [])
            if is_regular_market_bar(bar)
        ]
        result = analyze_bars(ticker, bars)

        if result is None:
            skipped.append(ticker)
        else:
            results.append(result)

    results.sort(
        key=lambda result: (
            result.technical_candidate(),
            result.score(),
            result.breakout_confirmed,
            result.volume_ratio,
        ),
        reverse=True,
    )

    return results, skipped


def yes_no(value: bool) -> str:
    return "YES" if value else "no"


def show_cts_scanner() -> None:
    print("\nCTS READ-ONLY MARKET SCANNER")
    print(
        f"Watchlist: {len(WATCHLIST)} symbols | "
        f"{BAR_MINUTES}-minute bars"
    )
    print("No order-placement code is used.")

    try:
        results, skipped = fetch_scanner_results()
    except Exception as error:
        print("\nUnable to run the scanner.")
        print(f"Reason: {error}")
        return

    if not results:
        print("\nNo usable market data was returned.")
        return

    latest_timestamp = max(
        result.bar_timestamp
        for result in results
    )
    latest_market_time = latest_timestamp.astimezone(
        MARKET_TIMEZONE
    )
    data_age = (
        datetime.now(timezone.utc)
        - latest_timestamp
    )

    print(
        "\nLatest completed bar: "
        + latest_market_time.strftime(
            "%a %b %d, %Y %I:%M %p ET"
        )
    )

    if data_age > timedelta(minutes=30):
        print(
            "Market is closed or data is delayed. "
            "These are not live signals."
        )

    print("\nTOP TECHNICAL CANDIDATES")

    for result in results[:10]:
        print(
            f"\n{result.ticker} | "
            f"${result.last_price:,.2f} | "
            f"{result.direction} bias | "
            f"Technical score: {result.score()}/4"
        )

        if result.technical_candidate():
            print("CTS technical candidate: YES")
        else:
            print("CTS technical candidate: no")

        print(
            f"Price and EMA trend agree: "
            f"{yes_no(result.trend_confirmed)}"
        )
        print(
            f"Potter Box <= "
            f"{MAX_BOX_WIDTH_PERCENT:.1f}%: "
            f"{yes_no(result.potter_box_found)} "
            f"({result.box_width_percent:.2f}%)"
        )
        print(
            f"Volume >= "
            f"{VOLUME_SURGE_MULTIPLIER:.1f}x same-time "
            f"average: "
            f"{yes_no(result.volume_confirmed)} "
            f"({result.volume_ratio:.2f}x)"
        )
        if result.direction == "CALL":
            print(
                f"CALL breakout above "
                f"${result.box_high:,.2f}: "
                f"{yes_no(result.breakout_confirmed)}"
            )
        elif result.direction == "PUT":
            print(
                f"PUT breakdown below "
                f"${result.box_low:,.2f}: "
                f"{yes_no(result.breakout_confirmed)}"
            )
        else:
            print(
                "Breakout/breakdown confirmed: no"
            )

    if skipped:
        print(
            "\nInsufficient data: "
            + ", ".join(skipped)
        )

    technical_candidates = [
        result
        for result in results
        if result.technical_candidate()
    ]

    if technical_candidates:
        print("\nOPTIONS LIQUIDITY GATE")

        try:
            from options_service import (
                evaluate_option_liquidity,
                show_option_liquidity,
            )

            for candidate in technical_candidates[:3]:
                option_result = evaluate_option_liquidity(
                    ticker=candidate.ticker,
                    direction=candidate.direction,
                    underlying_price=candidate.last_price,
                )

                if option_result is None:
                    print(
                        f"\n{candidate.ticker} "
                        "options: no usable contract data."
                    )
                    continue

                show_option_liquidity(option_result)
        except Exception as error:
            print("\nUnable to run options liquidity gate.")
            print(f"Reason: {error}")

        print("\nNEWS RISK GATE")

        try:
            from news_service import (
                evaluate_news_risk,
                show_news_risk,
            )

            for candidate in technical_candidates[:3]:
                news_result = evaluate_news_risk(
                    candidate.ticker
                )
                show_news_risk(news_result)
        except Exception as error:
            print("\nUnable to run news risk gate.")
            print(f"Reason: {error}")

        print("\nEARNINGS RISK GATE")

        try:
            from earnings_service import (
                evaluate_earnings_risk,
                show_earnings_risk,
            )

            for candidate in technical_candidates[:3]:
                earnings_result = evaluate_earnings_risk(
                    candidate.ticker
                )
                show_earnings_risk(earnings_result)
        except Exception as error:
            print("\nUnable to run earnings risk gate.")
            print(f"Reason: {error}")

    print("\nScanner result: watch candidates only.")
    print("Technical and risk gates completed.")
    print("No order was submitted.")
