#!/usr/bin/env bash
# host_watchdog.sh — External host-cron watchdog for Money Printer (no Hermes / no LLM).
#
# Purpose:
#   Dependency-free bash safety net that runs every 5 minutes from the *host*
#   crontab. It is ALERT-ONLY: it never restarts anything (restart logic lives
#   in scripts/watchdog_cron.sh) and it survives even if tmux, the venv, or
#   the dashboard machinery breaks.
#
# Phase 0 redesign (FR-0.5, 2026-07-24) — replaces the logic that produced
# ~10 false Discord alerts/day (362 since Jun 11, ~100% false):
#
#   1. Process liveness (pgrep -f run_web_dashboard) alerts on the FIRST
#      failing check — a genuinely dead process alerts within one 5-min cron
#      cycle. This is what meets the 10-minute real-failure detection bound,
#      so the log-staleness margin can stay wide without weakening coverage.
#   2. Log staleness: newest logs/session_*.log older than STALE_MAX_AGE
#      (15 min). The runtime ML retrain was removed in Phase 0 (FR-0.2), so
#      the longest legitimate quiet window is a cycle transition of a few
#      minutes (heartbeats otherwise land every ~65-80 s). The old
#      "retraining" marker (logs/.orchestrator_state) is no longer produced
#      and is IGNORED entirely, present or not.
#   3. Missing logs: at cycle rollover the session logs may be archived
#      moments before the new one exists. Zero session_*.log files is only a
#      failure if it persists for MISSING_LOG_GRACE (10 min — i.e. observed
#      on at least two consecutive cron runs). A single empty-window
#      observation arms a grace timer and stays silent.
#   4. Per-condition de-duplication: each condition (process_dead, log_stale,
#      logs_missing) re-alerts at most once per REALERT_INTERVAL (60 min).
#      A NEW condition alerts immediately even if another condition alerted
#      recently, and recovery clears a condition's state so its next
#      occurrence alerts immediately again. One ongoing incident therefore
#      produces at most one Discord message per hour, not an alert storm.
#
# Deploy (on the GCE VM, Ubuntu):
#   chmod +x ~/money_printer/scripts/host_watchdog.sh
#   crontab -e
#   Add:
#     */5 * * * * /home/USER/money_printer/scripts/host_watchdog.sh >> /home/USER/money_printer/logs/host_watchdog.log 2>&1
#
# Test manually:
#   ~/money_printer/scripts/host_watchdog.sh              # real check (may alert)
#   ~/money_printer/scripts/host_watchdog.sh --check-only # read-only, never alerts; exit 0=OK 1=fail
#
# Pause / resume (maintenance):
#   touch ~/money_printer/.host_watchdog_disabled
#   rm ~/money_printer/.host_watchdog_disabled
#
# The decision logic is mirrored 1:1 by the pure-python reference in
# scripts/vm_watchdog.py (evaluate_host_health) and exercised by
# tests/test_watchdog_phase0.py — keep the two in sync.

set -uo pipefail

# ---------------------------------------------------------------------------
# Constants (env-overridable for tests / ops tuning)
# ---------------------------------------------------------------------------
PROCESS_PATTERN="${HOST_WATCHDOG_PROCESS_PATTERN:-run_web_dashboard}"
PROJECT_DIR="${HOST_WATCHDOG_PROJECT_DIR:-$HOME/money_printer}"
LOG_DIR="$PROJECT_DIR/logs"
STATE_DIR="$LOG_DIR"
DISABLE_FLAG="$PROJECT_DIR/.host_watchdog_disabled"

# Thresholds (see header). All in seconds.
STALE_MAX_AGE="${HOST_WATCHDOG_STALE_MAX_AGE:-900}"          # 15 min
MISSING_LOG_GRACE="${HOST_WATCHDOG_MISSING_LOG_GRACE:-600}"  # 10 min
REALERT_INTERVAL="${HOST_WATCHDOG_REALERT_INTERVAL:-3600}"   # 60 min per condition
CURL_TIMEOUT=10

# State files. All inside logs/, none matching session_*.log, so archive
# sweeps never touch them.
LAST_ALERT_FILE="$STATE_DIR/host_watchdog_last_alert.ts"      # last delivered alert (observability)
MISSING_SINCE_FILE="$STATE_DIR/host_watchdog_missing_since.ts" # first check that saw zero session logs

# Per-condition last-alert timestamp file.
cond_file() { printf '%s' "$STATE_DIR/host_watchdog_cond_$1.ts"; }

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

Conditions:
  process_dead  — no run_web_dashboard process       → alert on first check
  log_stale     — newest session log > ${STALE_MAX_AGE}s old → alert
  logs_missing  — zero session logs for > ${MISSING_LOG_GRACE}s     → alert (rollover grace)
Each condition re-alerts at most once per ${REALERT_INTERVAL}s.

Options:
  --check-only   Run checks read-only; never alerts, writes no state.
                 Exit 0=healthy (or within rollover grace), 1=unhealthy.
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

# Read a unix timestamp from a state file; echoes 0 if absent/corrupt.
read_ts() {
    local ts=""
    [[ -r "$1" ]] && ts=$(head -n1 "$1" 2>/dev/null)
    [[ "$ts" =~ ^[0-9]+$ ]] || ts=0
    printf '%s' "$ts"
}

clear_condition() {
    rm -f "$(cond_file "$1")"
}

# ---------------------------------------------------------------------------
# Evaluation
#
# evaluate MODE NOW
#   MODE = "rw" (normal: may arm/clear state files) or "ro" (--check-only:
#   reads existing state, writes nothing).
# Sets globals:
#   FAILING  — space-separated condition names failing now (post-grace)
#   PENDING  — conditions observed but still within grace (never alerted)
#   DETAILS  — human-readable description for the Discord message
# ---------------------------------------------------------------------------
evaluate() {
    local mode="$1"
    local now="$2"
    FAILING=""
    PENDING=""
    DETAILS=""

    # ---- 1. Process liveness: no grace, no margin ------------------------
    if check_process; then
        [[ "$mode" == "rw" ]] && clear_condition process_dead
    else
        FAILING="$FAILING process_dead"
        DETAILS="${DETAILS}process \`run_web_dashboard.py\` is not running; "
    fi

    # ---- 2. Session logs -------------------------------------------------
    # Find the most recently modified session log without `find -printf`
    # (BSD/macOS compat) — plain glob + stat is portable enough for Ubuntu.
    local newest=""
    local f
    for f in "$LOG_DIR"/session_*.log; do
        [[ -e "$f" ]] || continue
        if [[ -z "$newest" || "$f" -nt "$newest" ]]; then
            newest="$f"
        fi
    done

    if [[ -z "$newest" ]]; then
        # Rollover grace: a brief zero-log window is normal while the old
        # session logs are archived before the new one exists. Only alert if
        # the absence persists across consecutive checks (> MISSING_LOG_GRACE).
        [[ "$mode" == "rw" ]] && clear_condition log_stale
        local since
        since=$(read_ts "$MISSING_SINCE_FILE")
        if (( since == 0 )); then
            if [[ "$mode" == "rw" ]]; then
                echo "$now" > "$MISSING_SINCE_FILE"
            fi
            PENDING="$PENDING logs_missing"
            log "Log check: no session_*.log files — rollover grace armed (alert after ${MISSING_LOG_GRACE}s)"
        elif (( now - since >= MISSING_LOG_GRACE )); then
            FAILING="$FAILING logs_missing"
            DETAILS="${DETAILS}no session_*.log files in logs/ for >$(( MISSING_LOG_GRACE / 60 ))min; "
            log "Log check: no session_*.log files for $(( now - since ))s (grace ${MISSING_LOG_GRACE}s exceeded)"
        else
            PENDING="$PENDING logs_missing"
            log "Log check: no session_*.log files for $(( now - since ))s (within ${MISSING_LOG_GRACE}s rollover grace)"
        fi
    else
        if [[ "$mode" == "rw" ]]; then
            rm -f "$MISSING_SINCE_FILE"
            clear_condition logs_missing
        fi
        local mtime age
        mtime=$(stat -c %Y "$newest" 2>/dev/null || echo 0)
        age=$(( now - mtime ))
        if (( age > STALE_MAX_AGE )); then
            FAILING="$FAILING log_stale"
            DETAILS="${DETAILS}newest session log is stale: ${age}s old (max ${STALE_MAX_AGE}s / $(( STALE_MAX_AGE / 60 ))min); "
            log "Log check: newest log '$newest' is ${age}s old (max ${STALE_MAX_AGE}s)"
        else
            [[ "$mode" == "rw" ]] && clear_condition log_stale
        fi
    fi
}

# ---------------------------------------------------------------------------
# Discord delivery (mechanics unchanged: env DISCORD_WEBHOOK_URL).
# No cooldown logic in here — per-condition backoff is decided by the caller.
# Returns 0 only when the message was actually delivered.
# ---------------------------------------------------------------------------
send_discord() {
    local message="$1"

    if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
        log "DISCORD_WEBHOOK_URL not set — skipping Discord alert: $message"
        return 1
    fi

    local payload http_code
    payload=$(printf '{"content": "%s"}' "$message")
    http_code=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time "$CURL_TIMEOUT" \
        "$DISCORD_WEBHOOK_URL" 2>/dev/null || true)

    if [[ "$http_code" == "204" || "$http_code" == "200" ]]; then
        log "Discord alert sent (HTTP $http_code): $message"
        return 0
    fi
    log "Discord alert failed (HTTP $http_code): $message"
    return 1
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

NOW=$(date +%s)

# --check-only: report health read-only, never alert, no state writes.
if $CHECK_ONLY; then
    evaluate ro "$NOW"
    if [[ -z "$FAILING" ]]; then
        if [[ -n "$PENDING" ]]; then
            log "Health check: OK (pending within grace:$PENDING)"
        else
            log "Health check: OK (process alive, logs fresh)"
        fi
        exit 0
    fi
    log "Health check: FAILING ($FAILING )"
    exit 1
fi

# Maintenance / disable flag — stay silent.
if [[ -f "$DISABLE_FLAG" ]]; then
    log "Watchdog disabled ($DISABLE_FLAG exists) — skipping checks"
    exit 0
fi

evaluate rw "$NOW"

if [[ -z "$FAILING" ]]; then
    # Healthy (or within rollover grace): exit 0 silently.
    exit 0
fi

# Unhealthy — decide which conditions are due an alert (per-condition backoff).
TO_ALERT=""
for c in $FAILING; do
    last=$(read_ts "$(cond_file "$c")")
    if (( NOW - last >= REALERT_INTERVAL )); then
        TO_ALERT="$TO_ALERT $c"
    else
        log "Alert for '$c' suppressed (backoff: $(( REALERT_INTERVAL - (NOW - last) ))s remaining)"
    fi
done

log "UNHEALTHY:$FAILING — $DETAILS"

if [[ -n "$TO_ALERT" ]]; then
    local_host=$(hostname 2>/dev/null || echo "unknown-host")
    if send_discord "🚨 Money Printer host-watchdog [$local_host]: $DETAILS"; then
        # One message covered every currently-failing condition — refresh all
        # their backoff timestamps, plus the global last-alert marker.
        for c in $FAILING; do
            echo "$NOW" > "$(cond_file "$c")"
        done
        echo "$NOW" > "$LAST_ALERT_FILE"
    fi
    # On delivery failure the timestamps stay unset, so the next cron run
    # retries (bounded to one attempt per 5-min cycle — no storm possible).
fi

exit 1
