import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class ManagedPaperPosition:
    ticker: str
    contract_symbol: str
    quantity: int
    entry_price: float
    entry_order_id: str
    opened_at: str
    peak_price: float
    trailing_active: bool = False


@dataclass
class PaperSessionState:
    session_date: str
    trades_opened: int = 0
    losing_trades: int = 0
    realized_pnl: float = 0.0
    realized_pnl_verified_at: str | None = None
    submitted_contracts: list[str] = field(default_factory=list)
    positions: list[ManagedPaperPosition] = field(default_factory=list)


def state_file_path() -> Path:
    home = Path.home()

    if sys.platform == "darwin":
        directory = home / "Library" / "Application Support" / "CTS-AI"
    else:
        directory = home / ".local" / "state" / "cts-ai"

    return directory / "paper_state.json"


def new_state(today: date | None = None) -> PaperSessionState:
    today = today or date.today()
    return PaperSessionState(session_date=today.isoformat())


def _state_from_dict(data: dict) -> PaperSessionState:
    return PaperSessionState(
        session_date=str(data["session_date"]),
        trades_opened=int(data.get("trades_opened", 0)),
        losing_trades=int(data.get("losing_trades", 0)),
        realized_pnl=float(data.get("realized_pnl", 0.0)),
        realized_pnl_verified_at=(
            str(data.get("realized_pnl_verified_at"))
            if data.get("realized_pnl_verified_at") is not None
            else None
        ),
        submitted_contracts=[
            str(item).strip().upper()
            for item in data.get("submitted_contracts", [])
        ],
        positions=[
            ManagedPaperPosition(**position)
            for position in data.get("positions", [])
        ],
    )


def load_state(
    path: Path | None = None,
    today: date | None = None,
) -> PaperSessionState:
    path = path or state_file_path()
    today = today or date.today()

    if not path.exists():
        return new_state(today)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = _state_from_dict(data)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError(
            "Paper state file is unreadable; automation must remain locked."
        ) from error

    if state.session_date != today.isoformat():
        state = PaperSessionState(
            session_date=today.isoformat(),
            trades_opened=0,
            losing_trades=0,
            realized_pnl=0.0,
            realized_pnl_verified_at=None,
            submitted_contracts=[],
            positions=state.positions,
        )

    return state


def save_state(
    state: PaperSessionState,
    path: Path | None = None,
) -> None:
    path = path or state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    serialized = json.dumps(
        asdict(state),
        indent=2,
        sort_keys=True,
    )
    temporary_path.write_text(serialized, encoding="utf-8")
    temporary_path.replace(path)


def record_submitted_contract(
    state: PaperSessionState,
    contract_symbol: str,
) -> None:
    symbol = contract_symbol.strip().upper()
    if not symbol:
        raise ValueError("Contract symbol cannot be blank.")
    normalized_submitted = [item.strip().upper() for item in state.submitted_contracts]
    if symbol not in normalized_submitted:
        state.submitted_contracts.append(symbol)
        state.trades_opened += 1


def add_position(
    state: PaperSessionState,
    position: ManagedPaperPosition,
) -> None:
    symbol = position.contract_symbol.strip().upper()

    if any(
        item.contract_symbol.strip().upper() == symbol
        for item in state.positions
    ):
        raise RuntimeError("Managed position already exists for contract.")

    state.positions.append(position)

    if symbol not in [item.strip().upper() for item in state.submitted_contracts]:
        state.submitted_contracts.append(symbol)
        state.trades_opened += 1


def update_realized_pnl_verified_at(
    state: PaperSessionState,
    verified_at: datetime | None = None,
) -> None:
    verified_at = verified_at or datetime.now(timezone.utc)
    if verified_at.tzinfo is None:
        raise ValueError("Verified timestamp must include a timezone.")
    state.realized_pnl_verified_at = verified_at.isoformat()


def remove_position(
    state: PaperSessionState,
    contract_symbol: str,
    exit_price: float,
) -> float:
    normalized_symbol = contract_symbol.strip().upper()
    position = next(
        (
            item
            for item in state.positions
            if item.contract_symbol.strip().upper() == normalized_symbol
        ),
        None,
    )

    if position is None:
        raise RuntimeError("Managed position was not found.")

    pnl = (
        exit_price - position.entry_price
    ) * 100 * position.quantity
    state.realized_pnl += pnl

    if pnl < 0:
        state.losing_trades += 1

    state.positions.remove(position)
    return pnl


def make_position(
    ticker: str,
    contract_symbol: str,
    entry_price: float,
    entry_order_id: str,
    quantity: int = 1,
) -> ManagedPaperPosition:
    return ManagedPaperPosition(
        ticker=ticker.upper(),
        contract_symbol=contract_symbol,
        quantity=quantity,
        entry_price=entry_price,
        entry_order_id=entry_order_id,
        opened_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        peak_price=entry_price,
        trailing_active=False,
    )


def show_state_recovery_status() -> None:
    print("\nCTS PAPER STATE RECOVERY")
    print("Read-only inspection. No broker method is used.")

    try:
        state = load_state()
    except RuntimeError as error:
        print("Status: BLOCK")
        print(str(error))
        print("No order was submitted.")
        return

    print("Status: READY")
    print(f"State file: {state_file_path()}")
    print(f"Session date: {state.session_date}")
    print(f"Trades opened today: {state.trades_opened}")
    print(f"Losing trades today: {state.losing_trades}")
    print(f"Realized P/L today: ${state.realized_pnl:+.2f}")
    print(f"Recoverable open positions: {len(state.positions)}")

    for position in state.positions:
        print(
            f"- {position.contract_symbol} | Entry "
            f"${position.entry_price:.2f} | Peak "
            f"${position.peak_price:.2f} | Trail "
            f"active: {position.trailing_active}"
        )

    print("State inspection completed. No order was submitted.")
