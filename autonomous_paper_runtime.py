"""Production PAPER-only adapters for the autonomous runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from alpaca_service import get_paper_trading_client
from cts_entry_window import MARKET_TIMEZONE
from exit_monitor import MonitorConfig, PaperExitMonitor, configure_logging
from options_service import evaluate_option_liquidity
from paper_entry_service import PaperEntryRequest, evaluate_paper_entry_readiness
from paper_execution_service import submit_paper_entry
from paper_state_service import load_state
from paper_entry_order_tracker import PaperEntryOrderTracker
from paper_realized_pl_sync import OrderHistoryPage, synchronize_managed_realized_pl


@dataclass(frozen=True)
class RuntimeClock:
    timestamp: datetime
    market_open: bool


class AutonomousPaperRuntime:
    """Owns one explicitly paper-mode production client and read adapters."""

    def __init__(self, state_dir: Path, paper_configuration: str | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.paper_configuration = paper_configuration
        self._client: Any = None
        self._monitor: PaperExitMonitor | None = None

    @property
    def client(self) -> Any:
        if self.paper_configuration != "true":
            raise RuntimeError("Exact Alpaca PAPER configuration is required.")
        if self._client is None:
            self._client = get_paper_trading_client()
        return self._client

    def clock(self) -> RuntimeClock:
        raw = self.client.get_clock()
        timestamp = getattr(raw, "timestamp", None)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise RuntimeError("Paper broker clock timestamp is unavailable.")
        return RuntimeClock(timestamp, getattr(raw, "is_open", None) is True)

    def lookup_by_client_order_id(self, client_order_id: str) -> Any:
        return self.client.get_order_by_client_id(client_order_id)

    def lookup_order_by_id(self, broker_order_id: str) -> Any:
        return self.client.get_order_by_id(broker_order_id)

    def positions(self) -> list[Any]:
        return list(self.client.get_all_positions())

    def _order_history_page(self, page_token: str | None) -> OrderHistoryPage:
        if page_token is not None:
            raise RuntimeError("Alpaca order history does not expose a safe continuation token.")
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        tracker = PaperEntryOrderTracker(self.state_dir / "paper_entry_orders.json")
        starts = [datetime.fromisoformat(item.submitted_at.replace("Z", "+00:00")) for item in tracker.records]
        market_now = getattr(
            self, "_history_as_of", datetime.now(timezone.utc)
        ).astimezone(MARKET_TIMEZONE)
        session_start = datetime.combine(market_now.date(), time.min, MARKET_TIMEZONE)
        # Include the entire current Eastern date even when its first activity
        # predates today's managed entry, plus every retained prior-day lot.
        after = min(starts + [session_start])
        raw = list(self.client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500, after=after,
            direction=Sort.ASC, nested=True,
        )))
        normalized = tuple({
            "id": getattr(item, "id", None),
            "client_order_id": getattr(item, "client_order_id", None),
            "symbol": getattr(item, "symbol", None),
            "side": getattr(item, "side", None),
            "status": getattr(item, "status", None),
            "qty": getattr(item, "qty", None),
            "filled_qty": getattr(item, "filled_qty", None),
            "filled_avg_price": getattr(item, "filled_avg_price", None),
            "filled_at": getattr(item, "filled_at", None),
            "updated_at": getattr(item, "updated_at", None),
            "order_class": getattr(item, "order_class", None),
            "legs": getattr(item, "legs", None),
            # Alpaca PAPER Order has no fee field and PAPER fills carry no fees.
            "fee": "0",
            "multiplier": "100",
        } for item in raw)
        return OrderHistoryPage(normalized, None, len(raw) < 500, True)

    def synchronize_realized_pl(self, now: datetime) -> Any:
        self._history_as_of = now
        try:
            return synchronize_managed_realized_pl(
                history_provider=self._order_history_page,
                tracker_path=self.state_dir / "paper_entry_orders.json",
                journal_path=self.state_dir / "submission_intents.json",
                state_path=self.state_dir.parent / "paper_state.json",
                now=now,
            )
        finally:
            del self._history_as_of

    def monitor_cycle(self, **kwargs: Any) -> Any:
        if self._monitor is None:
            config = MonitorConfig(
                state_file=self.state_dir / "exit_monitor_state.json",
                log_file=self.state_dir / "exit_monitor.log",
            )
            self._monitor = PaperExitMonitor(
                self.client, config=config, logger=configure_logging(config.log_file)
            )
        return self._monitor.cycle(**kwargs)

    def build_readiness(self, scanner_result: Any, now: datetime) -> tuple[Any, datetime]:
        option = evaluate_option_liquidity(
            scanner_result.ticker,
            scanner_result.direction,
            scanner_result.last_price,
        )
        if option is None or option.acceptable is not True:
            raise RuntimeError("No affordable liquid paper option was found.")
        request = PaperEntryRequest(
            ticker=scanner_result.ticker,
            contract_symbol=option.contract_symbol,
            side="BUY",
            quantity=1,
            limit_price=option.midpoint_price,
            now=now,
        )
        return evaluate_paper_entry_readiness(request), datetime.now(timezone.utc)

    def paper_state_health(self, now: datetime) -> tuple[bool, str]:
        market_now = now.astimezone(MARKET_TIMEZONE)
        state = load_state(self.state_dir.parent / "paper_state.json", today=market_now.date())
        value = state.realized_pnl_verified_at
        if not value:
            return False, "Managed paper state lacks fresh realized-P/L verification."
        if (
            state.realized_pnl_verification_source != "PAPER_BROKER_MANAGED_FILLS"
            or not state.realized_pnl_evidence_id
        ):
            return False, "Managed paper state lacks trusted broker evidence."
        try:
            verified = datetime.fromisoformat(value)
        except ValueError:
            return False, "Managed paper state realized-P/L verification is malformed."
        if verified.tzinfo is None:
            return False, "Managed paper state realized-P/L verification is timezone-naive."
        age = (now - verified).total_seconds()
        if verified.astimezone(MARKET_TIMEZONE).date() != market_now.date() or not 0 <= age <= 300:
            return False, "Managed paper state realized-P/L verification is stale."
        return True, "Managed paper state realized-P/L verification is fresh."

    @staticmethod
    def submitter(*, preview: Any, client_order_id: str) -> Any:
        return submit_paper_entry(preview, client_order_id)
