import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from threading import Event
from unittest.mock import patch
from zoneinfo import ZoneInfo

from catalyst_monitor import (
    CatalystMonitor,
    CatalystMonitorConfig,
    article_fingerprint,
    is_monitoring_time,
)
from catalyst_service import CatalystHeadline, CatalystWatchResult

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)


def headline(
    ticker="AAPL",
    minutes_old=5,
    freshness="BREAKING",
    article_id=None,
    source="Wire",
    text="AAPL wins contract award",
):
    return CatalystHeadline(
        ticker=ticker,
        created_at=NOW - timedelta(minutes=minutes_old),
        age=timedelta(minutes=minutes_old),
        freshness=freshness,
        event_type="contract/deal",
        classification="FAVORABLE",
        source=source,
        headline=text,
        provider_symbols=[ticker],
        relevance="DIRECT",
        is_material=True,
        article_id=article_id,
    )


def result(ticker, items):
    return CatalystWatchResult(
        ticker=ticker,
        status="MATERIAL BREAKING" if items else "NO MATERIAL CATALYST",
        headlines=items,
    )


class CatalystMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = CatalystMonitorConfig(
            poll_seconds=0.01,
            state_file=root / "state.json",
            log_file=root / "monitor.log",
        )
        self.logger = logging.getLogger(f"test.catalyst.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def tearDown(self):
        self.temp_dir.cleanup()

    def monitor(self):
        return CatalystMonitor(self.config, self.logger)

    def test_monitoring_boundaries_and_sessions(self):
        self.assertTrue(is_monitoring_time(datetime(2026, 8, 19, 4, 0, tzinfo=ET), self.config))
        self.assertTrue(is_monitoring_time(datetime(2026, 8, 19, 12, 0, tzinfo=ET), self.config))
        self.assertTrue(is_monitoring_time(datetime(2026, 8, 19, 19, 59, tzinfo=ET), self.config))
        self.assertFalse(is_monitoring_time(datetime(2026, 8, 19, 3, 59, tzinfo=ET), self.config))
        self.assertFalse(is_monitoring_time(datetime(2026, 8, 19, 20, 0, tzinfo=ET), self.config))
        self.assertFalse(is_monitoring_time(datetime(2026, 8, 22, 12, 0, tzinfo=ET), self.config))

    def test_configurable_poll_interval_and_stop_event(self):
        stop = Event()
        monitor = self.monitor()
        calls = []

        def poll():
            calls.append(True)
            stop.set()

        with patch.object(monitor, "poll", side_effect=poll), \
             patch.object(stop, "wait", return_value=None) as wait:
            monitor.run(stop)

        self.assertEqual(len(calls), 1)
        wait.assert_called_once_with(0.01)

    def test_first_run_alerts_only_recent_breaking_and_baselines_backlog(self):
        recent = headline(minutes_old=10)
        older = headline(minutes_old=120, freshness="RECENT", text="AAPL older contract award")
        stale = headline(minutes_old=24 * 60, freshness="STALE", text="AAPL stale contract award")
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [recent, older, stale])]):
            alerts = self.monitor().poll(NOW)

        self.assertEqual([item.headline for item in alerts], [recent.headline])
        state = json.loads(self.config.state_file.read_text())
        self.assertTrue(state["baseline_initialized"])
        self.assertEqual(len(state["seen_articles"]), 3)

    def test_malformed_state_creates_silent_baseline(self):
        self.config.state_file.write_text("not json")
        recent = headline(minutes_old=5)
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [recent])]):
            alerts = self.monitor().poll(NOW)

        self.assertEqual(alerts, [])
        self.assertTrue(json.loads(self.config.state_file.read_text())["baseline_initialized"])

    def test_new_breaking_and_recent_alerts_are_reported(self):
        monitor = self.monitor()
        monitor.state.baseline_initialized = True
        breaking = headline(minutes_old=5)
        recent = headline(minutes_old=100, freshness="RECENT", text="AAPL recent contract award")
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [breaking, recent])]):
            alerts = monitor.poll(NOW)

        self.assertEqual(len(alerts), 2)

    def test_stale_broad_informational_unavailable_and_nonmaterial_are_ignored(self):
        monitor = self.monitor()
        monitor.state.baseline_initialized = True
        ignored = headline(minutes_old=5)
        ignored.is_material = False
        unavailable = CatalystWatchResult("MSFT", "UNAVAILABLE", [], "provider down")
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL", "MSFT"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [ignored]), unavailable]):
            alerts = monitor.poll(NOW)

        self.assertEqual(alerts, [])

    def test_duplicate_suppression_within_cycle_and_after_restart(self):
        first = headline(article_id="provider-1")
        second = headline(ticker="MSFT", article_id="provider-1")
        responses = [result("AAPL", [first]), result("MSFT", [second])]
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL", "MSFT"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=responses):
            monitor = self.monitor()
            monitor.state.baseline_initialized = True
            alerts = monitor.poll(NOW)

        self.assertEqual(len(alerts), 1)
        restarted = self.monitor()
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [first])]):
            self.assertEqual(restarted.poll(NOW + timedelta(minutes=5)), [])

        record = next(iter(restarted.state.seen_articles.values()))
        self.assertEqual(record["tickers"], ["AAPL", "MSFT"])

    def test_provider_id_is_preferred_and_fallback_excludes_ticker(self):
        first = headline(ticker="AAPL", article_id="same-id")
        second = headline(ticker="MSFT", article_id="same-id")
        self.assertEqual(article_fingerprint(first), article_fingerprint(second))
        first.article_id = None
        second.article_id = None
        self.assertEqual(article_fingerprint(first), article_fingerprint(second))

    def test_prunes_by_age_and_maximum(self):
        monitor = self.monitor()
        monitor.state.seen_articles = {
            str(index): {
                "first_seen_at": (
                    NOW - timedelta(days=8 if index == 0 else 1)
                ).isoformat(),
                "tickers": ["AAPL"],
            }
            for index in range(3)
        }
        monitor.config = CatalystMonitorConfig(
            state_file=self.config.state_file,
            log_file=self.config.log_file,
            max_fingerprints=2,
        )
        monitor._prune_state(NOW)
        self.assertEqual(len(monitor.state.seen_articles), 2)
        self.assertNotIn("0", monitor.state.seen_articles)

    def test_atomic_state_file_is_written_without_temp_file(self):
        monitor = self.monitor()
        monitor.state.baseline_initialized = True
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", return_value=[result("AAPL", [])]):
            monitor.poll(NOW)

        self.assertTrue(self.config.state_file.exists())
        self.assertFalse(Path(str(self.config.state_file) + ".tmp").exists())

    def test_provider_failure_does_not_stop_later_cycles(self):
        monitor = self.monitor()
        monitor.state.baseline_initialized = True
        fresh = headline()
        with patch("catalyst_monitor.resolve_watchlist", return_value=["AAPL"]), \
             patch("catalyst_monitor.evaluate_catalyst_watch", side_effect=[RuntimeError("temporary"), [result("AAPL", [fresh])]]):
            self.assertEqual(monitor.poll(NOW), [])
            self.assertEqual(len(monitor.poll(NOW + timedelta(minutes=5))), 1)

    def test_no_protected_modules_are_imported_by_monitor(self):
        source = Path("catalyst_monitor.py").read_text()
        for module in (
            "decision_service",
            "paper_entry_service",
            "paper_execution_service",
            "risk_service",
            "options_service",
            "daily_limits_service",
        ):
            self.assertNotIn(f"import {module}", source)


if __name__ == "__main__":
    unittest.main()
