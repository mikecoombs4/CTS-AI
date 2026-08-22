import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autonomous_paper_runtime import AutonomousPaperRuntime


class AutonomousPaperRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Mock()
        self.runtime = AutonomousPaperRuntime(Path(self.temp.name), "true")

    def tearDown(self):
        self.temp.cleanup()

    def test_one_paper_client_serves_exact_read_only_lookups(self):
        self.client.get_all_positions.return_value = []
        with patch("autonomous_paper_runtime.get_paper_trading_client", return_value=self.client):
            self.runtime.lookup_by_client_order_id("exact-client-id")
            self.runtime.lookup_order_by_id("exact-broker-id")
            self.runtime.positions()
        self.client.get_order_by_client_id.assert_called_once_with("exact-client-id")
        self.client.get_order_by_id.assert_called_once_with("exact-broker-id")
        self.client.get_all_positions.assert_called_once_with()

    def test_client_rejects_any_nonexact_paper_configuration(self):
        for value in (None, "TRUE", "false", " true "):
            runtime = AutonomousPaperRuntime(Path(self.temp.name), value)
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _ = runtime.client

    def test_clock_requires_aware_broker_timestamp(self):
        self.runtime._client = self.client
        self.client.get_clock.return_value = SimpleNamespace(
            timestamp=datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc), is_open=True
        )
        self.assertTrue(self.runtime.clock().market_open)
        self.client.get_clock.return_value = SimpleNamespace(
            timestamp=datetime(2026, 8, 24, 14, 0), is_open=True
        )
        with self.assertRaises(RuntimeError):
            self.runtime.clock()

    def test_automated_readiness_uses_real_option_result_without_prompt(self):
        option = SimpleNamespace(
            acceptable=True, contract_symbol="AAPL260918C00150000", midpoint_price=1.0
        )
        candidate = SimpleNamespace(
            ticker="AAPL", direction="CALL", last_price=150.0
        )
        expected = SimpleNamespace(status="PASS")
        with patch("autonomous_paper_runtime.evaluate_option_liquidity", return_value=option) as options, patch(
            "autonomous_paper_runtime.evaluate_paper_entry_readiness", return_value=expected
        ) as readiness:
            result, created = self.runtime.build_readiness(
                candidate, datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
            )
        self.assertIs(result, expected)
        options.assert_called_once_with("AAPL", "CALL", 150.0)
        request = readiness.call_args.args[0]
        self.assertEqual(request.contract_symbol, option.contract_symbol)
        self.assertEqual(request.quantity, 1)
        self.assertEqual(request.limit_price, 1.0)
        self.assertIsNotNone(created.tzinfo)

    def test_runtime_exposes_no_live_mode_or_alternative_submitter(self):
        source = Path("autonomous_paper_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("paper=False", source)
        self.assertNotIn("main.py", source)
        self.assertEqual(source.count("submit_paper_entry"), 2)

    def test_production_history_adapter_uses_mocked_read_boundary_only(self):
        self.runtime._client = self.client
        self.client.get_orders.return_value = []
        page = self.runtime._order_history_page(None)
        self.assertTrue(page.complete)
        self.assertTrue(page.fees_complete)
        request = self.client.get_orders.call_args.kwargs["filter"]
        self.assertEqual(request.status.value, "all")
        self.assertEqual(request.limit, 500)
        self.assertEqual(request.direction.value, "asc")
        self.assertTrue(request.nested)
        for forbidden in (
            "submit_order", "cancel_order_by_id", "cancel_orders",
            "replace_order_by_id", "close_position",
        ):
            getattr(self.client, forbidden).assert_not_called()

    def test_full_size_history_is_explicitly_incomplete(self):
        self.runtime._client = self.client
        self.client.get_orders.return_value = [SimpleNamespace()] * 500
        page = self.runtime._order_history_page(None)
        self.assertFalse(page.complete)

    def test_paper_state_health_requires_fresh_verified_pnl_timestamp(self):
        now = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
        with patch("autonomous_paper_runtime.load_state") as load:
            load.return_value = SimpleNamespace(
                realized_pnl_verified_at=now.isoformat(),
                realized_pnl_verification_source="PAPER_BROKER_MANAGED_FILLS",
                realized_pnl_evidence_id="evidence",
            )
            self.assertTrue(self.runtime.paper_state_health(now)[0])
            load.return_value = SimpleNamespace(
                realized_pnl_verified_at=None,
                realized_pnl_verification_source=None,
                realized_pnl_evidence_id=None,
            )
            self.assertFalse(self.runtime.paper_state_health(now)[0])


if __name__ == "__main__":
    unittest.main()
