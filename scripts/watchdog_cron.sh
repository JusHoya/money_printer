#!/usr/bin/env bash
# watchdog_cron.sh — Lightweight bash watchdog for Money Printer trading system.
# Pure bash, no LLM, designed to run every 5 minutes via cron.
# Complementary to scripts/vm_watchdog.py (which does AI-assisted deep fixes)
# and scripts/host_watchdog.sh (alert-only last line of defense).
#
# Phase 0 hardening (FR-0.5, 2026-07-24):
#   * If the health endpoint fails but the process is ALIVE, do not kill it on
#     a single failed probe (a busy cycle transition or slow request can time
#     out the 10s curl). The failure must persist across two consecutive cron
#     runs (a confirmation marker aged 240–1800 s) before a restart.
#   * If the process is DEAD, restart immediately on the first failing check.
#   * Discord reporting is per-incident: the whole restart sequence (initial
#     alert + outcome) counts as ONE incident against ALERT_COOLDOWN. This
#     fixes the old bug where writing the cooldown timestamp on the initial
#     "attempting restart" message suppressed the far more important
#     "restart FAILED — manual intervention" outcome message seconds later.
#   * No dependence on the retired retrain marker (logs/.orchestrator_state);
#     runtime retrains were removed in Phase 0 (FR-0.2).
#
# Deploy (on VM):
#   chmod +x ~/money_printer/scripts/watchdog_cron.sh
#   crontab -e
#   Add: */5 * * * * /home/USER/money_printer/scripts/watchdog_cron.sh >> /home/USER/money_printer/logs/watchdog.log 2>&1
# Test manually:
#   ~/money_printer/scripts/watchdog_cron.sh --check-only
# Maintenance mode (pause watchdog):
#   touch ~/money_printer/.watchdog_disabled
# Resume:
#   rm ~/money_printer/.watchdog_disabled

set -uo pipefail

# ---------------------------------------------------------------------------
# Constants (env-overridable for tests / ops tuning)
# ---------------------------------------------------------------------------
HEALTH_URL="http://localhost:8050/api/status"
TMUX_SESSION="money"
PROJECT_DIR="${WATCHDOG_PROJECT_DIR:-$HOME/money_printer}"
STATE_DIR="$PROJECT_DIR/logs"
LAST_ALERT_FILE="$STATE_DIR/watchdog_last_alert.ts"
FAIL_MARKER="$STATE_DIR/watchdog_cron_endpoint_fail.ts"
ALERT_COOLDOWN="${WATCHDOG_ALERT_COOLDOWN:-1800}"   # seconds between reported incidents
HEALTH_TIMEOUT=10                                   # curl max-time seconds
RESTART_STARTUP_WAIT="${WATCHDOG_RESTART_STARTUP_WAIT:-30}"  # seconds after tmux start
PROCESS_PATTERN="run_web_dashboard"
# Endpoint-failure confirmation window: the marker must come from a previous
# cron run (>= MIN) but not from an ancient forgotten incident (<= MAX).
CONFIRM_MIN_AGE="${WATCHDOG_CONFIRM_MIN_AGE:-240}"
CONFIRM_MAX_AGE="${WATCHDOG_CONFIRM_MAX_AGE:-1800}"

# 2026-06-10 fix (b): never hardcode the sim balance on auto-restart — that
# would silently override a future Phase-2 $500 run. Reuse the balance the
# process was launched with, written by run_dashboard.py to this marker file.
# Precedence: $MP_SIM_BALANCE env var > marker file > safe default 3000.
SIM_BALANCE_MARKER="$STATE_DIR/.sim_balance"
resolve_sim_balance() {
    local bal="${MP_SIM_BALANCE:-}"
    if [[ -z "$bal" && -r "$SIM_BALANCE_MARKER" ]]; then
        bal=$(head -n1 "$SIM_BALANCE_MARKER" 2>/dev/null | tr -dc '0-9.')
    fi
    # Validate: must be a positive number; otherwise fall back to 3000.
    if [[ -z "$bal" || ! "$bal" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        bal=3000
    fi
    printf '%s' "$bal"
}
SIM_BALANCE="$(resolve_sim_balance)"

START_CMD="cd $PROJECT_DIR && source $PROJECT_DIR/venv/bin/activate && PYTHONPATH=. python3 scripts/run_web_dashboard.py --auto-cycle --sim-balance $SIM_BALANCE --host 0.0.0.0 --port 8050 --no-browser"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    local ts
    ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    echo "[$ts] $*"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Lightweight watchdog for the Money Printer trading dashboard.
Dead process: restarts immediately. Endpoint down but process alive: restarts
only after the failure persists across two consecutive runs.

Options:
  --check-only   Health check only; no restarts, no alerts. Exit 0=OK, 1=failing.
  --help         Show this help and exit.

EOF
}

# Load .env if present (for DISCORD_WEBHOOK_URL)
load_env() {
    local env_file="$PROJECT_DIR/.env"
    if [[ -f "$env_file" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a
    fi
}

# Returns 0 if health endpoint responds 200, else 1
check_health() {
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$HEALTH_TIMEOUT" "$HEALTH_URL" 2>/dev/null || true)
    if [[ "$code" == "200" ]]; then
        return 0
    else
        log "Health check returned HTTP $code (expected 200)"
        return 1
    fi
}

# Returns 0 if a run_web_dashboard.py process is alive, else 1.
check_process() {
    pgrep -f "$PROCESS_PATTERN" >/dev/null 2>&1
}

# Read a unix timestamp from a state file; echoes 0 if absent/corrupt.
read_ts() {
    local ts=""
    [[ -r "$1" ]] && ts=$(head -n1 "$1" 2>/dev/null)
    [[ "$ts" =~ ^[0-9]+$ ]] || ts=0
    printf '%s' "$ts"
}

# Decide once per restart sequence whether this incident may report to
# Discord. One incident = initial alert + outcome message; both share the
# decision so an important outcome is never suppressed by its own alert.
INCIDENT_REPORT=true
begin_incident() {
    local now last elapsed
    now=$(date +%s)
    last=$(read_ts "$LAST_ALERT_FILE")
    elapsed=$(( now - last ))
    if (( last > 0 && elapsed < ALERT_COOLDOWN )); then
        INCIDENT_REPORT=false
        log "Incident within cooldown ($(( ALERT_COOLDOWN - elapsed ))s remaining) — restart proceeds, Discord silent"
    else
        INCIDENT_REPORT=true
        mkdir -p "$STATE_DIR"
        echo "$now" > "$LAST_ALERT_FILE"
    fi
}

# Send a Discord message for the current incident (delivery only; the
# per-incident throttle is decided in begin_incident).
# Args: $1 = message text
send_discord() {
    local message="$1"

    if ! $INCIDENT_REPORT; then
        log "Discord message suppressed (incident cooldown): $message"
        return 0
    fi

    if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
        log "DISCORD_WEBHOOK_URL not set — skipping Discord alert: $message"
        return 0
    fi

    local payload http_code
    payload=$(printf '{"content": "%s"}' "$message")
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 10 \
        "$DISCORD_WEBHOOK_URL" 2>/dev/null || true)

    if [[ "$http_code" == "204" || "$http_code" == "200" ]]; then
        log "Discord alert sent (HTTP $http_code): $message"
    else
        log "Discord alert failed (HTTP $http_code): $message"
    fi
}

# Kill any stale dashboard process gracefully, then forcefully if needed.
kill_stale_process() {
    if pkill -TERM -f "run_web_dashboard.py" 2>/dev/null; then
        log "Sent SIGTERM to run_web_dashboard.py processes; waiting 2s..."
        sleep 2
        # Force-kill any survivors
        if pkill -KILL -f "run_web_dashboard.py" 2>/dev/null; then
            log "Sent SIGKILL to remaining run_web_dashboard.py processes"
        fi
    else
        log "No run_web_dashboard.py processes found to kill"
    fi
}

# Kill existing tmux session if it exists
kill_tmux_session() {
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        log "Killing tmux session '$TMUX_SESSION'"
        tmux kill-session -t "$TMUX_SESSION" || true
    else
        log "No tmux session '$TMUX_SESSION' found"
    fi
}

# Start a fresh tmux session with the dashboard command
start_tmux_session() {
    log "Starting tmux session '$TMUX_SESSION'..."
    tmux new-session -d -s "$TMUX_SESSION" -c "$PROJECT_DIR" "$START_CMD"
    log "tmux session '$TMUX_SESSION' started; waiting ${RESTART_STARTUP_WAIT}s for startup..."
    sleep "$RESTART_STARTUP_WAIT"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
CHECK_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --check-only)
            CHECK_ONLY=true
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            log "Unknown argument: $arg"
            usage
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
mkdir -p "$STATE_DIR"
load_env

log "--- watchdog_cron.sh start (check-only=$CHECK_ONLY) ---"

# --check-only: health check only, no side effects
if $CHECK_ONLY; then
    if check_health; then
        log "Health check: OK"
        exit 0
    else
        log "Health check: FAILING"
        exit 1
    fi
fi

# Normal mode — check health first
if check_health; then
    rm -f "$FAIL_MARKER"
    log "Health check: OK — no action needed"
    exit 0
fi

# Unhealthy path

# Maintenance / disable flag
if [[ -f "$PROJECT_DIR/.watchdog_disabled" ]]; then
    log "Watchdog disabled (.watchdog_disabled exists) — skipping restart"
    exit 0
fi

NOW=$(date +%s)

if check_process; then
    # Endpoint down but the process is alive — could be a busy cycle
    # transition or one slow request. Require the failure to persist across
    # two consecutive cron runs before killing a live process.
    marker_ts=$(read_ts "$FAIL_MARKER")
    marker_age=$(( NOW - marker_ts ))
    if (( marker_ts > 0 && marker_age >= CONFIRM_MIN_AGE && marker_age <= CONFIRM_MAX_AGE )); then
        log "Endpoint failure CONFIRMED across consecutive checks (marker ${marker_age}s old) — restarting"
    else
        echo "$NOW" > "$FAIL_MARKER"
        log "Endpoint failing but process alive — confirmation armed; restart only if still failing next run"
        exit 1
    fi
else
    log "Process '$PROCESS_PATTERN' not running — restarting immediately"
fi

rm -f "$FAIL_MARKER"

log "Health check FAILED — initiating restart sequence"

begin_incident
send_discord "🔴 Money Printer health check failed — attempting restart"

# Kill stale process and tmux session
kill_stale_process
kill_tmux_session

# Start fresh
start_tmux_session

# Verify recovery
if check_health; then
    log "Restart succeeded — dashboard is healthy"
    send_discord "✅ Money Printer restart succeeded"
    exit 0
else
    log "Restart FAILED — dashboard still not responding after ${RESTART_STARTUP_WAIT}s"
    send_discord "🚨 Money Printer restart FAILED — manual intervention needed"
    exit 2
fi
