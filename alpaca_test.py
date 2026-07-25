from pathlib import Path

from alpaca.trading.client import TradingClient
from dotenv import dotenv_values


ENV_FILE = Path(__file__).with_name(".env")
config = dotenv_values(ENV_FILE)

api_key = (config.get("ALPACA_API_KEY") or "").strip()
secret_key = (config.get("ALPACA_SECRET_KEY") or "").strip()
paper_enabled = (
    config.get("ALPACA_PAPER", "true").strip().lower()
    in {"true", "yes", "y", "1"}
)

placeholder_values = {
    "your_actual_key_id",
    "your_actual_secret_key",
}

if (
    not api_key
    or not secret_key
    or api_key in placeholder_values
    or secret_key in placeholder_values
):
    raise RuntimeError(
        "Actual Alpaca paper keys were not found in .env."
    )

if not paper_enabled:
    raise RuntimeError(
        "ALPACA_PAPER must be set to true."
    )

client = TradingClient(
    api_key,
    secret_key,
    paper=True,
)

account = client.get_account()

print("Alpaca paper connection successful.")
print(f"Account status: {account.status}")
print(f"Cash: ${float(account.cash):,.2f}")
print(
    f"Buying power: "
    f"${float(account.buying_power):,.2f}"
)
print("No order was submitted.")