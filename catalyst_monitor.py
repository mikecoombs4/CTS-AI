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
from watchlist_service import resolve_watchlist

EASTERN_TIMEZONE = ZoneInfo("America/New_York")


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
    version: int = 1
    baseline_initialized: bool = False
    last_poll_at: str | None = None
    seen_articles: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.seen_articles is None:
            self.seen_articles = {}


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
            version=int(data.get("version", 1)),
            baseline_initialized=bool(
                data.get("baseline_initialized", False)
            ),
            last_poll_at=data.get("last_poll_at"),
            seen_articles=seen_articles,
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
                self.logger.info(
                    "NEW MATERIAL CATALYST | %s | %s | %s | %s",
                    ",".join(associated_tickers),
                    headline.created_at.astimezone(EASTERN_TIMEZONE).isoformat(),
                    headline.event_type,
                    headline.headline,
                )
            for grouped_headline in headlines:
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
