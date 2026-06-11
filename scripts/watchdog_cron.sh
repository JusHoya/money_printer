#!/usr/bin/env bash
# watchdog_cron.sh — Lightweight bash watchdog for Money Printer trading system.
# Pure bash, no LLM, designed to run every 5 minutes via cron.
# Complementary to scripts/vm_watchdog.py (which does AI-assisted deep fixes).
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
# Constants
# ---------------------------------------------------------------------------
HEALTH_URL="http://localhost:8050/api/status"
TMUX_SESSION="money"
PROJECT_DIR="$HOME/money_printer"
STATE_DIR="$PROJECT_DIR/logs"
LAST_ALERT_FILE="$STATE_DIR/watchdog_last_alert.ts"
ALERT_COOLDOWN=1800       # seconds between Discord alerts
HEALTH_TIMEOUT=10         # curl max-time seconds
RESTART_STARTUP_WAIT=30   # seconds to wait after tmux session start

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

# Send a Discord message, respecting the cooldown.
# Args: $1 = message text
# When within cooldown: logs a skip notice and returns without sending.
send_discord() {
    local message="$1"
    local now
    now=$(date +%s)

    # Cooldown check
    if [[ -f "$LAST_ALERT_FILE" ]]; then
        local last_ts
        last_ts=$(cat "$LAST_ALERT_FILE" 2>/dev/null || echo 0)
        local elapsed=$(( now - last_ts ))
        if (( elapsed < ALERT_COOLDOWN )); then
            local remaining=$(( ALERT_COOLDOWN - elapsed ))
            log "Discord alert suppressed (cooldown: ${remaining}s remaining): $message"
            return 0
        fi
    fi

    if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
        log "DISCORD_WEBHOOK_URL not set — skipping Discord alert: $message"
        return 0
    fi

    # Ensure state dir exists
    mkdir -p "$STATE_DIR"

    local payload
    payload=$(printf '{"content": "%s"}' "$message")
    local http_code
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 10 \
        "$DISCORD_WEBHOOK_URL" 2>/dev/null || true)

    if [[ "$http_code" == "204" || "$http_code" == "200" ]]; then
        log "Discord alert sent (HTTP $http_code): $message"
        echo "$now" > "$LAST_ALERT_FILE"
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
    log "Health check: OK — no action needed"
    exit 0
fi

# Unhealthy path

# Maintenance / disable flag
if [[ -f "$PROJECT_DIR/.watchdog_disabled" ]]; then
    log "Watchdog disabled (.watchdog_disabled exists) — skipping restart"
    exit 0
fi

log "Health check FAILED — initiating restart sequence"

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
