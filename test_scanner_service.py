import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from scanner_service import analyze_bars


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
                    31,
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


if __name__ == "__main__":
    unittest.main()
