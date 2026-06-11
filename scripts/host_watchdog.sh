#!/usr/bin/env bash
# host_watchdog.sh — External host-cron watchdog for Money Printer (no Hermes / no LLM).
#
# Purpose:
#   Replaces the dead autonomous monitoring with a dependency-free bash check that
#   runs every 5 minutes from the *host* crontab. It does NOT restart anything — it
#   only ALERTS via Discord when the trading process is dead or the session log has
#   gone stale. (Restart logic lives in scripts/watchdog_cron.sh; this is a lighter,
#   alert-only safety net that survives even if tmux/the dashboard machinery breaks.)
#
# Two liveness checks (alert on EITHER failure):
#   1. The run_web_dashboard.py process is alive   (pgrep -f run_web_dashboard).
#   2. The newest logs/session_*.log was written < STALE_MAX_AGE seconds ago.
#      The orchestrator runs ~30-min retrains (startup, periodic, and cycle
#      reset) during which the session log legitimately stops advancing. To
#      avoid false alerts the orchestrator writes a "retraining" state marker
#      (logs/.orchestrator_state); while that marker is fresh we widen the
#      staleness margin to RETRAIN_STALE_MAX_AGE. (2026-06-10 fix.)
#
# Alerts are throttled to at most once per ALERT_COOLDOWN seconds via a timestamp
# file, mirroring scripts/watchdog_cron.sh so we never spam the channel.
#
# Deploy (on the GCE VM, Ubuntu):
#   chmod +x ~/money_printer/scripts/host_watchdog.sh
#   crontab -e
#   Add:
#     */5 * * * * /home/USER/money_printer/scripts/host_watchdog.sh >> /home/USER/money_printer/logs/host_watchdog.log 2>&1
#
# Test manually:
#   ~/money_printer/scripts/host_watchdog.sh            # run a real check (may alert)
#   ~/money_printer/scripts/host_watchdog.sh --check-only  # check only, never alerts; exit 0=OK 1=fail
#
# Pause (maintenance):
#   touch ~/money_printer/.host_watchdog_disabled
# Resume:
#   rm ~/money_printer/.host_watchdog_disabled

set -uo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROCESS_PATTERN="run_web_dashboard"   # matched against full command line via pgrep -f
PROJECT_DIR="$HOME/money_printer"
LOG_DIR="$PROJECT_DIR/logs"
STATE_DIR="$LOG_DIR"
LAST_ALERT_FILE="$STATE_DIR/host_watchdog_last_alert.ts"
DISABLE_FLAG="$PROJECT_DIR/.host_watchdog_disabled"
# 2026-06-10 fix (c): orchestrator writes "running"/"retraining" + a unix ts to
# this marker. During a retrain the session log legitimately stops advancing for
# ~30 min; we widen the staleness margin then so we don't false-alert.
STATE_MARKER="$LOG_DIR/.orchestrator_state"

STALE_MAX_AGE=2400        # 40 min — newest session log must be fresher than this
RETRAIN_STALE_MAX_AGE=3000  # 50 min — widened margin while a retrain is in flight
STATE_MARKER_MAX_AGE=2700   # 45 min — ignore a "retraining" marker older than this
ALERT_COOLDOWN=1800       # 30 min — minimum seconds between Discord alerts
CURL_TIMEOUT=10           # curl max-time seconds for the Discord POST

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

External host-cron watchdog for the Money Printer trading dashboard.
Alert-only: checks process liveness + session-log freshness and pings Discord.

Options:
  --check-only   Run checks only; never alerts. Exit 0=healthy, 1=unhealthy.
  --help         Show this help and exit.

EOF
}

# Load ~/.env (or PROJECT_DIR/.env) if present, for DISCORD_WEBHOOK_URL.
# Missing files are tolerated silently — the env var may already be exported.
load_env() {
    local env_file
    for env_file in "$HOME/.env" "$PROJECT_DIR/.env"; do
        if [[ -f "$env_file" ]]; then
            set -a
            # shellcheck disable=SC1090
            source "$env_file" 2>/dev/null || log "Warning: failed to source $env_file"
            set +a
        fi
    done
}

# Returns 0 if a run_web_dashboard.py process is alive, else 1.
check_process() {
    if pgrep -f "$PROCESS_PATTERN" >/dev/null 2>&1; then
        return 0
    fi
    log "Process check: no process matching '$PROCESS_PATTERN' found"
    return 1
}

# Returns 0 if the orchestrator state marker says it is currently retraining
# (and the marker is recent enough to trust), else 1. A normal retrain freezes
# the session log for ~30 min, so we widen the staleness margin while it lasts.
is_retraining() {
    [[ -r "$STATE_MARKER" ]] || return 1
    local content state marker_ts now marker_age
    content=$(head -n1 "$STATE_MARKER" 2>/dev/null || echo "")
    state=$(printf '%s' "$content" | awk '{print $1}')
    marker_ts=$(printf '%s' "$content" | awk '{print $2}')
    [[ "$state" == "retraining" ]] || return 1
    # Guard against a stuck marker (crash mid-retrain): only honour it if recent.
    [[ "$marker_ts" =~ ^[0-9]+$ ]] || return 1
    now=$(date +%s)
    marker_age=$(( now - marker_ts ))
    (( marker_age < STATE_MARKER_MAX_AGE ))
}

# Returns 0 if the newest logs/session_*.log is fresher than the effective max
# age (widened during a retrain), else 1.
check_log_freshness() {
    local newest=""
    local f
    # Find the most recently modified session log without relying on `find -printf`
    # (BSD/macOS compat) — plain glob + stat is portable enough for Ubuntu.
    for f in "$LOG_DIR"/session_*.log; do
        [[ -e "$f" ]] || continue
        if [[ -z "$newest" || "$f" -nt "$newest" ]]; then
            newest="$f"
        fi
    done

    if [[ -z "$newest" ]]; then
        log "Log freshness check: no session_*.log files found in $LOG_DIR"
        return 1
    fi

    # 2026-06-10 fix (c): widen the margin while the orchestrator is retraining
    # so a normal ~30-min retrain freeze does NOT trigger a false alert.
    local max_age=$STALE_MAX_AGE
    if is_retraining; then
        max_age=$RETRAIN_STALE_MAX_AGE
        log "Log freshness check: orchestrator retraining — widening margin to ${max_age}s"
    fi

    local mtime now age
    mtime=$(stat -c %Y "$newest" 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$(( now - mtime ))

    if (( age < max_age )); then
        return 0
    fi

    log "Log freshness check: newest log '$newest' is ${age}s old (max ${max_age}s)"
    return 1
}

# Send a Discord message, respecting the cooldown.
# Args: $1 = message text. Within cooldown: logs a skip and returns 0.
send_discord() {
    local message="$1"
    local now
    now=$(date +%s)

    if [[ -f "$LAST_ALERT_FILE" ]]; then
        local last_ts elapsed
        last_ts=$(cat "$LAST_ALERT_FILE" 2>/dev/null || echo 0)
        # Guard against a corrupt/empty timestamp file
        [[ "$last_ts" =~ ^[0-9]+$ ]] || last_ts=0
        elapsed=$(( now - last_ts ))
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

    mkdir -p "$STATE_DIR"

    local payload http_code
    payload=$(printf '{"content": "%s"}' "$message")
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time "$CURL_TIMEOUT" \
        "$DISCORD_WEBHOOK_URL" 2>/dev/null || true)

    if [[ "$http_code" == "204" || "$http_code" == "200" ]]; then
        log "Discord alert sent (HTTP $http_code): $message"
        echo "$now" > "$LAST_ALERT_FILE"
    else
        log "Discord alert failed (HTTP $http_code): $message"
    fi
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

# --check-only: report health, never alert, no side effects.
if $CHECK_ONLY; then
    proc_ok=true
    log_ok=true
    check_process     || proc_ok=false
    check_log_freshness || log_ok=false
    if $proc_ok && $log_ok; then
        log "Health check: OK (process alive, logs fresh)"
        exit 0
    else
        log "Health check: FAILING (process_ok=$proc_ok log_ok=$log_ok)"
        exit 1
    fi
fi

# Maintenance / disable flag — stay silent.
if [[ -f "$DISABLE_FLAG" ]]; then
    log "Watchdog disabled ($DISABLE_FLAG exists) — skipping checks"
    exit 0
fi

# Run both checks; collect failure reasons.
problems=""
if ! check_process; then
    problems="${problems}process \`run_web_dashboard.py\` is not running; "
fi
if ! check_log_freshness; then
    problems="${problems}newest session log is stale (>${STALE_MAX_AGE}s / $(( STALE_MAX_AGE / 60 ))min); "
fi

if [[ -z "$problems" ]]; then
    # Healthy: exit 0 silently (the log line above only fires on failures).
    exit 0
fi

# Unhealthy — alert (throttled).
local_host=$(hostname 2>/dev/null || echo "unknown-host")
log "UNHEALTHY: $problems"
send_discord "🚨 Money Printer host-watchdog [$local_host]: $problems"
exit 1
