"""Production-wired, paper-only autonomous CTS runner."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable

from autonomous_paper_policy import evaluate_autonomous_paper_policy
from core_candidate_selector import (
    CORE_ORIGIN,
    CandidateEvaluation,
    decision_identity,
    latest_completed_bar_start,
    select_core_candidates,
)
from cts_entry_window import MARKET_TIMEZONE, cts_entry_window_open
from exit_monitor_supervisor import load_exit_monitor_health, supervise_exit_monitor
from paper_entry_order_tracker import PaperEntryOrderTracker
from paper_runner_state import PaperRunnerLedger, SingleRunnerLock
from paper_trial_preflight import TrialLimits
from supervised_paper_entry_handoff import SubmissionIntentJournal
from uncertain_submission_reconciler import reconcile_uncertain_submission


RUN_MODES = {"check", "dry-run", "execute-paper"}
AUDIT_VERSION = 1
DEFAULT_POLL_SECONDS = 30.0


@dataclass(frozen=True)
class RunnerPaths:
    root: Path

    @property
    def lock(self) -> Path: return self.root / "autonomous_runner.lock"
    @property
    def ledger(self) -> Path: return self.root / "autonomous_runner_state.json"
    @property
    def journal(self) -> Path: return self.root / "submission_intents.json"
    @property
    def tracker(self) -> Path: return self.root / "paper_entry_orders.json"
    @property
    def health(self) -> Path: return self.root / "exit_monitor_health.json"
    @property
    def audit(self) -> Path: return self.root / "autonomous_runner_audit.json"


@dataclass
class RunnerDependencies:
    configuration: dict[str, Any]
    resolve_watchlist: Callable[[], list[str]]
    scan: Callable[..., tuple[list[Any], list[str]]]
    build_readiness: Callable[[Any, datetime], tuple[Any, datetime]]
    policy: Callable[..., Any]
    selector: Callable[..., Any]
    startup_preflight: Callable[..., Any]
    broker_readiness: Callable[[], Any]
    clock: Callable[[], Any]
    lookup_client_order: Callable[[str], Any]
    lookup_broker_order: Callable[[str], Any]
    positions: Callable[[], list[Any]]
    monitor_cycle: Callable[..., Any]
    synchronize_realized_pl: Callable[[datetime], Any]
    paper_state_health: Callable[[datetime], tuple[bool, str]]
    handoff: Callable[..., Any]
    submitter: Callable[..., Any]
    now: Callable[[], datetime]
    sleep: Callable[[float], None]


@dataclass(frozen=True)
class RunnerResult:
    status: str
    entry_gate_open: bool
    submitted: bool
    cycles_processed: int
    reasons: tuple[str, ...]


class RunnerAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.events = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate audit JSON key")
                    result[key] = value
                return result
            data = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicates,
            )
            if data.get("version") != AUDIT_VERSION or not isinstance(data.get("events"), list):
                raise ValueError("invalid audit state")
            return list(data["events"])
        except Exception as error:
            raise RuntimeError("Autonomous runner audit state is unreadable.") from error

    def record(self, event: str, at: datetime, **fields: Any) -> None:
        safe = {"event": event, "at": at.isoformat(), **fields}
        candidate = self.events + [safe]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": AUDIT_VERSION, "events": candidate}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Autonomous runner audit persistence failed.") from error
        self.events = candidate


class AutonomousPaperRunner:
    def __init__(
        self,
        paths: RunnerPaths,
        dependencies: RunnerDependencies,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        if isinstance(poll_seconds, bool) or not 1 <= float(poll_seconds) <= 300:
            raise ValueError("Polling interval must be between 1 and 300 seconds.")
        self.paths = paths
        self.deps = dependencies
        self.poll_seconds = float(poll_seconds)
        self.entry_gate_open = False

    def _configuration_proven(self) -> bool:
        config = self.deps.configuration
        return (
            config.get("ALPACA_PAPER") == "true"
            and config.get("CTS_AUTONOMOUS_PAPER_ENABLED") == "true"
        )

    def _strict_state_load(self, now: datetime) -> tuple[PaperRunnerLedger, SubmissionIntentJournal, PaperEntryOrderTracker]:
        ledger = PaperRunnerLedger(self.paths.ledger)
        journal = SubmissionIntentJournal(self.paths.journal, now=now)
        tracker = PaperEntryOrderTracker(self.paths.tracker)
        load_exit_monitor_health(self.paths.health)
        RunnerAuditLog(self.paths.audit)
        return ledger, journal, tracker

    def _recover(self, now: datetime, journal: SubmissionIntentJournal, tracker: PaperEntryOrderTracker) -> list[str]:
        reasons: list[str] = []
        for intent in journal.intents:
            if intent.status == "SUBMISSION_UNCERTAIN":
                result = reconcile_uncertain_submission(
                    journal_path=self.paths.journal,
                    tracker_path=self.paths.tracker,
                    client_order_id=intent.client_order_id,
                    lookup_by_client_order_id=self.deps.lookup_client_order,
                    now=now,
                )
                if not result.reconciled:
                    reasons.extend(result.reasons)
        tracker = PaperEntryOrderTracker(self.paths.tracker)
        nonterminal = [item for item in tracker.records if not item.terminal]
        if nonterminal:
            if len(nonterminal) != 1 or tracker.record is not nonterminal[0]:
                reasons.append("Nonterminal tracker state is ambiguous.")
            else:
                result = tracker.reconcile(self.deps.lookup_broker_order)
                if not result.reconciled:
                    reasons.append("Tracked nonterminal order reconciliation failed.")
        return reasons

    def _supervise(self, now: datetime, positions: list[Any] | None = None) -> Any:
        snapshot = list(self.deps.positions() if positions is None else positions)
        return supervise_exit_monitor(
            health_path=self.paths.health,
            journal_path=self.paths.journal,
            tracker_path=self.paths.tracker,
            retrieve_broker_positions=lambda: list(snapshot),
            run_monitor_cycle=self.deps.monitor_cycle,
            now=now,
        )

    def _preflight(self, broker: Any) -> Any:
        return self.deps.startup_preflight(
            config=self.deps.configuration,
            limits=TrialLimits(1, 1),
            broker_readiness=broker,
            state_path=self.paths.ledger,
            log_path=self.paths.audit,
        )

    def _final_revalidate(self, selected: Any, policy: Any, preview_created_at: datetime, lock_held: bool) -> tuple[Any, Any, Any, Any, datetime] | None:
        now = self.deps.now()
        if not lock_held or not self.entry_gate_open or not cts_entry_window_open(now):
            return None
        broker = self.deps.broker_readiness()
        preflight = self._preflight(broker)
        if preflight.status != "STARTUP_READY":
            return None
        journal = SubmissionIntentJournal(self.paths.journal, now=now)
        tracker = PaperEntryOrderTracker(self.paths.tracker)
        if any(item.status == "SUBMISSION_UNCERTAIN" for item in journal.intents):
            return None
        if any(not item.terminal for item in tracker.records):
            return None
        if not tracker.new_entry_allowed(now):
            return None
        positions = self.deps.positions()
        if positions:
            return None
        health = self._supervise(now, positions)
        if not health.ready:
            self.entry_gate_open = False
            return None
        synchronization = self.deps.synchronize_realized_pl(now)
        if getattr(synchronization, "success", None) is not True:
            self.entry_gate_open = False
            return None
        paper_state_ready, _ = self.deps.paper_state_health(now)
        if not paper_state_ready:
            self.entry_gate_open = False
            return None
        try:
            fresh_readiness, fresh_preview_at = self.deps.build_readiness(
                selected.scanner_result, now
            )
            fresh_policy = self.deps.policy(
                readiness=fresh_readiness, origin=CORE_ORIGIN,
                startup_preflight=preflight,
                soft_news_configuration=self.deps.configuration.get("CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW"),
                preview_created_at=fresh_preview_at, as_of=now,
            )
        except Exception:
            return None
        preview = selected.readiness.order_preview
        fresh_preview = fresh_readiness.order_preview
        if (
            selected.decision_id != decision_identity(selected.ticker, selected.scanner_result.bar_timestamp)
            or not 0 <= (now - preview_created_at).total_seconds() <= 300
            or selected.ticker != selected.scanner_result.ticker.strip().upper()
            or preview.estimated_cost > 150
            or getattr(policy, "allowed", None) is not True
            or getattr(fresh_policy, "allowed", None) is not True
            or tuple(
                getattr(fresh_preview, field, None)
                for field in (
                    "ticker", "contract_symbol", "side", "quantity", "order_type",
                    "time_in_force", "limit_price", "estimated_cost", "eligible",
                )
            ) != tuple(
                getattr(preview, field, None)
                for field in (
                    "ticker", "contract_symbol", "side", "quantity", "order_type",
                    "time_in_force", "limit_price", "estimated_cost", "eligible",
                )
            )
            or fresh_readiness.scanner_candidate.ticker.strip().upper() != selected.ticker
            or decision_identity(
                fresh_readiness.scanner_candidate.ticker,
                fresh_readiness.scanner_candidate.bar_timestamp,
            ) != selected.decision_id
        ):
            return None
        return preflight, health, fresh_readiness, fresh_policy, fresh_preview_at

    def run(self, mode: str, *, max_iterations: int | None = None) -> RunnerResult:
        if mode not in RUN_MODES:
            raise ValueError("Runner mode must be check, dry-run, or execute-paper.")
        if not self._configuration_proven():
            return RunnerResult("BLOCKED", False, False, 0, ("Explicit Alpaca/autonomous paper configuration is missing.",))
        cycles = 0
        submitted = False
        reasons: list[str] = []
        with SingleRunnerLock(self.paths.lock):
            now = self.deps.now()
            try:
                ledger, journal, tracker = self._strict_state_load(now)
            except Exception as error:
                return RunnerResult("BLOCKED", False, False, 0, (f"State validation failed ({type(error).__name__}).",))
            recovery_reasons = self._recover(now, journal, tracker)
            try:
                synchronization = self.deps.synchronize_realized_pl(now)
                if getattr(synchronization, "success", None) is not True:
                    recovery_reasons.append(
                        getattr(synchronization, "reason", None)
                        or "Managed paper realized-P/L synchronization failed."
                    )
            except Exception as error:
                recovery_reasons.append(
                    f"Managed paper realized-P/L synchronization failed ({type(error).__name__})."
                )
            try:
                paper_state_ready, paper_state_reason = self.deps.paper_state_health(now)
            except Exception as error:
                paper_state_ready = False
                paper_state_reason = f"Managed paper state validation failed ({type(error).__name__})."
            if not paper_state_ready:
                recovery_reasons.append(paper_state_reason)
            positions = self.deps.positions()
            audit = RunnerAuditLog(self.paths.audit)
            if mode == "check":
                broker = self.deps.broker_readiness()
                preflight = self._preflight(broker)
                diagnostic_reasons = list(recovery_reasons)
                if preflight.status != "STARTUP_READY":
                    diagnostic_reasons.extend(preflight.reasons)
                audit.record(
                    "CHECK", now, entry_gate_open=False,
                    position_count=len(positions), reasons=diagnostic_reasons,
                )
                return RunnerResult(
                    "CHECK_OK" if not diagnostic_reasons else "BLOCKED",
                    False, False, 0, tuple(diagnostic_reasons),
                )
            health = self._supervise(now, positions)
            broker = self.deps.broker_readiness()
            preflight = self._preflight(broker)
            self.entry_gate_open = not recovery_reasons and health.ready and preflight.status == "STARTUP_READY"
            audit.record("STARTUP", now, mode=mode, entry_gate_open=self.entry_gate_open, reasons=recovery_reasons + health.reasons + preflight.reasons)

            iterations = 0
            while max_iterations is None or iterations < max_iterations:
                iterations += 1
                clock = self.deps.clock()
                current = clock.timestamp
                health = self._supervise(current)
                if not health.ready:
                    self.entry_gate_open = False
                    reasons.extend(health.reasons)
                try:
                    synchronization = self.deps.synchronize_realized_pl(current)
                    paper_state_ready, paper_state_reason = self.deps.paper_state_health(current)
                    if getattr(synchronization, "success", None) is not True or not paper_state_ready:
                        self.entry_gate_open = False
                        reasons.append(
                            getattr(synchronization, "reason", None)
                            or paper_state_reason
                        )
                except Exception as error:
                    self.entry_gate_open = False
                    reasons.append(
                        f"Managed paper realized-P/L synchronization failed ({type(error).__name__})."
                    )
                if not clock.market_open or not cts_entry_window_open(current) or not self.entry_gate_open:
                    if current.astimezone(MARKET_TIMEZONE).time().replace(tzinfo=None) >= time(16, 0) and not self.deps.positions():
                        break
                    self.deps.sleep(self.poll_seconds)
                    continue
                interval = latest_completed_bar_start(current)
                try:
                    ledger.claim_cycle(interval, current)
                except RuntimeError:
                    self.deps.sleep(self.poll_seconds)
                    continue
                cycles += 1
                evaluations: list[CandidateEvaluation] = []
                preview_times: dict[str, datetime] = {}
                policy_by_id: dict[str, Any] = {}
                try:
                    tickers = self.deps.resolve_watchlist()
                    results, skipped = self.deps.scan(tickers=tickers, as_of=current)
                    audit.record("SCAN", current, interval=interval.isoformat(), skipped=skipped, ticker_count=len(tickers))
                    for scanner in results:
                        if not scanner.technical_candidate():
                            audit.record(
                                "NEAR_MISS", current, ticker=scanner.ticker,
                                technical_score=scanner.score(),
                                interval=scanner.bar_timestamp.isoformat(),
                            )
                            continue
                        decision_id = decision_identity(scanner.ticker, scanner.bar_timestamp)
                        ledger.claim_decision(decision_id, scanner.ticker, interval, current)
                        try:
                            readiness, preview_at = self.deps.build_readiness(scanner, current)
                            policy = self.deps.policy(
                                readiness=readiness, origin=CORE_ORIGIN,
                                startup_preflight=preflight,
                                soft_news_configuration=self.deps.configuration.get("CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW"),
                                preview_created_at=preview_at, as_of=current,
                            )
                            evaluation = CandidateEvaluation(CORE_ORIGIN, readiness.scanner_candidate, readiness, policy)
                            evaluations.append(evaluation)
                            preview_times[decision_id] = preview_at
                            policy_by_id[decision_id] = policy
                        except Exception as error:
                            ledger.complete_decision(decision_id, "ERROR", current)
                            audit.record("CANDIDATE_ERROR", current, ticker=scanner.ticker, decision_id=decision_id, reason=type(error).__name__)
                    selection = self.deps.selector(evaluations, current)
                    ranked_ids = {item.decision_id for item in selection.ranked_eligible}
                    claimed = [item for item in PaperRunnerLedger(self.paths.ledger).decisions if item.interval_start == interval.astimezone(timezone.utc).replace(microsecond=0).isoformat() and item.status == "CLAIMED"]
                    for item in claimed:
                        ledger.complete_decision(item.decision_id, "ELIGIBLE" if item.decision_id in ranked_ids else "EXCLUDED", current)
                    if selection.selected is None:
                        ledger.complete_cycle(interval, "NO_SELECTION", current)
                        audit.record("NO_SELECTION", current, interval=interval.isoformat(), exclusions=[asdict(item) for item in selection.exclusions])
                        self.deps.sleep(self.poll_seconds)
                        continue
                    selected = selection.selected
                    ledger.complete_cycle(interval, "SELECTED", current, selected.decision_id)
                    policy = policy_by_id[selected.decision_id]
                    label = "DRY_RUN_ONLY" if mode == "dry-run" else "PAPER_SELECTION"
                    audit.record(
                        label, current, ticker=selected.ticker,
                        decision_id=selected.decision_id,
                        soft_reasons=list(policy.reasons),
                        soft_headlines=list(getattr(policy, "audit_headlines", ())),
                        exclusions=[asdict(item) for item in selection.exclusions],
                    )
                    if mode == "dry-run":
                        self.deps.sleep(self.poll_seconds)
                        continue
                    proof = self._final_revalidate(selected, policy, preview_times[selected.decision_id], True)
                    if proof is None:
                        reasons.append("Final pre-submission revalidation failed.")
                        self.entry_gate_open = False
                        self.deps.sleep(self.poll_seconds)
                        continue
                    preflight, _, fresh_readiness, fresh_policy, fresh_preview_at = proof
                    result = self.deps.handoff(
                        readiness=fresh_readiness,
                        preview=fresh_readiness.order_preview,
                        preview_created_at=fresh_preview_at,
                        preflight=preflight,
                        execution_enable_value=self.deps.configuration.get("CTS_PAPER_EXECUTION_ENABLED", ""),
                        paper_configuration_confirmed=True,
                        origin=CORE_ORIGIN,
                        tracker=PaperEntryOrderTracker(self.paths.tracker),
                        journal=SubmissionIntentJournal(self.paths.journal, now=current),
                        submitter=self.deps.submitter,
                        now=current,
                        autonomous_policy=fresh_policy,
                        autonomous_startup_preflight=preflight,
                    )
                    submitted = result.submitted
                    audit.record("PAPER_SUBMISSION", current, ticker=selected.ticker, status=result.status, client_order_id=result.client_order_id)
                    self.entry_gate_open = False
                    break
                except Exception as error:
                    self.entry_gate_open = False
                    reasons.append(f"Cycle failed closed ({type(error).__name__}).")
                    try:
                        ledger.complete_cycle(interval, "ERROR", current)
                    except Exception:
                        pass
                    self.deps.sleep(self.poll_seconds)
                    continue
        self.entry_gate_open = False
        return RunnerResult("SUBMITTED" if submitted else "COMPLETED", False, submitted, cycles, tuple(reasons))
