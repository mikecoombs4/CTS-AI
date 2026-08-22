"""Command-line entry point for the PAPER-only autonomous CTS runner."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from autonomous_paper_policy import evaluate_autonomous_paper_policy
from autonomous_paper_runner import (
    AutonomousPaperRunner,
    RunnerDependencies,
    RunnerPaths,
)
from autonomous_paper_runtime import AutonomousPaperRuntime
from broker_readiness_service import evaluate_broker_readiness
from core_candidate_selector import select_core_candidates
from paper_runner_state import RunnerLockUnavailable
from paper_trial_preflight import run_autonomous_paper_startup_preflight
from scanner_service import fetch_scanner_results
from supervised_paper_entry_handoff import submit_supervised_paper_entry
from watchlist_service import resolve_watchlist


def _state_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "CTS-AI" / "autonomous-paper"


def _configuration() -> dict[str, str | None]:
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).with_name(".env"))
    names = (
        "ALPACA_PAPER", "CTS_AUTONOMOUS_PAPER_ENABLED",
        "CTS_PAPER_EXECUTION_ENABLED", "CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW",
        "CTS_TRIAL_MAX_TRADES_PER_DAY", "CTS_TRIAL_MAX_OPEN_POSITIONS",
    )
    return {name: values.get(name) for name in names}


def build_runner() -> AutonomousPaperRunner:
    paths = RunnerPaths(_state_root())
    paths.root.mkdir(parents=True, exist_ok=True)
    configuration = _configuration()
    runtime = AutonomousPaperRuntime(paths.root, configuration.get("ALPACA_PAPER"))
    dependencies = RunnerDependencies(
        configuration=configuration,
        resolve_watchlist=resolve_watchlist,
        scan=fetch_scanner_results,
        build_readiness=runtime.build_readiness,
        policy=evaluate_autonomous_paper_policy,
        selector=select_core_candidates,
        startup_preflight=run_autonomous_paper_startup_preflight,
        broker_readiness=evaluate_broker_readiness,
        clock=runtime.clock,
        lookup_client_order=runtime.lookup_by_client_order_id,
        lookup_broker_order=runtime.lookup_order_by_id,
        positions=runtime.positions,
        monitor_cycle=runtime.monitor_cycle,
        synchronize_realized_pl=runtime.synchronize_realized_pl,
        paper_state_health=runtime.paper_state_health,
        handoff=submit_supervised_paper_entry,
        submitter=runtime.submitter,
        now=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
    )
    return AutonomousPaperRunner(paths, dependencies)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="CTS autonomous Alpaca PAPER runner")
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--execute-paper", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = "check" if args.check else "dry-run" if args.dry_run else "execute-paper"
    print(f"CTS AUTONOMOUS PAPER: {mode.upper()}")
    if mode == "execute-paper":
        print("PAPER ONLY. No live-trading mode exists.")
    try:
        result = build_runner().run(mode)
    except RunnerLockUnavailable:
        print("BLOCKED: another autonomous paper runner holds the lock.")
        return 2
    except KeyboardInterrupt:
        print("Stopped safely by Control-C; runner lock released.")
        return 130
    except Exception as error:
        print(f"BLOCKED: startup failed closed ({type(error).__name__}).")
        return 2
    print(f"STATUS: {result.status}")
    print(f"ENTRY_GATE: {'OPEN' if result.entry_gate_open else 'BLOCKED'}")
    if mode == "dry-run":
        print("All selections are DRY_RUN_ONLY; no entry submission was called.")
    return 0 if result.status not in {"BLOCKED"} else 2


if __name__ == "__main__":
    sys.exit(main())
