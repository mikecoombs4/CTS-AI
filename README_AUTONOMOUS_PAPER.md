# CTS Autonomous Paper Runner

This runner supports Alpaca paper trading only. It has no live or real-money mode.

## Required configuration names

Configure these names in the project `.env` file. Do not paste their values into logs or support messages.

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_PAPER
CTS_AUTONOMOUS_PAPER_ENABLED
CTS_PAPER_EXECUTION_ENABLED
CTS_PAPER_ALLOW_SOFT_NEWS_REVIEW
CTS_TRIAL_MAX_TRADES_PER_DAY
CTS_TRIAL_MAX_OPEN_POSITIONS
FINNHUB_API_KEY
```

The trial limits must be one trade and one open position. The autonomous runner never changes any setting itself.

## Safe commands

From `/Users/michaelcoombs/CTS-AI`, first run the diagnostic:

```bash
python3 run_autonomous_paper.py --check
```

Run the real read-only candidate pipeline without submitting an entry:

```bash
python3 run_autonomous_paper.py --dry-run
```

Every selected candidate in this mode is recorded as `DRY_RUN_ONLY`.

Only after `--check` succeeds and the Alpaca account is visibly the paper account, run:

```bash
python3 run_autonomous_paper.py --execute-paper
```

**PAPER ONLY:** this mode can submit one supervised paper entry. There is no live command.

Startup reports the selected mode, `STATUS`, and whether the in-memory `ENTRY_GATE` is open. Healthy startup requires successful recovery, a fresh exit-monitor cycle, exact paper configuration, and validated state.

Stop the runner with Control-C. It releases the single-runner lock before exiting. Never run two copies at once.

## State and logs

On macOS, files are under:

```text
~/Library/Application Support/CTS-AI/autonomous-paper/
```

Important files include:

```text
autonomous_runner.lock
autonomous_runner_state.json
autonomous_runner_audit.json
submission_intents.json
paper_entry_orders.json
exit_monitor_health.json
exit_monitor_state.json
exit_monitor.log
../paper_state.json
```

- `BLOCKED` means a required proof failed. Do not trade around it.
- `DRY_RUN_ONLY` is a recorded selection that was never submitted.
- `SUBMISSION_UNCERTAIN` means the runner will reconcile by client order ID and will not retry.
- Healthy exit monitoring requires a fresh successful heartbeat and exact agreement between tracked and broker paper positions.

Never delete or edit state files to bypass a block. Preserve them for recovery and audit. Investigate the reported category, correct the underlying paper configuration or provider problem, and rerun `--check`.

Before entry readiness can pass, managed `paper_state.json` must contain broker-reconciled, same-day realized P/L verified within five minutes. The runner reads complete Alpaca PAPER order history, reconciles filled BUYs to CORE_CTS intent and tracker identities, applies only matched closed SELL quantity, and excludes unrealized P/L. Unknown activity, incomplete history, malformed fills, or ambiguous fees blocks new entries without stopping exit supervision. The diagnostic and dry-run modes may perform this read-only synchronization; no account-equity or portfolio-history P/L is used.

After testing, stop the runner and disable the autonomous and paper-execution switches in `.env`. The runner cannot disable them for you. There is intentionally no real-money setup or live-trading procedure.
