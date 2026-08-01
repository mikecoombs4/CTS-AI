from dataclasses import dataclass
from pathlib import Path
from typing import Any

from order_preview_service import PaperOrderPreview


ENV_FILE = Path(__file__).with_name(".env")
ENABLE_VARIABLE = "CTS_PAPER_EXECUTION_ENABLED"
ENABLE_VALUE = "YES_PAPER_ONLY"


@dataclass
class PaperEntryPayload:
    symbol: str
    qty: int
    side: str
    order_type: str
    time_in_force: str
    limit_price: float
    position_intent: str
    extended_hours: bool
    client_order_id: str


def paper_execution_enabled() -> bool:
    from dotenv import dotenv_values

    config = dotenv_values(ENV_FILE)
    return (
        config.get(ENABLE_VARIABLE) or ""
    ).strip() == ENABLE_VALUE


def build_paper_entry_payload(
    preview: PaperOrderPreview,
    client_order_id: str,
) -> PaperEntryPayload:
    if not preview.eligible:
        raise RuntimeError("Order preview is not eligible.")

    if not client_order_id.strip():
        raise ValueError("Client order ID cannot be blank.")

    if len(client_order_id) > 128:
        raise ValueError("Client order ID exceeds 128 characters.")

    return PaperEntryPayload(
        symbol=preview.contract_symbol,
        qty=preview.quantity,
        side="buy",
        order_type="limit",
        time_in_force="day",
        limit_price=preview.limit_price,
        position_intent="buy_to_open",
        extended_hours=False,
        client_order_id=client_order_id,
    )


def validate_submission_authorization(
    preview: PaperOrderPreview,
    execution_enabled: bool,
    duplicate_open_order: bool,
) -> list[str]:
    reasons = []

    if not preview.eligible:
        reasons.append("Paper-order preview is not eligible")

    if not execution_enabled:
        reasons.append("Paper execution kill switch is locked")

    if duplicate_open_order:
        reasons.append("An open order already exists for this contract")

    return reasons


def _value(source: Any, name: str, default=None):
    if isinstance(source, dict):
        return source.get(name, default)

    return getattr(source, name, default)


def _duplicate_order_exists(client, symbol: str) -> bool:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
    )

    return any(
        str(_value(order, "symbol", "")) == symbol
        for order in orders
    )


def submit_paper_entry(
    preview: PaperOrderPreview,
    client_order_id: str,
):
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import (
        OrderSide,
        PositionIntent,
        TimeInForce,
    )
    from alpaca.trading.requests import LimitOrderRequest

    from alpaca_service import get_alpaca_credentials

    payload = build_paper_entry_payload(
        preview=preview,
        client_order_id=client_order_id,
    )
    api_key, secret_key = get_alpaca_credentials()
    client = TradingClient(api_key, secret_key, paper=True)
    duplicate = _duplicate_order_exists(
        client,
        preview.contract_symbol,
    )
    failures = validate_submission_authorization(
        preview=preview,
        execution_enabled=paper_execution_enabled(),
        duplicate_open_order=duplicate,
    )

    if failures:
        raise RuntimeError("; ".join(failures))

    request = LimitOrderRequest(
        symbol=payload.symbol,
        qty=payload.qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=payload.limit_price,
        extended_hours=False,
        client_order_id=payload.client_order_id,
        position_intent=PositionIntent.BUY_TO_OPEN,
    )

    return client.submit_order(order_data=request)


def show_paper_execution_lock() -> None:
    enabled = paper_execution_enabled()
    status = "ENABLED" if enabled else "LOCKED"

    print("\nCTS PAPER EXECUTION KILL SWITCH")
    print(f"Status: {status}")
    print("Broker environment is hard-coded to paper=True.")
    print("Options entries use BUY LIMIT / DAY / BUY_TO_OPEN.")
    print("Duplicate open orders for the same contract are refused.")

    if not enabled:
        print(
            f"Private .env flag {ENABLE_VARIABLE} is not enabled."
        )
    else:
        print("Paper submissions are permitted by the private flag.")

    print("Status check only. No order was submitted.")
