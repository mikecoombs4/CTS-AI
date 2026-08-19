from __future__ import annotations

import hashlib
import json
import logging
import signal
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

from catalyst_service import CatalystHeadline, evaluate_catalyst_watch
from scanner_service import fetch_scanner_results
from watchlist_service import resolve_watchlist

EASTERN_TIMEZONE = ZoneInfo("America/New_York")
FRESH_BAR_MAX_AGE = timedelta(minutes=30)
TECHNICAL_RECHECK_INTERVAL = timedelta(minutes=15)
MORNING_ENTRY_START = time(9, 45)
MORNING_ENTRY_END = time(11, 30)
AFTERNOON_ENTRY_START = time(13, 0)
AFTERNOON_ENTRY_END = time(15, 30)


@dataclass(frozen=True)
class CatalystMonitorConfig:
    poll_seconds: float = 300.0
    monitoring_start: time = time(4, 0)
    monitoring_end: time = time(20, 0)
    initial_alert_lookback_minutes: int = 90
    state_retention_days: int = 7
    max_fingerprints: int = 2000
    state_file: Path = Path(__file__).with_name(
        "cts_catalyst_monitor_state.json"
    )
    log_file: Path = Path(__file__).with_name(
        "cts_catalyst_monitor.log"
    )


@dataclass
class CatalystMonitorState:
    version: int = 2
    baseline_initialized: bool = False
    last_poll_at: str | None = None
    seen_articles: dict[str, dict[str, Any]] | None = None
    pending_technical: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.seen_articles is None:
            self.seen_articles = {}
        if self.pending_technical is None:
            self.pending_technical = {}


def eastern_time(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Catalyst monitor time must include a timezone.")
    return current.astimezone(EASTERN_TIMEZONE)


def is_monitoring_time(
    now: datetime | None = None,
    config: CatalystMonitorConfig | None = None,
) -> bool:
    config = config or CatalystMonitorConfig()
    market_now = eastern_time(now)
    return (
        market_now.weekday() < 5
        and config.monitoring_start <= market_now.time() < config.monitoring_end
    )


def _entry_window(now: datetime) -> bool:
    market_now = eastern_time(now)
    if market_now.weekday() >= 5:
        return False
    current_time = market_now.time().replace(tzinfo=None)
    return (
        MORNING_ENTRY_START <= current_time <= MORNING_ENTRY_END
        or AFTERNOON_ENTRY_START <= current_time <= AFTERNOON_ENTRY_END
    )


def _next_weekday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _next_entry_start(now: datetime) -> datetime:
    market_now = eastern_time(now)
    current_time = market_now.time().replace(tzinfo=None)
    day = market_now.date()

    if market_now.weekday() < 5:
        if current_time < MORNING_ENTRY_START:
            return datetime.combine(
                day, MORNING_ENTRY_START, EASTERN_TIMEZONE
            ).astimezone(timezone.utc)
        if current_time < AFTERNOON_ENTRY_START:
            return datetime.combine(
                day, AFTERNOON_ENTRY_START, EASTERN_TIMEZONE
            ).astimezone(timezone.utc)

    next_day = _next_weekday(day + timedelta(days=1))
    return datetime.combine(
        next_day, MORNING_ENTRY_START, EASTERN_TIMEZONE
    ).astimezone(timezone.utc)


def _first_session_expiry(now: datetime) -> datetime:
    market_now = eastern_time(now)
    current_time = market_now.time().replace(tzinfo=None)
    day = market_now.date()
    if market_now.weekday() < 5 and current_time < AFTERNOON_ENTRY_END:
        expiry_day = day
    else:
        expiry_day = _next_weekday(day + timedelta(days=1))
    return datetime.combine(
        expiry_day, AFTERNOON_ENTRY_END, EASTERN_TIMEZONE
    ).astimezone(timezone.utc)


def _next_recheck(now: datetime) -> datetime:
    candidate = now + TECHNICAL_RECHECK_INTERVAL
    candidate_et = eastern_time(candidate)
    candidate_time = candidate_et.time().replace(tzinfo=None)
    if candidate_time > MORNING_ENTRY_END and candidate_time < AFTERNOON_ENTRY_START:
        return datetime.combine(
            candidate_et.date(), AFTERNOON_ENTRY_START, EASTERN_TIMEZONE
        ).astimezone(timezone.utc)
    if candidate_time > AFTERNOON_ENTRY_END:
        return _next_entry_start(candidate)
    return candidate


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def article_fingerprint(headline: CatalystHeadline) -> str:
    provider_id = str(headline.article_id or "").strip()
    if provider_id:
        return f"id:{provider_id}"

    raw = "|".join(
        (
            _normalize_text(headline.source),
            headline.created_at.astimezone(timezone.utc).isoformat(),
            _normalize_text(headline.headline),
        )
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_state() -> CatalystMonitorState:
    return CatalystMonitorState()


def _load_state(
    path: Path,
    logger: logging.Logger,
) -> tuple[CatalystMonitorState, bool]:
    if not path.exists():
        return _new_state(), False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        seen_articles = data.get("seen_articles", {})
        if not isinstance(seen_articles, dict):
            raise ValueError("seen_articles is not an object")
        state = CatalystMonitorState(
            version=2,
            baseline_initialized=bool(
                data.get("baseline_initialized", False)
            ),
            last_poll_at=data.get("last_poll_at"),
            seen_articles=seen_articles,
            pending_technical=(
                data.get("pending_technical", {})
                if int(data.get("version", 1)) >= 2
                else {}
            ),
        )
        return state, False
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        logger.warning("Catalyst monitor state is unreadable: %s", error)
        return _new_state(), True


def _save_state(
    path: Path,
    state: CatalystMonitorState,
    logger: logging.Logger,
) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": state.version,
                    "baseline_initialized": state.baseline_initialized,
                    "last_poll_at": state.last_poll_at,
                    "seen_articles": state.seen_articles,
                    "pending_technical": state.pending_technical,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except (OSError, TypeError, ValueError) as error:
        logger.error("Catalyst monitor state save failed: %s", error)
        return False


def configure_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("cts.catalyst_monitor")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger


class CatalystMonitor:
    def __init__(
        self,
        config: CatalystMonitorConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or CatalystMonitorConfig()
        self.logger = logger or configure_logging(self.config.log_file)
        self.state, self.state_was_malformed = _load_state(
            self.config.state_file,
            self.logger,
        )
        self.silent_baseline = self.state_was_malformed

    def _prune_state(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.config.state_retention_days)
        seen = self.state.seen_articles or {}
        retained = {}
        for fingerprint, record in seen.items():
            try:
                first_seen = datetime.fromisoformat(
                    str(record["first_seen_at"])
                )
                if first_seen.tzinfo is None or first_seen >= cutoff:
                    retained[fingerprint] = record
            except (KeyError, TypeError, ValueError):
                continue

        if len(retained) > self.config.max_fingerprints:
            ordered = sorted(
                retained.items(),
                key=lambda item: item[1]["first_seen_at"],
                reverse=True,
            )[: self.config.max_fingerprints]
            retained = dict(ordered)
        self.state.seen_articles = retained

    def _record_article(
        self,
        fingerprint: str,
        headline: CatalystHeadline,
        now: datetime,
    ) -> None:
        seen = self.state.seen_articles
        if seen is None:
            seen = {}
            self.state.seen_articles = seen
        record = seen.get(fingerprint)
        tickers = set(record.get("tickers", []) if record else [])
        tickers.add(headline.ticker)
        seen[fingerprint] = {
            "article_id": headline.article_id,
            "tickers": sorted(tickers),
            "headline": headline.headline,
            "source": headline.source,
            "created_at": headline.created_at.astimezone(
                timezone.utc
            ).isoformat(),
            "first_seen_at": (
                record.get("first_seen_at")
                if record
                else now.astimezone(timezone.utc).isoformat()
            ),
        }

    def _upsert_pending(
        self,
        ticker: str,
        fingerprint: str,
        headline: CatalystHeadline,
        now: datetime,
    ) -> None:
        pending = self.state.pending_technical
        if pending is None:
            pending = {}
            self.state.pending_technical = pending
        record = pending.get(ticker)
        if record is None:
            record = {
                "ticker": ticker,
                "catalyst_fingerprints": [],
                "catalyst_first_seen_at": now.isoformat(),
                "technical_scan_status": (
                    "OUTSIDE CTS ENTRY WINDOW — PENDING RECHECK"
                ),
                "technical_scan_at": None,
                "last_scanned_bar_timestamp": None,
                "next_check_at": now.isoformat(),
                "expires_at": _first_session_expiry(now).isoformat(),
            }
            pending[ticker] = record

        fingerprints = set(record.get("catalyst_fingerprints", []))
        fingerprints.add(fingerprint)
        record["catalyst_fingerprints"] = sorted(fingerprints)
        record["latest_material_headline"] = {
            "headline": headline.headline,
            "source": headline.source,
            "created_at": headline.created_at.astimezone(
                timezone.utc
            ).isoformat(),
            "event_type": headline.event_type,
            "classification": headline.classification,
        }
        record["next_check_at"] = min(
            record.get("next_check_at", now.isoformat()),
            now.isoformat(),
        )

    @staticmethod
    def _pending_due(record: dict[str, Any], now: datetime) -> bool:
        try:
            next_check = datetime.fromisoformat(
                str(record.get("next_check_at"))
            )
            return next_check <= now
        except (TypeError, ValueError):
            return True

    def _remove_expired_pending(self, now: datetime) -> None:
        pending = self.state.pending_technical or {}
        for ticker, record in list(pending.items()):
            try:
                expires_at = datetime.fromisoformat(
                    str(record["expires_at"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if now > expires_at:
                self.logger.info(
                    "Pending technical recheck expired: ticker=%s",
                    ticker,
                )
                del pending[ticker]

    def _log_technical_context(
        self,
        ticker: str,
        status: str,
        result: Any = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        details = [f"TECHNICAL CONTEXT | ticker={ticker}", f"status={status}"]
        if result is not None:
            bar_timestamp = getattr(result, "bar_timestamp", None)
            if bar_timestamp is not None and bar_timestamp.tzinfo is not None:
                current = now or datetime.now(timezone.utc)
                age = current.astimezone(timezone.utc) - bar_timestamp.astimezone(
                    timezone.utc
                )
                details.extend(
                    [
                        f"direction={result.direction}",
                        f"bar={bar_timestamp.astimezone(EASTERN_TIMEZONE).isoformat()}",
                        f"age_minutes={age.total_seconds() / 60:.1f}",
                    ]
                )
            if status == "NOT TECHNICALLY READY — PENDING RECHECK":
                details.append(f"score={result.score()}/4")
        if reason:
            details.append(f"reason={reason}")
        self.logger.info(" | ".join(details))
        self.logger.info(
            "TECHNICAL CONTEXT IS INFORMATIONAL ONLY — "
            "NO TRADE APPROVAL OR ORDER WAS CREATED."
        )

    def _scan_pending(
        self,
        tickers: list[str],
        now: datetime,
    ) -> None:
        if not tickers:
            return

        try:
            results, skipped = fetch_scanner_results(tickers=tickers)
            scan_failed = None
        except Exception as error:
            results, skipped = [], list(tickers)
            scan_failed = str(error)
            self.logger.error(
                "Technical scanner failed; pending rechecks remain: %s",
                error,
            )

        result_by_ticker = {result.ticker: result for result in results}
        in_entry_window = _entry_window(now)
        pending = self.state.pending_technical or {}
        for ticker in tickers:
            record = pending.get(ticker)
            if record is None:
                continue

            if not in_entry_window:
                status = "OUTSIDE CTS ENTRY WINDOW — PENDING RECHECK"
                record["technical_scan_status"] = status
                record["technical_scan_at"] = now.isoformat()
                record["next_check_at"] = _next_entry_start(now).isoformat()
                self._log_technical_context(ticker, status, now=now)
                continue

            result = result_by_ticker.get(ticker)
            if scan_failed:
                status = "TECHNICAL DATA UNAVAILABLE/STALE — PENDING RECHECK"
                record["technical_scan_status"] = status
                record["technical_scan_at"] = now.isoformat()
                record["next_check_at"] = _next_recheck(now).isoformat()
                self._log_technical_context(
                    ticker, status, reason=scan_failed, now=now
                )
                continue

            if result is None or ticker in skipped:
                status = "TECHNICAL DATA UNAVAILABLE/STALE — PENDING RECHECK"
                record["technical_scan_status"] = status
                record["technical_scan_at"] = now.isoformat()
                record["next_check_at"] = _next_recheck(now).isoformat()
                self._log_technical_context(
                    ticker,
                    status,
                    reason="missing or skipped scanner data",
                    now=now,
                )
                continue

            bar_timestamp = getattr(result, "bar_timestamp", None)
            if bar_timestamp is None or bar_timestamp.tzinfo is None:
                age = None
            else:
                age = now - bar_timestamp.astimezone(timezone.utc)
            previous_bar_timestamp = record.get(
                "last_scanned_bar_timestamp"
            )
            same_bar = (
                age is not None
                and previous_bar_timestamp
                == bar_timestamp.astimezone(timezone.utc).isoformat()
            )
            record["technical_scan_at"] = now.isoformat()
            record["last_scanned_bar_timestamp"] = (
                bar_timestamp.astimezone(timezone.utc).isoformat()
                if bar_timestamp is not None and bar_timestamp.tzinfo is not None
                else None
            )
            record["next_check_at"] = _next_recheck(now).isoformat()

            if same_bar:
                self.logger.info(
                    "Technical recheck skipped: ticker=%s completed bar unchanged",
                    ticker,
                )
                continue

            if age is None or age < timedelta(0) or age > FRESH_BAR_MAX_AGE:
                status = "TECHNICAL DATA UNAVAILABLE/STALE — PENDING RECHECK"
            elif result.technical_candidate():
                status = "TECHNICAL CANDIDATE"
            else:
                status = "NOT TECHNICALLY READY — PENDING RECHECK"

            record["technical_scan_status"] = status
            self._log_technical_context(ticker, status, result, now=now)
            if status == "TECHNICAL CANDIDATE":
                del pending[ticker]

    def _alertable(
        self,
        headline: CatalystHeadline,
        now: datetime,
    ) -> bool:
        if self.silent_baseline:
            return False
        if not self.state.baseline_initialized:
            age = now - headline.created_at
            return (
                headline.freshness == "BREAKING"
                and age <= timedelta(
                    minutes=self.config.initial_alert_lookback_minutes
                )
            )
        return headline.freshness in {"BREAKING", "RECENT"}

    def poll(self, now: datetime | None = None) -> list[CatalystHeadline]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("Catalyst monitor time must include a timezone.")
        current = current.astimezone(timezone.utc)
        if not is_monitoring_time(current, self.config):
            return []

        self._remove_expired_pending(current)
        tickers = resolve_watchlist()
        self.logger.info("Catalyst poll started: watchlist_size=%d", len(tickers))
        try:
            results = evaluate_catalyst_watch(tickers=tickers, now=current)
        except Exception as error:
            self.logger.error("Catalyst provider poll failed: %s", error)
            self.state.last_poll_at = current.isoformat()
            _save_state(self.config.state_file, self.state, self.logger)
            return []

        alerts: list[CatalystHeadline] = []
        new_tickers: set[str] = set()
        duplicate_count = 0
        unavailable_count = 0
        current_material: dict[str, list[CatalystHeadline]] = {}
        for result in results:
            if result.status == "UNAVAILABLE":
                unavailable_count += 1
            for headline in result.material_headlines:
                fingerprint = article_fingerprint(headline)
                current_material.setdefault(fingerprint, []).append(headline)

        if self.state_was_malformed:
            self.state.baseline_initialized = False

        for fingerprint, headlines in current_material.items():
            headline = headlines[0]
            associated_tickers = sorted(
                {item.ticker for item in headlines}
            )
            already_seen = fingerprint in (self.state.seen_articles or {})
            if already_seen:
                duplicate_count += 1
            elif self._alertable(headline, current):
                alerts.append(headline)
                new_tickers.update(associated_tickers)
                self.logger.info(
                    "NEW MATERIAL CATALYST | %s | %s | %s | %s",
                    ",".join(associated_tickers),
                    headline.created_at.astimezone(EASTERN_TIMEZONE).isoformat(),
                    headline.event_type,
                    headline.headline,
                )
            for grouped_headline in headlines:
                if not already_seen and self._alertable(
                    headline, current
                ):
                    self._upsert_pending(
                        grouped_headline.ticker,
                        fingerprint,
                        grouped_headline,
                        current,
                    )
                self._record_article(
                    fingerprint,
                    grouped_headline,
                    current,
                )

        self.state.baseline_initialized = True
        self.state.last_poll_at = current.isoformat()
        self._prune_state(current)
        _save_state(self.config.state_file, self.state, self.logger)
        self.state_was_malformed = False
        self.silent_baseline = False
        pending_due = {
            ticker
            for ticker, record in (self.state.pending_technical or {}).items()
            if ticker not in new_tickers
            and self._pending_due(record, current)
            and _entry_window(current)
        }
        scan_tickers = sorted(new_tickers | pending_due)
        self._scan_pending(scan_tickers, current)
        _save_state(self.config.state_file, self.state, self.logger)
        self.logger.info(
            "Catalyst poll complete: alerts=%d duplicates_suppressed=%d "
            "unavailable=%d",
            len(alerts),
            duplicate_count,
            unavailable_count,
        )
        return alerts

    def run(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self.logger.info(
            "CTS read-only catalyst monitor started: poll_seconds=%.1f "
            "hours=%s-%s ET weekdays_only=true",
            self.config.poll_seconds,
            self.config.monitoring_start.strftime("%H:%M"),
            self.config.monitoring_end.strftime("%H:%M"),
        )
        try:
            while not stop.is_set():
                try:
                    self.poll()
                except Exception:
                    self.logger.exception(
                        "Catalyst monitor cycle failed; continuing"
                    )
                stop.wait(self.config.poll_seconds)
        finally:
            now = datetime.now(timezone.utc)
            self.state.last_poll_at = now.isoformat()
            self._prune_state(now)
            _save_state(self.config.state_file, self.state, self.logger)
            self.logger.info("CTS read-only catalyst monitor stopped")


def run_catalyst_monitor() -> None:
    stop = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    CatalystMonitor().run(stop)


if __name__ == "__main__":
    run_catalyst_monitor()
