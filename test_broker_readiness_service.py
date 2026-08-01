import unittest
from dataclasses import dataclass

from broker_readiness_service import classify_broker_readiness


@dataclass
class TestAccount:
    status: str = "ACTIVE"
    trading_blocked: bool = False
    account_blocked: bool = False
    trade_suspended_by_user: bool = False
    options_trading_level: int | None = 2
    options_buying_power: str = "1000.00"


@dataclass
class TestClock:
    is_open: bool = True


class BrokerReadinessTests(unittest.TestCase):
    def classify(self, account=None, clock=None, orders=None, positions=None):
        return classify_broker_readiness(
            account=account or TestAccount(),
            clock=clock or TestClock(),
            open_orders=orders or [],
            positions=positions or [],
            paper_mode=True,
        )

    def test_ready_paper_account_passes(self) -> None:
        result = self.classify()

        self.assertEqual(result.status, "PASS")

    def test_options_level_below_two_blocks(self) -> None:
        result = self.classify(
            account=TestAccount(options_trading_level=1)
        )

        self.assertEqual(result.status, "BLOCK")

    def test_insufficient_options_buying_power_blocks(self) -> None:
        result = self.classify(
            account=TestAccount(options_buying_power="100.00")
        )

        self.assertEqual(result.status, "BLOCK")

    def test_closed_market_blocks(self) -> None:
        result = self.classify(clock=TestClock(is_open=False))

        self.assertEqual(result.status, "BLOCK")

    def test_existing_open_order_blocks(self) -> None:
        result = self.classify(orders=[object()])

        self.assertEqual(result.status, "BLOCK")

    def test_two_existing_positions_block(self) -> None:
        result = self.classify(positions=[object(), object()])

        self.assertEqual(result.status, "BLOCK")


if __name__ == "__main__":
    unittest.main()
