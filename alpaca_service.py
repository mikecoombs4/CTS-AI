from pathlib import Path

from alpaca.trading.client import TradingClient
from dotenv import dotenv_values


ENV_FILE = Path(__file__).with_name(".env")


def get_alpaca_credentials() -> tuple[str, str]:
    config = dotenv_values(ENV_FILE)

    api_key = (config.get("ALPACA_API_KEY") or "").strip()
    secret_key = (
        config.get("ALPACA_SECRET_KEY") or ""
    ).strip()

    if not api_key or not secret_key:
        raise RuntimeError(
            "Alpaca paper credentials are missing."
        )

    return api_key, secret_key


def get_paper_account():
    return get_paper_trading_client().get_account()


def get_paper_trading_client() -> TradingClient:
    """Return a client that is permanently restricted to paper trading."""
    api_key, secret_key = get_alpaca_credentials()

    return TradingClient(
        api_key,
        secret_key,
        paper=True,
    )


def show_paper_account_status() -> None:
    try:
        account = get_paper_account()
    except Exception as error:
        print("\nUnable to connect to Alpaca paper trading.")
        print(f"Reason: {error}")
        return

    print("\nALPACA PAPER ACCOUNT")
    print(f"Status: {account.status}")
    print(f"Cash: ${float(account.cash):,.2f}")
    print(
        f"Buying power: "
        f"${float(account.buying_power):,.2f}"
    )
    print(f"Equity: ${float(account.equity):,.2f}")
    print(f"Trading blocked: {account.trading_blocked}")
    print("Read-only check completed.")
    print("No order was submitted.")
