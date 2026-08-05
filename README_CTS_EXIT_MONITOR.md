# CTS-AI Automatic Paper Exit Monitor

This update adds automatic exit management to the Alpaca paper account. It
does not enable live trading or automatic entries.

## Exit rules

- Stop loss: close at a 25% option-premium loss.
- Trailing activation: begins after the position reaches a 20% gain.
- Trailing distance: close after a 10% pullback from the highest option price
  observed by the monitor after it starts.
- Profit target: close at a 35% gain.
- New-entry window for the future entry engine: 9:45 AM through 3:29:59 PM ET.
- At 3:55 PM ET on weekdays: cancel every working paper order, then close every
  open option position whose contract expires that day.
- If a forced close has not filled, keep checking and retry when necessary
  until the 0DTE position is gone.

## Files in this update

- `main.py` - adds menu option 5 for the paper exit monitor.
- `alpaca_service.py` - exposes a paper-only trading client.
- `scanner_service.py` - existing scanner included for a complete update.
- `exit_monitor.py` - automatic exit engine.
- `test_exit_monitor.py` - automated safety tests.
- `requirements.txt` - required Python packages.

The monitor stores restart-safe progress in `cts_exit_state.json` and activity
in `cts_exit_monitor.log`. Those two files are created automatically.

## Install the update on the Mac

1. Extract the ZIP file.
2. Copy its files into `/Users/michaelcoombs/CTS-AI` and choose **Replace** when
   macOS asks about files with the same names.
3. Do not delete or replace the existing `.env` file. It contains the Alpaca
   paper credentials and is intentionally not included in the ZIP.
4. Open Terminal and run:

```bash
cd /Users/michaelcoombs/CTS-AI
python3 -m pip install -r requirements.txt
python3 -m unittest -v test_exit_monitor.py
```

All tests should report `OK`.

## Start the monitor

Run it directly for unattended paper exit protection:

```bash
cd /Users/michaelcoombs/CTS-AI
python3 exit_monitor.py
```

Or run `python3 main.py` and choose option 5.

Keep the Mac powered on, awake, connected to the internet, and leave the
monitor running. If the Mac is asleep, shut down, disconnected, or the program
is stopped, CTS-AI cannot monitor or close positions.

Press **Control+C** to stop the monitor safely.
