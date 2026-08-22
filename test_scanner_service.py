import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from scanner_service import analyze_bars, is_regular_market_bar


ET = ZoneInfo("America/New_York")


@dataclass
class TestBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def make_bars(mode: str) -> list[TestBar]:
    bars = []
    base = 100.0

    for index in range(24):
        if mode == "bull":
            close = base + index * 0.08
        elif mode == "bear":
            close = base - index * 0.08
        else:
            close = base + (0.10 if index % 2 else -0.10)

        bars.append(
            TestBar(
                timestamp=datetime(
                    2026,
                    7,
                    index + 1,
                    14,
                    0,
                    tzinfo=timezone.utc,
                ),
                open=close,
                high=close + 0.15,
                low=close - 0.15,
                close=close,
                volume=100,
            )
        )

    if mode == "bull":
        close = max(bar.high for bar in bars[-8:]) + 0.25
    elif mode == "bear":
        close = min(bar.low for bar in bars[-8:]) - 0.25
    else:
        close = 100.0

    bars.append(
        TestBar(
            timestamp=datetime(
                2026,
                7,
                31,
                14,
                15,
                tzinfo=timezone.utc,
            ),
            open=close,
            high=close + 0.05,
            low=close - 0.05,
            close=close,
            volume=200,
        )
    )

    return bars


class ScannerDirectionTests(unittest.TestCase):
    def test_bullish_breakout_becomes_call_candidate(self) -> None:
        result = analyze_bars("BULL", make_bars("bull"))

        self.assertIsNotNone(result)
        self.assertEqual(result.direction, "CALL")
        self.assertTrue(result.breakout_confirmed)

    def test_bearish_breakdown_becomes_put_candidate(self) -> None:
        result = analyze_bars("BEAR", make_bars("bear"))

        self.assertIsNotNone(result)
        self.assertEqual(result.direction, "PUT")
        self.assertTrue(result.breakout_confirmed)

    def test_sideways_price_stays_neutral(self) -> None:
        result = analyze_bars("FLAT", make_bars("flat"))

        self.assertIsNotNone(result)
        self.assertEqual(result.direction, "NEUTRAL")
        self.assertFalse(result.breakout_confirmed)

    def test_completed_bar_boundaries_and_end_evidence(self) -> None:
        bars = make_bars("bull")
        template = bars[-1]
        bar_0930 = TestBar(**{
            **template.__dict__, "timestamp": datetime(2026, 8, 24, 9, 30, tzinfo=ET)
        })
        bar_0945 = TestBar(**{
            **template.__dict__, "timestamp": datetime(2026, 8, 24, 9, 45, tzinfo=ET)
        })
        at_0945 = analyze_bars(
            "BULL", bars[:-1] + [bar_0930, bar_0945],
            as_of=datetime(2026, 8, 24, 9, 45, tzinfo=ET),
        )
        self.assertEqual(at_0945.bar_timestamp, bar_0930.timestamp)
        self.assertEqual(at_0945.bar_end_timestamp, bar_0945.timestamp)
        before = analyze_bars(
            "BULL", bars[:-1] + [bar_0930],
            as_of=datetime(2026, 8, 24, 9, 44, 59, tzinfo=ET),
        )
        self.assertNotEqual(before.bar_timestamp, bar_0930.timestamp)
        at_1000 = analyze_bars(
            "BULL", bars[:-1] + [bar_0930, bar_0945],
            as_of=datetime(2026, 8, 24, 10, 0, tzinfo=ET),
        )
        self.assertEqual(at_1000.bar_timestamp, bar_0945.timestamp)

    def test_0930_regular_bar_is_included(self) -> None:
        bar = TestBar(datetime(2026, 8, 24, 9, 30, tzinfo=ET), 1, 1, 1, 1, 1)
        self.assertTrue(is_regular_market_bar(bar))

    def test_naive_future_and_malformed_bars_fail_closed(self) -> None:
        as_of = datetime(2026, 8, 24, 10, 0, tzinfo=ET)
        cases = (
            make_bars("bull") + [TestBar(datetime(2026, 8, 24, 9, 45), 1, 1, 1, 1, 1)],
            make_bars("bull") + [TestBar(as_of + timedelta(minutes=15), 1, 1, 1, 1, 1)],
            make_bars("bull") + [SimpleNamespace(timestamp="bad")],
        )
        for bars in cases:
            with self.subTest(last=bars[-1].timestamp):
                self.assertIsNone(analyze_bars("BULL", bars, as_of=as_of))

    def test_unsorted_completed_bars_are_deterministic_and_duplicates_fail(self) -> None:
        bars = make_bars("bull")
        for index, bar in enumerate(bars):
            bar.timestamp = datetime(2026, 7, 27, 9, 30, tzinfo=ET) + timedelta(minutes=15 * index)
        as_of = bars[-1].timestamp + timedelta(minutes=15)
        ordered = analyze_bars("BULL", bars, as_of=as_of)
        reversed_result = analyze_bars("BULL", list(reversed(bars)), as_of=as_of)
        self.assertEqual(ordered.bar_timestamp, reversed_result.bar_timestamp)
        self.assertIsNone(analyze_bars("BULL", bars + [bars[-1]], as_of=as_of))


if __name__ == "__main__":
    unittest.main()
