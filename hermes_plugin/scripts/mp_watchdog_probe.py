#!/usr/bin/env python3
"""Hermes cron probe for the Money Printer dashboard.

Invokes scripts/watchdog_cron.sh (the lightweight bash watchdog) and emits its
output + exit code for Hermes to reason about. The bash script is self-contained:
it does the health check, restart, and Discord alert. This probe exists so Hermes
owns the schedule (via `hermes cron create`) and keeps a run history alongside
other scheduled tasks.

Deploy: copy this file to ~/.hermes/scripts/mp_watchdog_probe.py on the VM
(Hermes requires scripts to live under HERMES_HOME/scripts/ for sandboxing).
"""
import subprocess
import sys
import time

SCRIPT = "/home/hoyer/money_printer/scripts/watchdog_cron.sh"
TIMEOUT = 90  # seconds — bash worst-case is ~60s (health + restart + recheck); Hermes cap is 120s

print(f"=== Money Printer probe @ {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===")

try:
    result = subprocess.run(
        [SCRIPT],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    stderr = result.stderr.strip()
    if stderr:
        print(f"--- stderr ---\n{stderr}")
    print(f"=== exit_code: {result.returncode} ===")
except subprocess.TimeoutExpired:
    print(f"=== TIMEOUT: watchdog exceeded {TIMEOUT}s ===")
    sys.exit(1)
except FileNotFoundError:
    print(f"=== ERROR: watchdog not found at {SCRIPT} ===")
    sys.exit(2)

sys.exit(0)
