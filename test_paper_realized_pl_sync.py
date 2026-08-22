import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from daily_limits_service import evaluate_daily_limits
from paper_realized_pl_sync import OrderHistoryPage, synchronize_managed_realized_pl
from paper_state_service import load_state, new_state, save_state


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=ET)
SYMBOL = "AAPL260918C00150000"


def order(order_id, client_id, side, price, qty=1, when=NOW, symbol=SYMBOL, fee="0"):
    return {
        "id": order_id, "client_order_id": client_id, "symbol": symbol,
        "side": side, "status": "filled", "qty": str(qty),
        "filled_qty": str(qty), "filled_avg_price": str(price),
        "filled_at": when.isoformat(), "updated_at": when.isoformat(),
        "order_class": "simple", "legs": None,
        "fee": fee, "multiplier": "100",
    }


class PaperRealizedPLSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.tracker = root / "tracker.json"
        self.journal = root / "journal.json"
        self.state = root / "state.json"

    def tearDown(self):
        self.temp.cleanup()

    def managed(self, qty=1, submitted=NOW - timedelta(days=1)):
        tracker_record = {
            "paper_only": True, "side": "buy", "order_type": "limit",
            "time_in_force": "day", "position_intent": "buy_to_open",
            "order_shape_verified": True,
            "trading_date": submitted.astimezone(ET).date().isoformat(),
            "broker_order_id": "buy-1", "client_order_id": "client-1",
            "option_symbol": SYMBOL, "underlying_ticker": "AAPL",
            "requested_quantity": qty, "filled_quantity": qty,
            "limit_price": 1.0, "average_fill_price": 1.0,
            "normalized_status": "filled", "submitted_at": submitted.isoformat(),
            "updated_at": submitted.isoformat(), "filled_at": submitted.isoformat(),
            "terminal": True, "outcome": "success", "blocking_reason": None,
            "position_exposure_exists": True,
            "requires_exit_monitor_handoff": True,
        }
        self.tracker.write_text(json.dumps({"version": 3, "orders": [tracker_record]}))
        intent = {
            "paper_only": True, "origin": "CORE_CTS",
            "trading_date": tracker_record["trading_date"],
            "client_order_id": "client-1", "ticker": "AAPL",
            "option_symbol": SYMBOL, "quantity": qty, "limit_price": 1.0,
            "status": "TRACKED", "created_at": submitted.isoformat(),
            "updated_at": submitted.isoformat(), "broker_order_id": "buy-1",
            "blocking_reason": None,
        }
        self.journal.write_text(json.dumps({"version": 2, "intents": [intent]}))

    def sync(self, orders=(), now=NOW, provider=None):
        provider = provider or (lambda token: OrderHistoryPage(tuple(orders), None, True, True))
        return synchronize_managed_realized_pl(
            history_provider=provider, tracker_path=self.tracker,
            journal_path=self.journal, state_path=self.state, now=now,
        )

    def test_complete_empty_morning_verifies_trusted_zero(self):
        result = self.sync()
        self.assertTrue(result.success)
        self.assertEqual(result.realized_pnl, Decimal("0"))
        state = load_state(self.state, today=NOW.date())
        self.assertEqual(state.realized_pnl_verification_source, "PAPER_BROKER_MANAGED_FILLS")
        self.assertTrue(state.realized_pnl_evidence_id)

    def test_winning_and_losing_closes_use_decimal_multiplier(self):
        self.managed()
        buy = order("buy-1", "client-1", "buy", "1.00", when=NOW - timedelta(days=1))
        win = self.sync((buy, order("sell-1", "exit-1", "sell", "1.60")))
        self.assertEqual(win.realized_pnl, Decimal("60.00"))
        loss = self.sync((buy, order("sell-2", "exit-2", "sell", "0.40")), now=NOW + timedelta(minutes=1))
        self.assertEqual(loss.realized_pnl, Decimal("-60.00"))
        self.assertFalse(evaluate_daily_limits(0, 0, 0, float(loss.realized_pnl)).new_trade_allowed)

    def test_open_unrealized_is_excluded_and_partial_close_counts_only_closed(self):
        self.managed(qty=2)
        buy = order("buy-1", "client-1", "buy", "1.00", qty=2, when=NOW - timedelta(days=1))
        self.assertEqual(self.sync((buy,)).realized_pnl, Decimal("0"))
        result = self.sync((buy, order("sell-1", "exit-1", "sell", "1.25")), now=NOW + timedelta(minutes=1))
        self.assertEqual(result.realized_pnl, Decimal("25.00"))

    def test_terminal_partial_fill_exposure_is_preserved_and_closed(self):
        self.managed(qty=2)
        tracker = json.loads(self.tracker.read_text())
        tracker["orders"][0].update(
            filled_quantity=1, normalized_status="canceled", outcome="failure",
            filled_at=None,
        )
        self.tracker.write_text(json.dumps(tracker))
        buy = order("buy-1", "client-1", "buy", "1", qty=2, when=NOW - timedelta(days=1))
        buy.update(filled_qty="1", status="canceled", filled_at=None)
        sell = order("sell-1", "exit-1", "sell", "1.20")
        self.assertEqual(self.sync((buy, sell)).realized_pnl, Decimal("20.00"))

    def test_prior_day_entry_and_multiple_managed_lots_close_today(self):
        self.managed(qty=2, submitted=NOW - timedelta(days=2))
        buy = order("buy-1", "client-1", "buy", "1.00", qty=2, when=NOW - timedelta(days=2))
        close = order("sell-1", "exit-1", "sell", "1.25", qty=2)
        self.assertEqual(self.sync((buy, close)).realized_pnl, Decimal("50.00"))

    def test_fees_are_applied_to_buy_and_sell(self):
        self.managed()
        buy = order("buy-1", "client-1", "buy", "1", when=NOW - timedelta(days=1), fee="1.25")
        sell = order("sell-1", "exit-1", "sell", "2", fee=".75")
        self.assertEqual(self.sync((buy, sell)).realized_pnl, Decimal("98.00"))

    def test_unknown_duplicate_unmatched_and_excess_activity_block(self):
        manual = order("manual", "manual-client", "buy", "1")
        self.assertFalse(self.sync((manual,)).success)
        self.managed()
        buy = order("buy-1", "client-1", "buy", "1", when=NOW - timedelta(days=1))
        self.assertFalse(self.sync((buy, buy)).success)
        self.assertFalse(self.sync((order("sell", "exit", "sell", "1"),)).success)
        self.assertFalse(self.sync((buy, order("sell", "exit", "sell", "1", qty=2))).success)

    def test_wrong_symbol_quantity_multiplier_and_identity_block(self):
        self.managed()
        entry_time = NOW - timedelta(days=1)
        cases = [
            order("buy-1", "client-1", "buy", "1", when=entry_time, symbol="MSFT260918C00150000"),
            order("buy-1", "client-1", "buy", "1", qty=2, when=entry_time),
            {**order("buy-1", "client-1", "buy", "1", when=entry_time), "multiplier": "1"},
            order("wrong", "client-1", "buy", "1", when=entry_time),
            order("buy-1", "client-1", "buy", "1", when=NOW),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertFalse(self.sync((item,)).success)

    def test_provider_failure_malformed_and_ambiguous_empty_block(self):
        self.assertFalse(self.sync(provider=lambda token: (_ for _ in ()).throw(TimeoutError())).success)
        self.assertFalse(self.sync(provider=lambda token: {"orders": []}).success)
        self.assertFalse(self.sync(provider=lambda token: OrderHistoryPage((), None, False, True)).success)
        self.assertFalse(self.sync(provider=lambda token: OrderHistoryPage((), None, True, False)).success)

    def test_complete_pagination_and_bad_pagination(self):
        pages = {
            None: OrderHistoryPage((), "next", False, True),
            "next": OrderHistoryPage((), None, True, True),
        }
        self.assertTrue(self.sync(provider=lambda token: pages[token]).success)
        self.assertFalse(self.sync(provider=lambda token: OrderHistoryPage((), "loop", False, True)).success)

    def test_eastern_date_boundary_and_idempotent_restart(self):
        first = self.sync(now=datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc))
        self.assertTrue(first.success)
        before = load_state(self.state, today=datetime(2026, 8, 24).date())
        second = self.sync(now=datetime(2026, 8, 24, 0, 1, tzinfo=ET))
        self.assertEqual(first.evidence_id, second.evidence_id)
        after = load_state(self.state, today=NOW.date())
        self.assertEqual(before.realized_pnl_evidence_id, after.realized_pnl_evidence_id)

    def test_older_verification_and_atomic_failure_do_not_refresh(self):
        state = new_state(NOW.date())
        future = NOW + timedelta(minutes=1)
        state.realized_pnl = -10
        state.realized_pnl_verified_at = future.isoformat()
        state.realized_pnl_verification_source = "PAPER_BROKER_MANAGED_FILLS"
        state.realized_pnl_evidence_id = "newer"
        save_state(state, self.state)
        self.assertTrue(self.sync(now=NOW).success)
        unchanged = load_state(self.state, today=NOW.date())
        self.assertEqual(unchanged.realized_pnl_evidence_id, "newer")
        with patch("paper_state_service.Path.replace", side_effect=OSError("replace")):
            self.assertFalse(self.sync(now=future + timedelta(minutes=1)).success)
        unchanged = load_state(self.state, today=NOW.date())
        self.assertEqual(unchanged.realized_pnl_verified_at, future.isoformat())


if __name__ == "__main__":
    unittest.main()
