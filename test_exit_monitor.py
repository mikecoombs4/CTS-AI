import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from exit_monitor import (
    MonitorConfig,
    PaperExitMonitor,
    new_paper_entries_allowed,
    option_expiration,
)


EASTERN = ZoneInfo("America/New_York")


def option_position(
    symbol="SPY260801C00640000",
    current=1.00,
    entry=1.00,
    return_percent=0.0,
):
    return SimpleNamespace(
        symbol=symbol,
        asset_class="us_option",
        side="long",
        current_price=str(current),
        avg_entry_price=str(entry),
        unrealized_plpc=str(return_percent / 100.0),
    )


class FakeClient:
    def __init__(self, positions=None, orders=None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.close_calls = []
        self.cancel_all_calls = 0
        self.cancel_calls = []

    def get_all_positions(self):
        return list(self.positions)

    def get_orders(self):
        return list(self.orders)

    def cancel_orders(self):
        self.cancel_all_calls += 1
        self.orders = []
        return []

    def cancel_order_by_id(self, order_id):
        self.cancel_calls.append(str(order_id))
        self.orders = [
            order
            for order in self.orders
            if str(order.id) != str(order_id)
        ]

    def close_position(self, symbol):
        self.close_calls.append(symbol)
        order = SimpleNamespace(
            id=f"close-{len(self.close_calls)}",
            symbol=symbol,
            status="new",
        )
        self.orders.append(order)
        return order


class ExitMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config = MonitorConfig(
            poll_seconds=0.01,
            state_file=root / "state.json",
            log_file=root / "monitor.log",
        )
        self.logger = logging.getLogger(f"test.{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def tearDown(self):
        self.temp_dir.cleanup()

    def monitor(self, client):
        return PaperExitMonitor(
            client=client,
            config=self.config,
            logger=self.logger,
        )

    def test_option_expiration_parses_occ_symbol(self):
        self.assertEqual(
            option_expiration("SPY260801C00640000").isoformat(),
            "2026-08-01",
        )
        self.assertIsNone(option_expiration("SPY"))

    def test_entry_gate_closes_at_330_eastern(self):
        too_early = datetime(2026, 7, 31, 9, 44, tzinfo=EASTERN)
        before = datetime(2026, 7, 31, 15, 29, tzinfo=EASTERN)
        cutoff = datetime(2026, 7, 31, 15, 30, tzinfo=EASTERN)
        self.assertFalse(new_paper_entries_allowed(too_early))
        self.assertTrue(new_paper_entries_allowed(before))
        self.assertFalse(new_paper_entries_allowed(cutoff))

    def test_stop_loss_submits_paper_close(self):
        client = FakeClient(
            [option_position(return_percent=-25.0, current=0.75)]
        )
        self.monitor(client).cycle(
            datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        )
        self.assertEqual(client.close_calls, ["SPY260801C00640000"])

    def test_profit_target_submits_paper_close(self):
        client = FakeClient(
            [option_position(return_percent=35.0, current=1.35)]
        )
        self.monitor(client).cycle(
            datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        )
        self.assertEqual(client.close_calls, ["SPY260801C00640000"])

    def test_trailing_high_water_survives_restart(self):
        client = FakeClient(
            [option_position(return_percent=20.0, current=1.20)]
        )
        self.monitor(client).cycle(
            datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        )
        self.assertEqual(client.close_calls, [])

        client.positions = [
            option_position(return_percent=7.0, current=1.07)
        ]
        restarted = self.monitor(client)
        restarted.cycle(
            datetime(2026, 7, 31, 12, 1, tzinfo=EASTERN)
        )
        self.assertEqual(client.close_calls, ["SPY260801C00640000"])

    def test_existing_symbol_order_is_canceled_before_normal_exit(self):
        symbol = "SPY260801C00640000"
        client = FakeClient(
            [option_position(return_percent=-30.0, current=0.70)],
            [SimpleNamespace(id="working-1", symbol=symbol, status="new")],
        )
        monitor = self.monitor(client)
        midday = datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        monitor.cycle(midday)
        self.assertEqual(client.cancel_calls, ["working-1"])
        self.assertEqual(client.close_calls, [])

        monitor.cycle(midday)
        self.assertEqual(client.close_calls, [symbol])

    def test_355_boundary_cancels_then_closes_every_0dte(self):
        first = option_position("SPY260731C00640000")
        second = option_position("QQQ260731P00550000")
        client = FakeClient([first, second])
        monitor = self.monitor(client)
        cutoff = datetime(2026, 7, 31, 15, 55, tzinfo=EASTERN)

        monitor.cycle(cutoff)
        self.assertEqual(client.cancel_all_calls, 1)
        self.assertEqual(client.close_calls, [])

        monitor.cycle(cutoff)
        self.assertCountEqual(
            client.close_calls,
            ["SPY260731C00640000", "QQQ260731P00550000"],
        )

        monitor.cycle(cutoff)
        self.assertEqual(len(client.close_calls), 2)

    def test_forced_exit_retries_if_order_disappears_but_position_remains(self):
        symbol = "SPY260731C00640000"
        client = FakeClient([option_position(symbol)])
        monitor = self.monitor(client)
        cutoff = datetime(2026, 7, 31, 15, 55, tzinfo=EASTERN)
        monitor.cycle(cutoff)
        monitor.cycle(cutoff)
        self.assertEqual(len(client.close_calls), 1)

        client.orders = []
        monitor.cycle(cutoff)
        self.assertEqual(len(client.close_calls), 2)

    def test_355_boundary_cancels_entry_orders_without_a_position(self):
        entry = SimpleNamespace(
            id="entry-1",
            symbol="SPY260731C00640000",
            status="new",
        )
        client = FakeClient(positions=[], orders=[entry])
        monitor = self.monitor(client)
        monitor.cycle(
            datetime(2026, 7, 31, 15, 55, tzinfo=EASTERN)
        )
        self.assertEqual(client.cancel_all_calls, 1)
        self.assertEqual(client.close_calls, [])

    def test_later_expiration_is_not_forced_closed(self):
        later = option_position("SPY260807C00640000")
        client = FakeClient([later])
        monitor = self.monitor(client)
        monitor.cycle(
            datetime(2026, 7, 31, 15, 56, tzinfo=EASTERN)
        )
        self.assertEqual(client.cancel_all_calls, 1)
        monitor.cycle(
            datetime(2026, 7, 31, 15, 56, tzinfo=EASTERN)
        )
        self.assertEqual(client.close_calls, [])

    def test_closed_position_is_removed_from_persistent_state(self):
        symbol = "SPY260801C00640000"
        client = FakeClient(
            [option_position(symbol, return_percent=-25.0, current=0.75)]
        )
        monitor = self.monitor(client)
        midday = datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        monitor.cycle(midday)
        client.positions = []
        client.orders = []
        monitor.cycle(midday)
        saved = json.loads(self.config.state_file.read_text())
        self.assertNotIn(symbol, saved["positions"])

    def test_cycle_returns_structured_success_and_aware_heartbeat(self):
        result = self.monitor(FakeClient()).cycle(
            datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
        )
        self.assertTrue(result.success)
        self.assertEqual(result.monitored_symbols, [])
        self.assertEqual(result.failed_actions, [])
        self.assertEqual(result.blocking_reasons, [])
        self.assertIsNotNone(datetime.fromisoformat(result.heartbeat_at).tzinfo)

    def test_cycle_uses_supplied_position_snapshot_without_retrieval(self):
        client = FakeClient()
        client.get_all_positions = Mock(side_effect=AssertionError("must not retrieve"))
        position = option_position()
        result = self.monitor(client).cycle(
            datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN),
            positions_snapshot=[position],
        )
        self.assertTrue(result.success)
        self.assertEqual(result.monitored_symbols, [position.symbol])
        client.get_all_positions.assert_not_called()

    def test_retrieval_malformed_position_exit_and_state_failures_report_failure(self):
        failing_close = FakeClient([option_position(return_percent=-30.0, current=0.7)])
        failing_close.close_position = Mock(side_effect=OSError("close failed"))
        cases = (
            self.monitor(SimpleNamespace(get_all_positions=Mock(side_effect=TimeoutError()), get_orders=lambda: [])),
            self.monitor(FakeClient([SimpleNamespace(
                symbol="SPY260801C00640000", asset_class="us_option", side="long",
                current_price=None, avg_entry_price="1.0",
            )])),
            self.monitor(failing_close),
        )
        for index, monitor in enumerate(cases):
            with self.subTest(index=index):
                result = monitor.cycle(datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN))
                self.assertFalse(result.success)
                self.assertTrue(result.failed_actions)
                self.assertTrue(result.blocking_reasons)
        with patch("exit_monitor._save_state", side_effect=OSError("state")):
            result = self.monitor(FakeClient()).cycle(
                datetime(2026, 7, 31, 12, 0, tzinfo=EASTERN)
            )
        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
