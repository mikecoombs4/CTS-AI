import unittest

from risk_service import build_trade_plan


class TradePlanTests(unittest.TestCase):
    def test_affordable_contract_passes(self) -> None:
        plan = build_trade_plan(
            ticker="TEST",
            contract_symbol="TEST_CALL",
            entry_price=1.20,
        )

        self.assertTrue(plan.acceptable)
        self.assertAlmostEqual(plan.position_cost, 120.0)
        self.assertAlmostEqual(plan.stop_price, 0.90)
        self.assertAlmostEqual(plan.estimated_stop_loss, 30.0)
        self.assertAlmostEqual(plan.target_1_price, 1.44)
        self.assertAlmostEqual(plan.target_2_price, 1.62)

    def test_contract_over_cap_fails(self) -> None:
        plan = build_trade_plan(
            ticker="TEST",
            contract_symbol="TEST_CALL",
            entry_price=1.60,
        )

        self.assertFalse(plan.acceptable)
        self.assertIn(
            "Position cost exceeds $150 cap",
            plan.failed_checks,
        )

    def test_fractional_cent_levels_round_up(self) -> None:
        plan = build_trade_plan(
            ticker="TEST",
            contract_symbol="TEST_CALL",
            entry_price=0.35,
        )

        self.assertAlmostEqual(plan.stop_price, 0.27)
        self.assertAlmostEqual(plan.target_1_price, 0.42)
        self.assertAlmostEqual(plan.target_2_price, 0.48)

    def test_plan_cannot_exceed_remaining_daily_budget(self) -> None:
        plan = build_trade_plan(
            ticker="TEST",
            contract_symbol="TEST_PUT",
            entry_price=1.40,
            realized_pnl_today=-30.0,
        )

        self.assertFalse(plan.acceptable)
        self.assertAlmostEqual(
            plan.remaining_daily_loss_budget,
            20.0,
        )

    def test_daily_loss_limit_stops_new_plan(self) -> None:
        plan = build_trade_plan(
            ticker="TEST",
            contract_symbol="TEST_PUT",
            entry_price=0.50,
            realized_pnl_today=-50.0,
        )

        self.assertFalse(plan.acceptable)
        self.assertIn(
            "Daily realized-loss limit of $50 reached",
            plan.failed_checks,
        )


if __name__ == "__main__":
    unittest.main()
