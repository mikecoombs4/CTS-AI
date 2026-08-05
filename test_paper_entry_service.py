import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_entry_service import (
    PaperEntryRequest,
    evaluate_paper_entry_readiness,
    is_exact_entry_window,
)
from paper_state_service import PaperSessionState
from scanner_service import ScannerResult
from options_service import OptionLiquidityResult
from risk_service import TradePlan
from news_service import NewsRiskResult
from earnings_service import EarningsRiskResult
from broker_readiness_service import BrokerReadinessResult
from daily_limits_service import DailyLimitsResult, evaluate_daily_limits
from decision_service import FinalDecision
from market_session_service import MarketSessionResult

ET = ZoneInfo("America/New_York")


def make_scanner_candidate(ticker: str) -> ScannerResult:
    return ScannerResult(
        ticker=ticker,
        bar_timestamp=datetime(2026, 8, 5, 10, 0, tzinfo=ET),
        direction="CALL",
        last_price=150.0,
        ema_9=151.0,
        ema_20=148.0,
        box_high=149.0,
        box_low=147.0,
        box_width_percent=1.0,
        volume_ratio=2.0,
        trend_confirmed=True,
        potter_box_found=True,
        volume_confirmed=True,
        breakout_confirmed=True,
    )


def make_option_liquidity(contract_symbol: str) -> OptionLiquidityResult:
    return OptionLiquidityResult(
        ticker="AAPL",
        direction="CALL",
        contract_symbol=contract_symbol,
        expiration_date="20260917",
        strike_price=150.0,
        bid_price=0.90,
        ask_price=1.10,
        midpoint_price=1.0,
        contract_cost=100.0,
        spread_percent=20.0,
        open_interest=200,
        acceptable=True,
        failed_checks=[],
    )


def make_trade_plan() -> TradePlan:
    return TradePlan(
        ticker="AAPL",
        contract_symbol="AAPL260817C00150000",
        contracts=1,
        entry_price=1.0,
        position_cost=100.0,
        stop_price=0.75,
        estimated_stop_loss=25.0,
        target_1_price=1.20,
        target_2_price=1.35,
        realized_loss_today=0.0,
        remaining_daily_loss_budget=50.0,
        acceptable=True,
        failed_checks=[],
    )


def make_news_result() -> NewsRiskResult:
    return NewsRiskResult(
        ticker="AAPL",
        status="PASS",
        headlines=[],
        blocking_matches=[],
        catalyst_matches=[],
    )


def make_earnings_result() -> EarningsRiskResult:
    return EarningsRiskResult(
        ticker="AAPL",
        status="PASS",
        report_date=None,
        days_until_report=None,
        reason="No earnings concern",
    )


def make_market_session() -> MarketSessionResult:
    return MarketSessionResult(
        status="PASS",
        market_time=datetime(2026, 8, 5, 10, 0, tzinfo=ET),
        entry_allowed=True,
        reason="Morning CTS entry window is open.",
    )


def make_broker_readiness() -> BrokerReadinessResult:
    return BrokerReadinessResult(
        status="PASS",
        paper_mode=True,
        account_status="ACTIVE",
        options_trading_level=2,
        options_buying_power=1000.0,
        market_open=True,
        open_orders=0,
        open_positions=0,
        reasons=["Paper broker readiness checks passed"],
    )


def make_daily_limits() -> DailyLimitsResult:
    return DailyLimitsResult(
        status="PASS",
        new_trade_allowed=True,
        trades_opened_today=0,
        open_positions=0,
        losing_trades_today=0,
        realized_pnl_today=0.0,
        reasons=["Daily anti-overtrading limits are available"],
    )


def make_final_decision() -> FinalDecision:
    return FinalDecision(
        ticker="AAPL",
        status="PASS",
        automatic_paper_eligible=True,
        reasons=["Every CTS technical and risk gate passed"],
    )


class PaperEntryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = PaperEntryRequest(
            ticker="AAPL",
            contract_symbol="AAPL260817C00150000",
            side="BUY",
            quantity=1,
            limit_price=1.00,
            now=datetime(2026, 8, 5, 10, 0, tzinfo=ET),
        )

        self.state = PaperSessionState(
            session_date="2026-08-05",
            trades_opened=0,
            losing_trades=0,
            realized_pnl=0.0,
            realized_pnl_verified_at=datetime(2026, 8, 5, 9, 59, tzinfo=ET).isoformat(),
            submitted_contracts=[],
            positions=[],
        )

    def _patch_everything(self, **overrides):
        defaults = {
            "_load_state": self.state,
            "evaluate_market_session": make_market_session(),
            "evaluate_broker_readiness": make_broker_readiness(),
            "fetch_scanner_results": ([make_scanner_candidate("AAPL")], []),
            "evaluate_option_liquidity": make_option_liquidity(
                self.request.contract_symbol
            ),
            "build_trade_plan": make_trade_plan(),
            "evaluate_news_risk": make_news_result(),
            "evaluate_earnings_risk": make_earnings_result(),
            "evaluate_final_decision": make_final_decision(),
            "evaluate_daily_limits": make_daily_limits(),
            "paper_execution_enabled": False,
        }
        defaults.update(overrides)

        patchers = []
        patchers.append(
            patch("paper_entry_service._load_state", return_value=defaults["_load_state"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_market_session", return_value=defaults["evaluate_market_session"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_broker_readiness", return_value=defaults["evaluate_broker_readiness"])
        )
        patchers.append(
            patch("paper_entry_service.fetch_scanner_results", return_value=defaults["fetch_scanner_results"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_option_liquidity", return_value=defaults["evaluate_option_liquidity"])
        )
        patchers.append(
            patch("paper_entry_service.build_trade_plan", return_value=defaults["build_trade_plan"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_news_risk", return_value=defaults["evaluate_news_risk"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_earnings_risk", return_value=defaults["evaluate_earnings_risk"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_final_decision", return_value=defaults["evaluate_final_decision"])
        )
        patchers.append(
            patch("paper_entry_service.evaluate_daily_limits", return_value=defaults["evaluate_daily_limits"])
        )
        patchers.append(
            patch("paper_entry_service.paper_execution_enabled", return_value=defaults["paper_execution_enabled"])
        )

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_exact_morning_window_boundaries(self) -> None:
        self.request.now = datetime(2026, 8, 5, 9, 45, 0, tzinfo=ET)
        self.assertTrue(is_exact_entry_window(self.request.now))
        self.request.now = datetime(2026, 8, 5, 11, 29, 59, tzinfo=ET)
        self.assertTrue(is_exact_entry_window(self.request.now))

    def test_exact_afternoon_window_boundaries(self) -> None:
        self.request.now = datetime(2026, 8, 5, 13, 0, 0, tzinfo=ET)
        self.assertTrue(is_exact_entry_window(self.request.now))
        self.request.now = datetime(2026, 8, 5, 15, 29, 59, tzinfo=ET)
        self.assertTrue(is_exact_entry_window(self.request.now))

    def test_weekend_blocks_entries(self) -> None:
        self.request.now = datetime(2026, 8, 8, 10, 0, tzinfo=ET)
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(
            any("outside the exact allowed entry windows" in reason for reason in report.reasons)
        )

    def test_morning_window_just_before_open_blocks(self) -> None:
        self.request.now = datetime(2026, 8, 5, 9, 44, 59, tzinfo=ET)
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(
            any("outside the exact allowed entry windows" in reason for reason in report.reasons)
        )

    def test_morning_window_at_closing_blocks(self) -> None:
        self.request.now = datetime(2026, 8, 5, 11, 30, 0, tzinfo=ET)
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(
            any("outside the exact allowed entry windows" in reason for reason in report.reasons)
        )

    def test_afternoon_window_just_before_open_blocks(self) -> None:
        self.request.now = datetime(2026, 8, 5, 12, 59, 59, tzinfo=ET)
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(
            any("outside the exact allowed entry windows" in reason for reason in report.reasons)
        )

    def test_afternoon_window_at_closing_blocks(self) -> None:
        self.request.now = datetime(2026, 8, 5, 15, 30, 0, tzinfo=ET)
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(
            any("outside the exact allowed entry windows" in reason for reason in report.reasons)
        )

    def test_cost_cap_blocks_entries(self) -> None:
        self.request.limit_price = 1.51
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("Contract cost exceeds" in reason for reason in report.reasons))

    def test_wrong_side_blocks_entries(self) -> None:
        self.request.side = "SELL"
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("Only BUY entries are supported" in reason for reason in report.reasons))

    def test_wrong_quantity_blocks_entries(self) -> None:
        self.request.quantity = 2
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("Quantity must be exactly 1" in reason for reason in report.reasons))

    def test_missing_realized_pnl_fails_closed(self) -> None:
        with patch("paper_entry_service._load_state", side_effect=RuntimeError("state inaccessible")):
            report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("Paper state is unavailable" in reason for reason in report.reasons))

    def test_daily_loss_limit_blocks_entries(self) -> None:
        self.state.realized_pnl = -50.0
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("Daily realized loss limit" in reason for reason in report.reasons))

    def test_restart_safe_duplicate_prevention_blocks_entries(self) -> None:
        self.state.submitted_contracts = [self.request.contract_symbol]
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("already been submitted today" in reason for reason in report.reasons))

    def test_open_position_duplicate_prevention_blocks_entries(self) -> None:
        self.state.positions = [type("P", (), {"contract_symbol": self.request.contract_symbol})()]
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any("already open in the managed paper state" in reason for reason in report.reasons))

    def test_locked_execution_flag_is_reported(self) -> None:
        self._patch_everything(paper_execution_enabled=False)
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.execution_enabled)
        self.assertFalse(report.submission_allowed)

    def test_submission_allowed_remains_false(self) -> None:
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.submission_allowed)

    def test_all_gates_passes_when_everything_is_ready(self) -> None:
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertTrue(report.allowed)
        self.assertEqual(report.status, "PASS")
        self.assertIsNotNone(report.order_preview)
        self.assertFalse(report.execution_enabled)

    def test_missing_realized_pnl_timestamp_blocks_entries(self) -> None:
        self.state.realized_pnl_verified_at = None
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_malformed_realized_pnl_timestamp_blocks_entries(self) -> None:
        self.state.realized_pnl_verified_at = "not-a-valid-timestamp"
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_previous_trading_date_realized_pnl_timestamp_blocks_entries(self) -> None:
        self.state.realized_pnl_verified_at = datetime(2026, 8, 4, 15, 0, tzinfo=ET).isoformat()
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_realized_pnl_timestamp_older_than_five_minutes_blocks_entries(self) -> None:
        self.state.realized_pnl_verified_at = datetime(2026, 8, 5, 9, 54, 0, tzinfo=ET).isoformat()
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_future_realized_pnl_timestamp_blocks_entries(self) -> None:
        self.state.realized_pnl_verified_at = datetime(2026, 8, 5, 10, 5, tzinfo=ET).isoformat()
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_fresh_realized_pnl_timestamp_passes(self) -> None:
        self.state.realized_pnl_verified_at = datetime(2026, 8, 5, 9, 56, 0, tzinfo=ET).isoformat()
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertTrue(report.allowed)
        self.assertFalse(any(
            "realized P/L synchronization is unavailable or stale" in reason
            for reason in report.reasons
        ))

    def test_requested_contract_root_mismatch_blocks_entries(self) -> None:
        self.request.contract_symbol = "MSFT260817C00150000"
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Requested contract root does not match the requested ticker" in reason
            for reason in report.reasons
        ))

    def test_requested_contract_symbol_mismatch_blocks_entries(self) -> None:
        bad_liquidity = make_option_liquidity("AAPL260817C00152000")
        self._patch_everything(evaluate_option_liquidity=bad_liquidity)
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Requested contract symbol does not match the option liquidity contract symbol" in reason
            for reason in report.reasons
        ))

    def test_requested_contract_direction_mismatch_blocks_entries(self) -> None:
        bad_liquidity = make_option_liquidity(self.request.contract_symbol)
        bad_liquidity.direction = "PUT"
        self._patch_everything(evaluate_option_liquidity=bad_liquidity)
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Requested contract direction does not match the option liquidity result direction" in reason
            for reason in report.reasons
        ))

    def test_liquidity_contract_cost_over_cap_blocks_entries(self) -> None:
        expensive_liquidity = make_option_liquidity(self.request.contract_symbol)
        expensive_liquidity.contract_cost = 200.0
        self._patch_everything(evaluate_option_liquidity=expensive_liquidity)
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Conservative contract cost exceeds" in reason
            for reason in report.reasons
        ))

    def test_daily_trades_opened_limit_blocks_entries(self) -> None:
        self.state.trades_opened = 2
        self._patch_everything(evaluate_daily_limits=evaluate_daily_limits(2, 0, 0, 0.0))
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Daily maximum of 2 trades reached" in reason
            for reason in report.reasons
        ))

    def test_broker_and_state_positions_agreement_is_required(self) -> None:
        self.state.positions = [type("P", (), {"contract_symbol": self.request.contract_symbol})()]
        broker = make_broker_readiness()
        broker.open_positions = 0
        self._patch_everything(evaluate_broker_readiness=broker)
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Broker open position count does not match managed paper state positions" in reason
            for reason in report.reasons
        ))

    def test_realized_pnl_exactly_negative_fifty_blocks_entries(self) -> None:
        self.state.realized_pnl = -50.0
        self._patch_everything()
        report = evaluate_paper_entry_readiness(self.request)
        self.assertFalse(report.allowed)
        self.assertTrue(any(
            "Daily realized loss limit has been reached" in reason
            for reason in report.reasons
        ))

    def test_locked_execution_enabled_still_disallows_submission(self) -> None:
        self._patch_everything(paper_execution_enabled=True)
        with patch("paper_execution_service.submit_paper_entry") as submit_mock:
            report = evaluate_paper_entry_readiness(self.request)
            self.assertTrue(report.execution_enabled)
            self.assertFalse(report.submission_allowed)
            submit_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
