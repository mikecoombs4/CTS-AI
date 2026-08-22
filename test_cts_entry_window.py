import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cts_entry_window import cts_entry_window_open, forced_0dte_exit_due


ET = ZoneInfo("America/New_York")


class CtsEntryWindowTests(unittest.TestCase):
    def test_exact_entry_and_exit_boundaries(self):
        cases = (
            ((9, 45), True), ((11, 29), True), ((11, 30), False),
            ((12, 59), False), ((13, 0), True), ((15, 29), True), ((15, 30), False),
        )
        for (hour, minute), expected in cases:
            with self.subTest(hour=hour, minute=minute):
                value = datetime(2026, 8, 24, hour, minute, tzinfo=ET)
                self.assertEqual(cts_entry_window_open(value), expected)
        self.assertFalse(forced_0dte_exit_due(datetime(2026, 8, 24, 15, 54, tzinfo=ET)))
        self.assertTrue(forced_0dte_exit_due(datetime(2026, 8, 24, 15, 55, tzinfo=ET)))

    def test_utc_equivalence_and_dst_awareness(self):
        for local in (
            datetime(2026, 8, 24, 9, 45, tzinfo=ET),
            datetime(2026, 11, 2, 13, 0, tzinfo=ET),
        ):
            self.assertEqual(
                cts_entry_window_open(local),
                cts_entry_window_open(local.astimezone(timezone.utc)),
            )

    def test_naive_time_fails_closed(self):
        with self.assertRaises(ValueError):
            cts_entry_window_open(datetime(2026, 8, 24, 9, 45))


if __name__ == "__main__":
    unittest.main()
