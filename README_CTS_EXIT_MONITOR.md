# CTS-AI Automatic Paper Exit Monitor

This corrected update is based on GitHub commit `43c0733` (`Add crash recovery
state`). It preserves the existing scanner, options, news, earnings, risk,
daily-limits, paper-execution, and crash-recovery services.

It does not enable live trading or automatic entries.

## Exit rules

- Initial stop: close at a 25% option-premium loss.
- Trailing activation: begins after the position reaches a 20% gain.
- Trailing distance: close after a 10% pullback from the highest option price
  observed by the monitor.
- Profit target: close at a 35% gain.
- The existing CTS market-session service blocks new entries after 3:30 PM ET.
- At 3:55 PM ET on weekdays: cancel working entry and 0DTE orders, then close
  every open option position whose contract expires that day. Unrelated
  later-dated exit orders are left alone.
- Keep checking pending exits and retry when necessary until each 0DTE position
  is gone.

The monitor stores restart-safe progress and logs under the Mac user's
`Library/Application Support/CTS-AI` directory.

## Verification

From `/Users/michaelcoombs/CTS-AI`, run:

```bash
python3 -m unittest discover -v
```

The corrected project passes 82 automated tests.

## Start the monitor

Run it directly:

```bash
python3 exit_monitor.py
```

Or run `python3 main.py` and choose option 11.

Keep the Mac powered on, awake, connected to the internet, and leave the
monitor running. Press **Control+C** to stop it safely.
