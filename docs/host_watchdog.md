# Host Watchdog (`scripts/host_watchdog.sh`)

An **external, alert-only** cron watchdog for the Money Printer trading system.
It replaces the dead autonomous monitoring (Hermes) with a dependency-free bash
script that runs from the **host crontab** every 5 minutes. No LLM, no API
credits, no Python — just `pgrep`, `stat`, and a Discord webhook POST.

## Why it exists

Hermes-based monitoring is off (out of credits). This script is the cheap,
always-on safety net: if the trading process dies or the loop quietly stalls, you
get a Discord ping instead of silently losing hours of paper-trading.

It is intentionally **alert-only** — it does *not* restart anything. Restart
logic already lives in `scripts/watchdog_cron.sh` (tmux + health endpoint).
`host_watchdog.sh` is a lighter, independent layer that keeps working even if the
dashboard's HTTP health endpoint or tmux machinery is broken.

## How it works

Every run performs **two liveness checks** and alerts on **either** failure:

1. **Process check** — `pgrep -f run_web_dashboard` must find a live process.
2. **Log-freshness check** — the newest `logs/session_*.log` must have an mtime
   younger than `STALE_MAX_AGE` (**2400s = 40 min**).

The 40-minute staleness margin is deliberate **defense-in-depth**. The
orchestrator runs a periodic retrain roughly every 30 minutes. After the
orchestrator fix the retrain no longer freezes the tick loop, but the generous
margin guarantees the watchdog never false-positives during a retrain window
(40 min > 35 min > ~30 min).

On failure the script POSTs a clear message to `DISCORD_WEBHOOK_URL`, **throttled
to at most once per `ALERT_COOLDOWN` (1800s = 30 min)** via the timestamp file
`logs/host_watchdog_last_alert.ts`. This mirrors `watchdog_cron.sh` so the
channel is never spammed. When healthy, the script exits `0` silently (it prints
nothing on the happy path).

### Configuration (top of the script)

| Variable          | Default | Meaning                                              |
|-------------------|---------|------------------------------------------------------|
| `PROCESS_PATTERN` | `run_web_dashboard` | `pgrep -f` pattern for the trading process |
| `STALE_MAX_AGE`   | `2400`  | Max age (s) of newest session log before alerting    |
| `ALERT_COOLDOWN`  | `1800`  | Min seconds between Discord alerts (throttle)        |
| `CURL_TIMEOUT`    | `10`    | `curl --max-time` for the Discord POST               |

### Environment

`DISCORD_WEBHOOK_URL` is read from the environment, or sourced from `~/.env`
(falling back to `~/money_printer/.env`). Missing `.env` files are tolerated
silently — if the webhook is unset, the script logs a skip notice and exits
without crashing.

## How to install (on the GCE VM, Ubuntu)

```bash
# 1. Make it executable
chmod +x ~/money_printer/scripts/host_watchdog.sh

# 2. Smoke-test it (never alerts in --check-only; exit 0=healthy, 1=unhealthy)
~/money_printer/scripts/host_watchdog.sh --check-only; echo "exit=$?"

# 3. Install the cron entry (replace USER with your VM username, e.g. via $USER)
crontab -e
```

Add this line (the install one-liner):

```cron
*/5 * * * * /home/USER/money_printer/scripts/host_watchdog.sh >> /home/USER/money_printer/logs/host_watchdog.log 2>&1
```

Or install it non-interactively in one shot:

```bash
( crontab -l 2>/dev/null; \
  echo "*/5 * * * * $HOME/money_printer/scripts/host_watchdog.sh >> $HOME/money_printer/logs/host_watchdog.log 2>&1" ) \
  | sort -u | crontab -
```

> Cron runs with a minimal environment, so `DISCORD_WEBHOOK_URL` must live in
> `~/.env` (or `~/money_printer/.env`). The script sources it on each run.

## Usage

```bash
# Real check — may POST a (throttled) Discord alert on failure
~/money_printer/scripts/host_watchdog.sh

# Check only — runs both checks, never alerts, exit 0=OK / 1=failing
~/money_printer/scripts/host_watchdog.sh --check-only

# Help
~/money_printer/scripts/host_watchdog.sh --help
```

## Pause / resume (maintenance)

```bash
touch ~/money_printer/.host_watchdog_disabled   # pause: checks skipped, stays silent
rm    ~/money_printer/.host_watchdog_disabled    # resume
```

## Files it touches

| Path                                   | Purpose                                  |
|----------------------------------------|------------------------------------------|
| `logs/host_watchdog.log`               | cron stdout/stderr (timestamps, results) |
| `logs/host_watchdog_last_alert.ts`     | epoch of last Discord alert (throttle)   |
| `.host_watchdog_disabled`              | maintenance flag (pause checks)          |

## Relationship to the other watchdogs

- **`scripts/host_watchdog.sh`** (this) — external host cron, alert-only, no deps.
- **`scripts/watchdog_cron.sh`** — in-tree, hits the HTTP health endpoint and
  *restarts* the tmux session on failure.
- **`scripts/vm_watchdog.py`** — heavier AI-assisted deep-fix watchdog (was driven
  by Hermes; currently off).

Run this one regardless — it has the fewest moving parts and is the most likely
to survive whatever broke the rest.
