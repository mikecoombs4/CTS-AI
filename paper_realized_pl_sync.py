"""Fail-closed realized-P/L verification from managed PAPER order evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from cts_entry_window import MARKET_TIMEZONE
from paper_entry_order_tracker import PaperEntryOrderTracker
from paper_state_service import (
    load_state,
    record_verified_realized_pnl,
    save_state,
)
from supervised_paper_entry_handoff import CORE_ORIGIN, SubmissionIntentJournal


VERIFICATION_SOURCE = "PAPER_BROKER_MANAGED_FILLS"
OPTION_MULTIPLIER = Decimal("100")


@dataclass(frozen=True)
class OrderHistoryPage:
    orders: tuple[Any, ...]
    next_page_token: str | None
    complete: bool
    fees_complete: bool


@dataclass(frozen=True)
class RealizedPLSyncResult:
    success: bool
    realized_pnl: Decimal | None
    evidence_id: str | None
    verified_at: str | None
    reason: str | None


def _value(source: Any, name: str, default: Any = None) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(_text(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} is malformed.") from error
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} is invalid.")
    return result


def _timestamp(value: Any, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} is malformed.") from error
    if result.tzinfo is None:
        raise ValueError(f"{name} must include a timezone.")
    return result


def _collect_complete_history(provider: Callable[[str | None], OrderHistoryPage]) -> list[Any]:
    token: str | None = None
    seen_tokens: set[str] = set()
    orders: list[Any] = []
    while True:
        page = provider(token)
        if not isinstance(page, OrderHistoryPage):
            raise ValueError("Paper order-history response is malformed.")
        if not isinstance(page.orders, tuple) or page.fees_complete is not True:
            raise ValueError("Paper order-history fees or records are incomplete.")
        orders.extend(page.orders)
        next_token = page.next_page_token
        if page.complete:
            if next_token is not None:
                raise ValueError("Complete paper order history contains a continuation token.")
            return orders
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise ValueError("Paper order history is truncated or has invalid pagination.")
        seen_tokens.add(next_token)
        token = next_token


def synchronize_managed_realized_pl(
    *,
    history_provider: Callable[[str | None], OrderHistoryPage],
    tracker_path: Path,
    journal_path: Path,
    state_path: Path,
    now: datetime,
) -> RealizedPLSyncResult:
    """Verify and atomically persist current-ET-date managed closed-lot P/L."""
    if now.tzinfo is None:
        return RealizedPLSyncResult(False, None, None, None, "Verification time is timezone-naive.")
    try:
        tracker = PaperEntryOrderTracker(tracker_path)
        journal = SubmissionIntentJournal(journal_path, now=now)
        state = load_state(state_path, today=now.astimezone(MARKET_TIMEZONE).date())
        orders = _collect_complete_history(history_provider)
        if len({id(item) for item in orders}) != len(orders):
            raise ValueError("Paper history contains a duplicated fill object.")

        intents = {item.client_order_id: item for item in journal.intents}
        managed: dict[str, Any] = {}
        managed_broker_ids: set[str] = set()
        for record in tracker.records:
            intent = intents.get(record.client_order_id)
            if intent is None or intent.origin != CORE_ORIGIN:
                raise ValueError("Tracker order lacks trusted CORE_CTS provenance.")
            if intent.broker_order_id not in {None, record.broker_order_id}:
                raise ValueError("Managed broker order identity is inconsistent.")
            managed[record.client_order_id] = record
            managed_broker_ids.add(record.broker_order_id)

        broker_ids: set[str] = set()
        client_ids: set[str] = set()
        canonical: list[dict[str, str]] = []
        normalized: list[dict[str, Any]] = []
        today = now.astimezone(MARKET_TIMEZONE).date()
        for order in orders:
            broker_id = _text(_value(order, "id"))
            client_id = _text(_value(order, "client_order_id"))
            symbol = _text(_value(order, "symbol")).upper()
            side = _text(_value(order, "side")).lower()
            status = _text(_value(order, "status")).lower()
            if not broker_id or not client_id or not symbol or side not in {"buy", "sell"}:
                raise ValueError("Paper order identity, symbol, or side is malformed.")
            if broker_id in broker_ids or client_id in client_ids:
                raise ValueError("Paper history contains duplicate order identity.")
            broker_ids.add(broker_id); client_ids.add(client_id)
            if _value(order, "legs") not in (None, [], ()) or _text(_value(order, "order_class", "simple")).lower() not in {"", "simple"}:
                raise ValueError("Multi-leg paper orders are unsupported.")
            qty = _decimal(_value(order, "qty"), "Order quantity", positive=True)
            filled = _decimal(_value(order, "filled_qty"), "Filled quantity")
            if filled > qty:
                raise ValueError("Paper filled quantity exceeds requested quantity.")
            if filled == 0:
                continue
            if status != "filled" and status not in {"partially_filled", "canceled", "expired", "done_for_day", "suspended"}:
                raise ValueError("Filled activity has an unsupported status.")
            price = _decimal(_value(order, "filled_avg_price"), "Average fill price", positive=True)
            fee = _decimal(_value(order, "fee"), "Broker-reported fee")
            filled_at = _timestamp(
                _value(order, "filled_at") or _value(order, "updated_at"),
                "Fill/update timestamp",
            )
            multiplier = _decimal(_value(order, "multiplier"), "Contract multiplier", positive=True)
            if multiplier != OPTION_MULTIPLIER:
                raise ValueError("Option contract multiplier is unsupported.")
            row = dict(id=broker_id, client_id=client_id, symbol=symbol, side=side,
                       qty=qty, filled=filled, price=price, fee=fee, filled_at=filled_at)
            normalized.append(row)
            canonical.append({
                "id": broker_id, "client_id": client_id, "symbol": symbol,
                "side": side, "filled": str(filled), "price": str(price),
                "fee": str(fee), "filled_at": filled_at.isoformat(),
            })

        lots: dict[str, list[Decimal]] = {}
        pnl = Decimal("0")
        reconciled_entries: set[str] = set()
        for row in sorted(normalized, key=lambda item: (item["filled_at"], item["id"])):
            is_today = row["filled_at"].astimezone(MARKET_TIMEZONE).date() == today
            if row["side"] == "buy":
                record = managed.get(row["client_id"])
                if record is None or row["id"] != record.broker_order_id:
                    if is_today:
                        raise ValueError("Unknown same-day filled BUY activity exists.")
                    continue
                if row["symbol"] != record.option_symbol.strip().upper() or row["filled"] > Decimal(str(record.requested_quantity)):
                    raise ValueError("Managed BUY symbol or quantity is inconsistent.")
                if (
                    row["filled"] != Decimal(str(record.filled_quantity))
                    or record.average_fill_price is None
                    or row["price"] != Decimal(str(record.average_fill_price))
                    or row["filled_at"] != _timestamp(
                        record.filled_at or record.updated_at,
                        "Tracker fill/update timestamp",
                    )
                ):
                    raise ValueError("Managed BUY fill evidence disagrees with the tracker.")
                reconciled_entries.add(row["client_id"])
                if row["filled"] != row["filled"].to_integral_value():
                    raise ValueError("Fractional option fills are unsupported.")
                unit_cost = row["price"] * OPTION_MULTIPLIER + row["fee"] / row["filled"]
                lots.setdefault(row["symbol"], []).extend([unit_cost] * int(row["filled"]))
            else:
                if not is_today:
                    continue
                if row["symbol"] not in lots:
                    raise ValueError("Unmatched or unknown same-day SELL activity exists.")
                if row["filled"] != row["filled"].to_integral_value():
                    raise ValueError("Fractional option fills are unsupported.")
                close_count = int(row["filled"])
                if close_count > len(lots[row["symbol"]]):
                    raise ValueError("SELL activity exceeds the managed long quantity.")
                proceeds_each = row["price"] * OPTION_MULTIPLIER - row["fee"] / row["filled"]
                for _ in range(close_count):
                    pnl += proceeds_each - lots[row["symbol"]].pop(0)

        expected_entries = {
            client_id for client_id, record in managed.items()
            if Decimal(str(record.filled_quantity)) > 0
        }
        if reconciled_entries != expected_entries:
            raise ValueError("Complete history is missing managed filled entry evidence.")

        canonical.sort(key=lambda item: (item["filled_at"], item["id"]))
        evidence_id = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        changed = record_verified_realized_pnl(
            state, trading_date=today, realized_pnl=float(pnl), verified_at=now,
            source=VERIFICATION_SOURCE, evidence_id=evidence_id,
        )
        if changed:
            save_state(state, state_path)
        return RealizedPLSyncResult(True, pnl, evidence_id, now.isoformat(), None)
    except Exception as error:
        return RealizedPLSyncResult(
            False, None, None, None,
            f"Managed PAPER realized-P/L reconciliation failed ({type(error).__name__}).",
        )
