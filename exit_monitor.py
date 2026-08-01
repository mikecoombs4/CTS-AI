"""Automatic exits for CTS-AI's Alpaca paper account."""

from __future__ import annotations

import json
import logging
import re
import signal
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

from exit_service import evaluate_exit


MARKET_TIMEZONE = ZoneInfo("America/New_York")
FORCED_0DTE_EXIT = clock_time(15, 55)
OPTION_SYMBOL = re.compile(
    r"^(?P<root>[A-Z0-9]{1,6})(?P<expiry>\d{6})[CP]\d{8}$"
)
ACTIVE_ORDER_STATUSES = {
    "accepted",
    "accepted_for_bidding",
    "calculated",
    "held",
    "new",
    "partially_filled",
    "pending_cancel",
    "pending_new",
    "pending_replace",
    "stopped",
}
EXIT_REASONS = {
    "EXIT_INITIAL_STOP": "25% initial stop",
    "EXIT_TRAILING_STOP": "10% trailing stop",
    "EXIT_TARGET": "35% profit target",
}


def monitor_data_directory() -> Path:
    return Path.home() / "Library" / "Application Support" / "CTS-AI"


@dataclass(frozen=True)
class MonitorConfig:
    poll_seconds: float = 5.0
    state_file: Path = field(
        default_factory=lambda: monitor_data_directory()
        / "exit_monitor_state.json"
    )
    log_file: Path = field(
        default_factory=lambda: monitor_data_directory()
        / "exit_monitor.log"
    )


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").lower()


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def eastern_time(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Monitor timestamps must include a timezone.")
    return current.astimezone(MARKET_TIMEZONE)


def option_expiration(symbol: str) -> date | None:
    match = OPTION_SYMBOL.fullmatch(symbol.upper())
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group("expiry"), "%y%m%d"
        ).date()
    except ValueError:
        return None


def is_option_position(position: Any) -> bool:
    asset_class = _enum_text(_value(position, "asset_class"))
    symbol = str(_value(position, "symbol", ""))
    return asset_class in {"us_option", "option", "options"} or (
        option_expiration(symbol) is not None
    )


def is_zero_dte(position: Any, market_date: date) -> bool:
    symbol = str(_value(position, "symbol", ""))
    return is_option_position(position) and (
        option_expiration(symbol) == market_date
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "positions": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "positions": {}}
    if not isinstance(state, dict):
        return {"version": 1, "positions": {}}
    state.setdefault("version", 1)
    state.setdefault("positions", {})
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def configure_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("cts.exit_monitor")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger


class PaperExitMonitor:
    def __init__(
        self,
        client: Any,
        config: MonitorConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.config = config or MonitorConfig()
        self.logger = logger or configure_logging(
            self.config.log_file
        )
        self.state = _load_state(self.config.state_file)

    def _active_orders(self) -> list[Any]:
        orders = self.client.get_orders()
        return [
            order
            for order in orders
            if _enum_text(_value(order, "status"))
            in ACTIVE_ORDER_STATUSES
        ]

    @staticmethod
    def _orders_for_symbol(
        orders: list[Any], symbol: str
    ) -> list[Any]:
        return [
            order
            for order in orders
            if str(_value(order, "symbol", "")).upper()
            == symbol.upper()
        ]

    def _cancel_symbol_orders(
        self, symbol: str, orders: list[Any]
    ) -> bool:
        canceled_any = False
        for order in self._orders_for_symbol(orders, symbol):
            order_id = _value(order, "id")
            if order_id is None:
                continue
            self.client.cancel_order_by_id(order_id)
            canceled_any = True
            self.logger.warning(
                "%s: canceled working order %s before exit",
                symbol,
                order_id,
            )
        return canceled_any

    def _cancel_cutoff_orders(
        self, orders: list[Any], market_date: date
    ) -> int:
        canceled = 0
        for order in orders:
            symbol = str(_value(order, "symbol", "")).upper()
            intent = _enum_text(_value(order, "position_intent"))
            is_entry = intent in {"buy_to_open", "sell_to_open"}
            is_zero_dte_order = option_expiration(symbol) == market_date
            if not (is_entry or is_zero_dte_order):
                continue

            order_id = _value(order, "id")
            if order_id is None:
                continue
            self.client.cancel_order_by_id(order_id)
            canceled += 1
            self.logger.warning(
                "%s: canceled cutoff order %s before 0DTE exits",
                symbol,
                order_id,
            )
        return canceled

    def _submit_close(
        self, symbol: str, reason: str, record: dict[str, Any]
    ) -> None:
        order = self.client.close_position(symbol)
        order_id = str(_value(order, "id", "unknown"))
        record["pending_exit_order_id"] = order_id
        record["exit_reason"] = reason
        record["exit_submitted_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        self.logger.warning(
            "%s: PAPER close submitted (%s), order=%s",
            symbol,
            reason,
            order_id,
        )

    def _normal_exit_reason(
        self, position: Any, record: dict[str, Any]
    ) -> str | None:
        if _enum_text(_value(position, "side")) != "long":
            return None

        current = _float_value(_value(position, "current_price"))
        entry = _float_value(_value(position, "avg_entry_price"))
        peak = _float_value(record.get("peak_price"))
        if current is None or entry is None or entry <= 0:
            return None

        decision = evaluate_exit(
            entry_price=entry,
            current_price=current,
            peak_price=peak,
            trailing_active=bool(record.get("trailing_active", False)),
        )
        record["peak_price"] = decision.peak_price
        record["trailing_active"] = decision.trailing_active
        record["trailing_stop_price"] = decision.trailing_stop_price
        record["last_price"] = current
        return EXIT_REASONS.get(decision.action)

    def _reconcile_state(self, positions: list[Any]) -> None:
        open_symbols = {
            str(_value(position, "symbol", "")).upper()
            for position in positions
        }
        tracked = self.state["positions"]
        for symbol in list(tracked):
            if symbol not in open_symbols:
                reason = tracked[symbol].get("exit_reason", "position closed")
                self.logger.info("%s: position is closed (%s)", symbol, reason)
                del tracked[symbol]

    def cycle(self, now: datetime | None = None) -> None:
        market_now = eastern_time(now)
        positions = list(self.client.get_all_positions())
        orders = self._active_orders()
        self._reconcile_state(positions)

        zero_dte_positions = [
            position
            for position in positions
            if is_zero_dte(position, market_now.date())
        ]
        forced_window = (
            market_now.weekday() < 5
            and market_now.time() >= FORCED_0DTE_EXIT
        )

        if forced_window:
            today = market_now.date().isoformat()
            if self.state.get("forced_cancel_date") != today:
                canceled = self._cancel_cutoff_orders(
                    orders,
                    market_now.date(),
                )
                self.state["forced_cancel_date"] = today
                self.logger.critical(
                    "3:55 PM ET safety boundary: canceled %d entry/0DTE "
                    "paper orders before forced liquidation",
                    canceled,
                )
                _save_state(self.config.state_file, self.state)
                return

        if forced_window and zero_dte_positions:
            for position in zero_dte_positions:
                symbol = str(_value(position, "symbol", "")).upper()
                record = self.state["positions"].setdefault(symbol, {})
                record["exit_reason"] = "3:55 PM ET forced 0DTE close"
                symbol_orders = self._orders_for_symbol(orders, symbol)
                pending_id = str(record.get("pending_exit_order_id", ""))

                if pending_id and any(
                    str(_value(order, "id", "")) == pending_id
                    for order in symbol_orders
                ):
                    self.logger.info(
                        "%s: waiting for forced PAPER exit order %s",
                        symbol,
                        pending_id,
                    )
                    continue

                record.pop("pending_exit_order_id", None)
                if self._cancel_symbol_orders(symbol, symbol_orders):
                    continue
                self._submit_close(
                    symbol,
                    "3:55 PM ET forced 0DTE close",
                    record,
                )

            _save_state(self.config.state_file, self.state)
            return

        for position in positions:
            if not is_option_position(position):
                continue

            symbol = str(_value(position, "symbol", "")).upper()
            record = self.state["positions"].setdefault(symbol, {})
            reason = record.get("exit_reason")
            if not reason:
                reason = self._normal_exit_reason(position, record)
                if reason:
                    record["exit_reason"] = reason

            if not reason:
                continue

            symbol_orders = self._orders_for_symbol(orders, symbol)
            pending_id = str(record.get("pending_exit_order_id", ""))
            if pending_id and any(
                str(_value(order, "id", "")) == pending_id
                for order in symbol_orders
            ):
                continue

            record.pop("pending_exit_order_id", None)
            if self._cancel_symbol_orders(symbol, symbol_orders):
                continue
            self._submit_close(symbol, str(reason), record)

        _save_state(self.config.state_file, self.state)

    def run(self, stop_event: Event | None = None) -> None:
        stop = stop_event or Event()
        self.logger.info(
            "CTS automatic PAPER exit monitor started: stop=-25%%, "
            "trail=+20%% activation/10%% distance, target=+35%%, "
            "forced 0DTE close=3:55 PM ET"
        )

        while not stop.is_set():
            try:
                self.cycle()
            except Exception:
                self.logger.exception(
                    "Monitor cycle failed; retrying in %.1f seconds",
                    self.config.poll_seconds,
                )
            stop.wait(self.config.poll_seconds)

        self.logger.info("CTS automatic PAPER exit monitor stopped")


def run_paper_exit_monitor() -> None:
    from alpaca_service import get_paper_trading_client

    config = MonitorConfig()
    logger = configure_logging(config.log_file)
    try:
        client = get_paper_trading_client()
        account = client.get_account()
    except Exception as error:
        logger.error("Unable to connect to Alpaca PAPER trading: %s", error)
        return

    if bool(_value(account, "trading_blocked", False)):
        logger.error("Alpaca PAPER account is trading-blocked; monitor stopped")
        return

    stop = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    PaperExitMonitor(
        client=client,
        config=config,
        logger=logger,
    ).run(stop)


if __name__ == "__main__":
    run_paper_exit_monitor()
