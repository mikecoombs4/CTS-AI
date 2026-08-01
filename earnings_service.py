import csv
import io
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")
EARNINGS_API_URL = "https://www.alphavantage.co/query"
CACHE_MAX_AGE = timedelta(hours=12)
BLOCK_DAYS = 1
REVIEW_DAYS = 3
ETF_SYMBOLS = {"SPY", "QQQ", "IWM"}


@dataclass
class EarningsRiskResult:
    ticker: str
    status: str
    report_date: date | None
    days_until_report: int | None
    reason: str


def _cache_file() -> Path:
    home = Path.home()

    if sys.platform == "darwin":
        cache_directory = (
            home / "Library" / "Caches" / "CTS-AI"
        )
    else:
        cache_directory = home / ".cache" / "cts-ai"

    return cache_directory / "earnings_calendar.csv"


def _get_api_key() -> str:
    from dotenv import dotenv_values

    config = dotenv_values(ENV_FILE)
    api_key = (
        config.get("ALPHA_VANTAGE_API_KEY") or ""
    ).strip()

    if not api_key:
        raise RuntimeError(
            "Alpha Vantage API key is missing from .env."
        )

    return api_key


def _cache_is_fresh(cache_file: Path) -> bool:
    if not cache_file.exists():
        return False

    modified_at = datetime.fromtimestamp(
        cache_file.stat().st_mtime,
        tz=timezone.utc,
    )

    return (
        datetime.now(timezone.utc) - modified_at
        <= CACHE_MAX_AGE
    )


def _download_calendar() -> str:
    import requests

    try:
        response = requests.get(
            EARNINGS_API_URL,
            params={
                "function": "EARNINGS_CALENDAR",
                "horizon": "3month",
                "apikey": _get_api_key(),
            },
            timeout=20,
            headers={"User-Agent": "CTS-AI/1.0"},
        )
        response.raise_for_status()
        content = response.text
    except requests.RequestException as error:
        raise RuntimeError(
            "Earnings calendar request failed."
        ) from error

    lowered = content.lower()

    if "symbol" not in lowered or "reportdate" not in lowered:
        raise RuntimeError(
            "Earnings provider returned an unexpected response."
        )

    return content


def _load_calendar_text() -> str:
    cache_file = _cache_file()

    if _cache_is_fresh(cache_file):
        return cache_file.read_text(encoding="utf-8")

    content = _download_calendar()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(content, encoding="utf-8")
    return content


def _find_report_date(
    ticker: str,
    calendar_text: str,
) -> date | None:
    reader = csv.DictReader(io.StringIO(calendar_text))
    ticker = ticker.upper()

    for row in reader:
        if (row.get("symbol") or "").upper() != ticker:
            continue

        report_date = (row.get("reportDate") or "").strip()

        try:
            return date.fromisoformat(report_date)
        except ValueError:
            return None

    return None


def classify_earnings_date(
    ticker: str,
    report_date: date | None,
    today: date | None = None,
) -> EarningsRiskResult:
    ticker = ticker.upper()
    today = today or date.today()

    if ticker in ETF_SYMBOLS:
        return EarningsRiskResult(
            ticker=ticker,
            status="PASS",
            report_date=None,
            days_until_report=None,
            reason="ETF has no company earnings report.",
        )

    if report_date is None:
        return EarningsRiskResult(
            ticker=ticker,
            status="REVIEW",
            report_date=None,
            days_until_report=None,
            reason=(
                "No upcoming earnings date was found; "
                "verification is required."
            ),
        )

    days_until_report = (report_date - today).days

    if days_until_report < 0:
        status = "REVIEW"
        reason = "The provider returned a past report date."
    elif days_until_report <= BLOCK_DAYS:
        status = "BLOCK"
        reason = (
            "Earnings are today or within one calendar day."
        )
    elif days_until_report <= REVIEW_DAYS:
        status = "REVIEW"
        reason = "Earnings are within three calendar days."
    else:
        status = "PASS"
        reason = "No earnings report is due within three days."

    return EarningsRiskResult(
        ticker=ticker,
        status=status,
        report_date=report_date,
        days_until_report=days_until_report,
        reason=reason,
    )


def evaluate_earnings_risk(ticker: str) -> EarningsRiskResult:
    if ticker.upper() in ETF_SYMBOLS:
        return classify_earnings_date(ticker, None)

    calendar_text = _load_calendar_text()
    report_date = _find_report_date(
        ticker=ticker,
        calendar_text=calendar_text,
    )

    return classify_earnings_date(
        ticker=ticker,
        report_date=report_date,
    )


def show_earnings_risk(result: EarningsRiskResult) -> None:
    print(
        f"\n{result.ticker} EARNINGS RISK: "
        f"{result.status}"
    )

    if result.report_date is not None:
        print(
            f"Expected report date: "
            f"{result.report_date.isoformat()}"
        )

    if result.days_until_report is not None:
        print(
            f"Calendar days until report: "
            f"{result.days_until_report}"
        )

    print(result.reason)
    print("Read-only earnings check. No order was submitted.")
