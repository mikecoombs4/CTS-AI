import json
import os
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
    realized_pnl_verification_source: str | None = None
    realized_pnl_evidence_id: str | None = None
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


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate paper-state JSON key: {key}")
        result[key] = value
    return result


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
        realized_pnl_verification_source=(
            str(data["realized_pnl_verification_source"])
            if data.get("realized_pnl_verification_source") is not None else None
        ),
        realized_pnl_evidence_id=(
            str(data["realized_pnl_evidence_id"])
            if data.get("realized_pnl_evidence_id") is not None else None
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
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        if not isinstance(data, dict):
            raise ValueError("paper state root is malformed")
        state = _state_from_dict(data)
        if state.realized_pnl_verification_source not in {
            None, "PAPER_BROKER_MANAGED_FILLS",
        }:
            raise ValueError("paper state verification source is unknown")
        if bool(state.realized_pnl_verification_source) != bool(state.realized_pnl_evidence_id):
            raise ValueError("paper state verification metadata is incomplete")
        if state.realized_pnl_verified_at is not None:
            verified = datetime.fromisoformat(state.realized_pnl_verified_at)
            if verified.tzinfo is None:
                raise ValueError("paper state verification timestamp is naive")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError(
            "Paper state file is unreadable; automation must remain locked."
        ) from error

    if state.session_date != today.isoformat():
        state = PaperSessionState(
            session_date=today.isoformat(),
            trades_opened=0,
            losing_trades=0,
            # Retain the last value as stale until the new trading date is
            # independently proved.  Consumers must check the timestamp/date.
            realized_pnl=state.realized_pnl,
            realized_pnl_verified_at=state.realized_pnl_verified_at,
            realized_pnl_verification_source=state.realized_pnl_verification_source,
            realized_pnl_evidence_id=state.realized_pnl_evidence_id,
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
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except Exception as error:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("Paper state atomic write failed.") from error


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


def record_verified_realized_pnl(
    state: PaperSessionState,
    *,
    trading_date: date,
    realized_pnl: float,
    verified_at: datetime,
    source: str,
    evidence_id: str,
) -> bool:
    if verified_at.tzinfo is None:
        raise ValueError("Verified timestamp must include a timezone.")
    if source != "PAPER_BROKER_MANAGED_FILLS":
        raise ValueError("Realized P/L verification source is invalid.")
    if not evidence_id or not isinstance(evidence_id, str):
        raise ValueError("Realized P/L evidence identity is required.")
    existing = state.realized_pnl_verified_at
    if existing is not None:
        prior = datetime.fromisoformat(existing)
        if prior.tzinfo is None:
            raise ValueError("Existing verification timestamp is invalid.")
        if verified_at < prior:
            return False
        if verified_at == prior and state.realized_pnl_evidence_id != evidence_id:
            raise RuntimeError("Conflicting realized P/L evidence has the same timestamp.")
    state.session_date = trading_date.isoformat()
    state.realized_pnl = float(realized_pnl)
    state.realized_pnl_verified_at = verified_at.isoformat()
    state.realized_pnl_verification_source = source
    state.realized_pnl_evidence_id = evidence_id
    return True


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
