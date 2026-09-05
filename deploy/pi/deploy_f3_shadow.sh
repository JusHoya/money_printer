#!/usr/bin/env bash
# F3 shadow deploy for maia -- one command, idempotent (docs/factory/F3_RUNBOOK.md §3, §1.1).
#
#   ON maia, from the checkout (~/money_printer):
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id>              # e.g. 0c4b20502f2daf65 (fr31a_taker)
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --no-repair  # skip the NO-side state repair
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --any-time   # deploy off-boundary, forfeiting today
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --plan       # print the pre-flight and exit (no side effects)
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --max-wait 600      # refuse rather than wait longer
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --repair-budget 90  # how long step 6 really takes here
#
# What it does, in order:
#   1. git pull --ff-only (refuses on a non-fast-forward, like deploy/README.md says)
#   2. creates /srv/money_printer/data/forecast_cache owned by uid 1000 (compose bind)
#      and copies data/calibration/*.json into the /srv data bind
#   3. upserts GENOME_STRATEGY_ID in /srv/money_printer/.env and NEUTRALISES any
#      GENOME_STRATEGY_MODE line there -- that line is dead config (see §MODE below)
#   4. builds the image
#   5. waits (sandbox still UP) until the work still to come -- the repair -- will
#      finish just before a :00 UTC boundary, so `up -d` lands on it (see §BOUNDARY)
#   6. stops the sandbox, repairs the NO-side settlement sign in exchange_state.json and
#      trade_journal.jsonl (scripts/repair_no_settlement_pnl.py, dry run printed first,
#      then --apply with .bak-n backups) -- the engine fix 724d93c only corrects FUTURE
#      settlements; historical NO closes since 2026-09-01 are sign-flipped until repaired
#   7. a SHORT top-up wait if the repair beat (or blew) its budget, then
#      docker compose up -d --build, /healthz, then asserts the container's ACTUAL
#      GENOME_STRATEGY_MODE/ID VALUES and POLLS the FILE log for the loaded/REFUSED line
#   8. prints how to verify the F3 maia criterion after the next :00 UTC boundary
#
# §MODE -- which configuration layer actually wins (F3 review, 2026-09-05).
#   deploy/pi/docker-compose.yml declares GENOME_STRATEGY_MODE under `environment:`,
#   and Compose resolves `environment:` ABOVE `env_file:`. A GENOME_STRATEGY_MODE line
#   in /srv/money_printer/.env is therefore DEAD CONFIG: it can never change the
#   container's mode, it can only mislead whoever reads the file. This script no longer
#   writes it, comments out any line a previous run wrote, and asserts the mode by VALUE
#   from `docker exec ... printenv` -- the only layer that is the truth. The invoking
#   shell is the layer that does win, so a GENOME_STRATEGY_MODE other than "shadow"
#   exported into this script's environment is refused up front.
#
# §BOUNDARY -- why an off-hour deploy costs a market-day (F3 review, 2026-09-05).
#   GenomeStrategy evaluates at the top of the hour with a 120 s tolerance
#   (DEFAULT_TOP_OF_HOUR_TOLERANCE_S). A container whose first tick lands later than
#   that inside an hour records that hour as "missed"; at the next evaluated hour the
#   missed-hour rule closes EVERY visible city-day with GENOME_MISSED_HOUR for the rest
#   of the market-day, and the state file remembers it for STATE_KEEP_DAYS. The
#   2026-09-05T03:49:28Z deploy landed 49 minutes past a boundary and cost all four
#   cities that day; in F4 each such day is one fewer of the >=50 settled target_dates
#   the FR-5.2 gate needs. So this script waits for the aligned window by default.
#
#   Two numbers govern the wait, and BOTH are needed (F3 remediation, 2026-09-05):
#     LAUNCH_LEAD_S    how long `up -d --build` needs before the container's FIRST tick.
#     REPAIR_BUDGET_S  how long step 6 needs -- `compose stop` plus 2-4 `compose run`
#                      container starts. It is subtracted from the wait, because the
#                      repair runs BETWEEN the wait and `up -d`; a wait sized as if
#                      only `up -d` followed could sleep most of an hour and still
#                      launch past the tolerance, which is the exact forfeiture this
#                      gate exists to prevent.
#   "Aligned" is measured against the NEAREST :00, not against seconds-into-the-hour:
#   75 s BEFORE a boundary is the very spot the wait parks in, and calling it LATE
#   reported a successful wait as a failure.
set -euo pipefail

# ---- tunables (env-overridable; the defaults are what maia runs) --------------
# Mirrors src/strategies/genome_strategy.py DEFAULT_TOP_OF_HOUR_TOLERANCE_S.
TOP_OF_HOUR_TOLERANCE_S="${MP_TOP_OF_HOUR_TOLERANCE_S:-120}"
# `up -d --build` -> container boot -> first ladder tick, on a Pi 4.
LAUNCH_LEAD_S="${MP_LAUNCH_LEAD_S:-75}"
# Step 6 on a Pi 4: `compose stop` + 2-4 `compose run` container starts (§BOUNDARY).
REPAIR_BUDGET_S="${MP_REPAIR_BUDGET_S:-240}"
# Hard bound on the boundary wait: the script REFUSES rather than sleeping longer.
MAX_WAIT_S="${MP_MAX_WAIT_S:-3600}"
# The loaded/REFUSED line is POLLED to this deadline, never read once after a blind
# sleep: the bot writes it after /healthz answers and that gap is not bounded by 10 s.
LOADED_DEADLINE_S="${MP_LOADED_DEADLINE_S:-120}"
LOADED_POLL_S="${MP_LOADED_POLL_S:-5}"

log() { printf '[deploy_f3_shadow %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

# MP_DEPLOY_NOW_EPOCH is a TEST SEAM (tests/test_ops_f3_deploy_and_cadence.py): it pins
# the clock the boundary math reads so the pre-flight AND the wait can be exercised
# without waiting an hour. Never set it on maia.
now_epoch() { echo "${MP_DEPLOY_NOW_EPOCH:-$(date -u +%s)}"; }

# Prints "<into> <until_next> <nearest> <IN_WINDOW|LATE>".
# The verdict is the distance to the NEAREST :00 measured against the strategy's own
# tolerance. Seconds-into-the-hour alone cannot express "just before the next :00",
# so it called the wait's own target LATE and printed an unrecoverable-cost warning
# a few seconds past the hour (F3 remediation, blocking 2).
boundary_state() {
  local now into until_next nearest
  now="$(now_epoch)"
  into=$(( now % 3600 ))
  until_next=$(( 3600 - into ))
  nearest=$(( into < until_next ? into : until_next ))
  if (( nearest <= TOP_OF_HOUR_TOLERANCE_S )); then
    echo "$into $until_next $nearest IN_WINDOW"
  else
    echo "$into $until_next $nearest LATE"
  fi
}

# wait_seconds_for <budget_s> -- how long to sleep so that, after <budget_s> more
# seconds of work, `docker compose up -d` runs inside the aligned window. 0 when it
# already would. <budget_s> is what makes the repair fit: see §BOUNDARY.
wait_seconds_for() {
  local budget="$1" now into until_next target
  now="$(now_epoch)"
  into=$(( now % 3600 ))
  until_next=$(( 3600 - into ))
  if (( into + budget <= TOP_OF_HOUR_TOLERANCE_S )); then echo 0; return 0; fi
  target=$(( until_next - LAUNCH_LEAD_S - budget ))
  (( target < 0 )) && target=0
  echo "$target"
}

on_int() {
  echo
  log "interrupted (SIGINT) during the boundary wait"
  declare -F restore >/dev/null && restore
  exit 130
}

# nap <seconds> -- announced, interruptible sleep in <=60 s chunks with a progress
# line every 5 min. Under the MP_DEPLOY_NOW_EPOCH seam it ADVANCES the pinned clock
# instead of sleeping, so tests drive the real wait path in milliseconds.
nap() {
  local total="$1" left="$1" chunk
  if [[ -n "${MP_DEPLOY_NOW_EPOCH:-}" ]]; then
    MP_DEPLOY_NOW_EPOCH=$(( MP_DEPLOY_NOW_EPOCH + total ))
    export MP_DEPLOY_NOW_EPOCH
    return 0
  fi
  while (( left > 0 )); do
    chunk=$(( left % 60 )); (( chunk == 0 )) && chunk=60
    sleep "$chunk"
    left=$(( left - chunk ))
    if (( left > 0 && left % 300 == 0 )); then
      log "  ... ${left}s of boundary wait left (Ctrl-C is safe here)"
    fi
  done
}

# wait_for_launch_window <budget_s> <cap_s> <label>
# Sleeps until `up -d` will land in the aligned window after <budget_s> more seconds
# of work. Returns 1 WITHOUT sleeping when that would take longer than <cap_s>, so
# the caller decides whether a bounded wait is fatal or just a warning.
wait_for_launch_window() {
  local budget="$1" cap="$2" label="$3" secs target
  secs="$(wait_seconds_for "$budget")"
  if (( secs == 0 )); then
    log "$label: no wait needed -- \`up -d\` after the next ${budget}s of work still lands inside the aligned window"
    return 0
  fi
  if (( secs > cap )); then
    log "$label: would have to wait ${secs}s, more than the ${cap}s bound"
    return 1
  fi
  target="$(date -u -d "@$(( $(now_epoch) + secs ))" +%FT%TZ 2>/dev/null || echo "now+${secs}s")"
  log "$label: waiting ${secs}s (until ~${target}) so \`up -d\` lands on a :00 UTC boundary."
  log "         Ctrl-C is safe here -- nothing has been stopped yet; --any-time skips the wait."
  nap "$secs"
}

# read_genome_log <container_id> -- the newest /app/logs/money_printer_*.log lines that
# mention GenomeStrategy, or nothing at all. The loaded / REFUSED lines go through
# src/utils/logger.py, which attaches ONLY a FileHandler and forces any console
# StreamHandler to WARNING+ -- so NOTHING the bot logs reaches container stdout and
# `compose logs | grep 'GenomeStrategy'` is unconditionally empty, on a good deploy as
# readily as a broken one (F3 review, 2026-09-05). Read the file log in the container.
# MP_GENOME_LOG_CMD is a TEST SEAM (tests/test_ops_f3_deploy_and_cadence.py).
read_genome_log() {
  if [[ -n "${MP_GENOME_LOG_CMD:-}" ]]; then eval "$MP_GENOME_LOG_CMD" || true; return 0; fi
  docker exec "$1" sh -c \
    'set -e; f=$(ls -t /app/logs/money_printer_*.log 2>/dev/null | head -n 1); [ -n "$f" ] && grep -h "GenomeStrategy" "$f" | tail -n 5' || true
}

# poll_genome_log <container_id> -- read_genome_log every LOADED_POLL_S until it says
# something or LOADED_DEADLINE_S elapses; returns 1 if it never does. A single blind
# `sleep 10` followed by a hard `die` reported a CORRECT deploy as broken whenever the
# bot wrote its loaded line more than 10 s after /healthz answered (F3 remediation).
poll_genome_log() {
  local cid="$1" deadline out
  deadline=$(( $(date -u +%s) + LOADED_DEADLINE_S ))
  while :; do
    out="$(read_genome_log "$cid")"
    if [[ -n "$out" ]]; then printf '%s\n' "$out"; return 0; fi
    if (( $(date -u +%s) >= deadline )); then return 1; fi
    sleep "$LOADED_POLL_S"
  done
}

# MP_DEPLOY_LIB_ONLY=1 `source`s the pure boundary/poll helpers above and stops before
# any argument parsing or side effect, so tests/test_ops_f3_deploy_and_cadence.py can
# drive the real wait path. Never set it on maia.
trap on_int INT
if [[ "${MP_DEPLOY_LIB_ONLY:-0}" == 1 ]]; then return 0 2>/dev/null || exit 0; fi

GENOME_ID=""
DO_REPAIR=1
WAIT_FOR_BOUNDARY=1
PLAN_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-repair)          DO_REPAIR=0 ;;
    --any-time)           WAIT_FOR_BOUNDARY=0 ;;
    --at-boundary)        WAIT_FOR_BOUNDARY=1 ;;
    --plan|--dry-plan)    PLAN_ONLY=1 ;;
    --max-wait)           shift; MAX_WAIT_S="${1:?--max-wait needs a number of seconds}" ;;
    --max-wait=*)         MAX_WAIT_S="${1#*=}" ;;
    --repair-budget)      shift; REPAIR_BUDGET_S="${1:?--repair-budget needs a number of seconds}" ;;
    --repair-budget=*)    REPAIR_BUDGET_S="${1#*=}" ;;
    -h|--help)            awk 'NR==1{next} /^#/{print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)                   echo "unknown option: $1" >&2; exit 2 ;;
    *)                    [[ -n "$GENOME_ID" ]] && { echo "unexpected argument: $1" >&2; exit 2; }
                          GENOME_ID="$1" ;;
  esac
  shift
done
[[ -n "$GENOME_ID" ]] || {
  echo "usage: $0 <genome_id> [--no-repair] [--any-time|--at-boundary] [--plan]" >&2
  echo "                      [--max-wait SECONDS] [--repair-budget SECONDS]" >&2; exit 2; }
# No repair means no work between the wait and `up -d`, so nothing to budget for.
[[ "$DO_REPAIR" == 1 ]] || REPAIR_BUDGET_S=0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
STATE_ROOT=/srv/money_printer
ENV_FILE="$STATE_ROOT/.env"
SPEC="$ROOT/configs/factory/promoted/$GENOME_ID.json"
DASHBOARD_URL="${MP_DASHBOARD_URL:-http://localhost:8050}"

STOPPED=0   # 1 while the sandbox is stopped for the repair
restore() { if [[ "$STOPPED" == 1 ]]; then STOPPED=0; log "restoring the sandbox after a failure"; "${COMPOSE[@]}" up -d || true; fi; }
die() { log "ERROR: $*"; restore; exit 1; }
trap restore ERR

# ---- pre-flight (no side effects; runs before git/sudo/docker touch anything) ----
ENV_MODE="${GENOME_STRATEGY_MODE:-}"
read -r SEC_INTO_HOUR SEC_TO_BOUNDARY SEC_TO_NEAREST BOUNDARY_VERDICT <<<"$(boundary_state)"
PLANNED_WAIT_S="$(wait_seconds_for "$REPAIR_BUDGET_S")"
log "0/7 pre-flight"
echo "  genome_id           = $GENOME_ID"
echo "  spec                = $SPEC"
echo "  shell GENOME_STRATEGY_MODE = ${ENV_MODE:-<unset, compose default: shadow>}"
echo "  seconds into hour   = $SEC_INTO_HOUR"
echo "  next :00 UTC in     = ${SEC_TO_BOUNDARY}s"
echo "  nearest :00 UTC     = ${SEC_TO_NEAREST}s away (aligned window: within ${TOP_OF_HOUR_TOLERANCE_S}s of a :00)"
echo "  boundary verdict    = $BOUNDARY_VERDICT"
if [[ "$BOUNDARY_VERDICT" == LATE ]]; then
  echo "  COST of deploying now: the container's first tick lands more than"
  echo "    ${TOP_OF_HOUR_TOLERANCE_S}s past :00, so the missed-hour rule closes EVERY city-day now"
  echo "    visible (all four cities) with GENOME_MISSED_HOUR for the rest of the market-day."
  echo "    That is unrecoverable -- the genome state file remembers it for days, and in F4"
  echo "    each lost day is one fewer of the >=50 settled target_dates the gate needs."
  if [[ "$WAIT_FOR_BOUNDARY" == 1 ]]; then
    echo "    ACTION: this run will WAIT ~${PLANNED_WAIT_S}s for the next :00 before starting the container."
    echo "            Ctrl-C and re-run with --any-time to accept the cost instead."
  else
    echo "    ACTION: --any-time given -- proceeding and ACCEPTING the forfeited market-day."
  fi
else
  echo "  deploying now lands inside the aligned window; no city-day is forfeited."
fi
if [[ "$WAIT_FOR_BOUNDARY" == 1 ]]; then
  echo "  SCHEDULE: repair budget ${REPAIR_BUDGET_S}s + launch lead ${LAUNCH_LEAD_S}s -> planned wait ${PLANNED_WAIT_S}s"
  if (( PLANNED_WAIT_S > 0 )); then
    if [[ "$BOUNDARY_VERDICT" == IN_WINDOW ]]; then
      echo "    NOW is inside the window but \`up -d\` will not be: the repair moves the launch"
      echo "    ${REPAIR_BUDGET_S}s into the future, past the ${TOP_OF_HOUR_TOLERANCE_S}s tolerance. Hence the wait."
    fi
    echo "    The repair (step 6) runs BETWEEN the wait and \`up -d\`, so its ${REPAIR_BUDGET_S}s are"
    echo "    subtracted: the wait ends, the repair runs, and \`up -d\` lands ~${LAUNCH_LEAD_S}s before a :00."
    echo "    The wait is announced, chunked and Ctrl-C-able. --no-repair (it is idempotent) or"
    echo "    --any-time removes it; --repair-budget N retunes it for this host."
    if (( PLANNED_WAIT_S > MAX_WAIT_S )); then
      echo "    REFUSED: ${PLANNED_WAIT_S}s exceeds the ${MAX_WAIT_S}s --max-wait bound; this run would stop here."
    else
      echo "    --max-wait bound: ${MAX_WAIT_S}s (this run would sleep ${PLANNED_WAIT_S}s)."
    fi
  fi
fi
# The invoking shell is the layer that beats compose's default (§MODE), so a non-shadow
# value here would silently deploy something other than shadow. F3 is shadow only.
if [[ -n "$ENV_MODE" && "${ENV_MODE,,}" != "shadow" ]]; then
  echo "[deploy_f3_shadow] ERROR: GENOME_STRATEGY_MODE=$ENV_MODE is exported into this shell." >&2
  echo "  compose resolves the invoking shell above both env_file and its own default," >&2
  echo "  so this deploy would NOT be shadow. F3 deploys shadow only: unset it and re-run." >&2
  exit 2
fi
if [[ "$PLAN_ONLY" == 1 ]]; then
  log "--plan: pre-flight only, nothing was changed"
  exit 0
fi

cd "$ROOT"
log "1/7 git pull --ff-only"
git pull --ff-only || die "git pull refused (non-fast-forward) -- reconcile by hand, never reset the sandbox checkout"
[[ -f "$SPEC" ]] || die "promoted spec missing after pull: $SPEC"
grep -q '"mode": "shadow"' "$SPEC" || die "$SPEC is not a shadow spec; F3 deploys shadow only"

log "2/7 forecast cache bind"
sudo mkdir -p "$STATE_ROOT/data/forecast_cache"
sudo chown 1000:1000 "$STATE_ROOT/data/forecast_cache"

log "2b/7 frozen calibration payloads into the data bind"
# The image excludes data/ (.dockerignore) and the /srv/money_printer/data bind shadows
# /app/data, so the spec's calibration dir (data/calibration, tracked in git) must be
# copied into the bind or GenomeStrategy cannot be built (maia 2026-09-05 crash-loop).
sudo mkdir -p "$STATE_ROOT/data/calibration"
sudo cp -f "$ROOT"/data/calibration/*.json "$STATE_ROOT/data/calibration/"
sudo chown -R 1000:1000 "$STATE_ROOT/data/calibration"
echo "  $(ls "$STATE_ROOT/data/calibration" | wc -l) calibration files in $STATE_ROOT/data/calibration"

log "3/7 runtime env ($ENV_FILE)"
sudo touch "$ENV_FILE"
upsert() {  # upsert KEY VALUE -- replace the line if present, append otherwise
  local key="$1" val="$2"
  if sudo grep -qE "^${key}=" "$ENV_FILE"; then
    sudo sed -i -E "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
}
upsert GENOME_STRATEGY_ID "$GENOME_ID"
# GENOME_STRATEGY_MODE in the env file is DEAD CONFIG (§MODE). Comment out any line a
# previous run of this script wrote so nobody reads it as the running mode. Idempotent:
# after the first pass there is no `^GENOME_STRATEGY_MODE=` line left to match.
if sudo grep -qE '^GENOME_STRATEGY_MODE=' "$ENV_FILE"; then
  sudo sed -i -E 's|^GENOME_STRATEGY_MODE=(.*)$|# GENOME_STRATEGY_MODE=\1  # DEAD CONFIG: compose environment: wins (F3_RUNBOOK §3)|' "$ENV_FILE"
  log "commented out the dead GENOME_STRATEGY_MODE line in $ENV_FILE (compose environment: wins)"
fi
sudo grep -E '^#? *GENOME_' "$ENV_FILE" || true

SERVICE=sandbox   # the runtime service (container mp-sandbox); NOT config --services|head, which lists autoheal first

log "4/7 docker compose build $SERVICE"
# Built BEFORE the wait and the repair: `compose run` uses the image as built and the
# repair script lives INSIDE it (the Dockerfile copies the checkout), so the freshly
# pulled tree must be in the image first. The build is also the one slow step whose
# duration cannot be budgeted, which is why it happens before the clock matters.
"${COMPOSE[@]}" build "$SERVICE"

log "5/7 boundary gate (the sandbox is still UP for this wait)"
if [[ "$WAIT_FOR_BOUNDARY" == 1 ]]; then
  # Budgeted for the repair, which runs between here and `up -d` (§BOUNDARY).
  wait_for_launch_window "$REPAIR_BUDGET_S" "$MAX_WAIT_S" "boundary gate" || die \
    "the boundary wait would exceed --max-wait ${MAX_WAIT_S}s. Re-run within a few minutes of a
     :00 UTC, raise --max-wait, drop the repair with --no-repair (it is idempotent), or accept
     the forfeited market-day with --any-time"
else
  log "--any-time: not waiting for the boundary"
fi

if [[ "$DO_REPAIR" == 1 ]]; then
  log "6/7 NO-side settlement repair (sandbox stopped while the state file is rewritten)"
  REPAIR_START="$(date -u +%s)"
  "${COMPOSE[@]}" stop
  STOPPED=1
  REPAIR=(python scripts/repair_no_settlement_pnl.py --state /app/data/exchange_state.json --journal /app/data/trade_journal.jsonl)
  RUN=("${COMPOSE[@]}" run --rm --no-deps --entrypoint python "$SERVICE")
  "${RUN[@]}" -c 'import src.core.matching_engine; print("repair image OK")' || die "the $SERVICE image cannot import the engine"
  # dry run: exit 1 = repairs pending, 0 = nothing to do. Evaluated in an `if` so the
  # expected non-zero exit does not fire the ERR trap (which restarted the sandbox
  # mid-repair on 2026-09-05).
  if "${RUN[@]}" "${REPAIR[@]:1}"; then rc=0; else rc=$?; fi
  if [[ $rc -eq 1 ]]; then
    "${RUN[@]}" "${REPAIR[@]:1}" --apply || die "repair --apply failed; backups are next to the files as .bak-n"
    log "repair applied; second dry run must now be clean:"
    "${RUN[@]}" "${REPAIR[@]:1}" || die "repair not idempotent -- stop and inspect"
  elif [[ $rc -ne 0 ]]; then
    die "repair dry run exited $rc"
  else
    log "no stale NO-side rows found"
  fi
  log "repair took $(( $(date -u +%s) - REPAIR_START ))s (budget ${REPAIR_BUDGET_S}s; --repair-budget retunes it)"
else
  log "6/7 repair skipped (--no-repair)"
fi

log "7/7 launch gate, then docker compose up -d --build"
if [[ "$WAIT_FOR_BOUNDARY" == 1 ]]; then
  # Top up only BRIEFLY. The sandbox is STOPPED here, so sleeping to a boundary that is
  # most of an hour away would be worse than launching late: it would take the other
  # four bots down with it AND make the genome's own restart gap exceed one hour.
  # Worst-case downtime is therefore repair + TOPUP_CAP, i.e. ~8 min at the defaults.
  TOPUP_CAP=$(( REPAIR_BUDGET_S > LAUNCH_LEAD_S ? REPAIR_BUDGET_S : LAUNCH_LEAD_S ))
  if ! wait_for_launch_window 0 "$TOPUP_CAP" "launch gate"; then
    log "WARNING: the repair overran its ${REPAIR_BUDGET_S}s budget and the next :00 is more than"
    log "         ${TOPUP_CAP}s away. The sandbox is stopped, so this run launches NOW rather than"
    log "         leaving it down. Expect GENOME_MISSED_HOUR for today's city-days (§BOUNDARY)."
  fi
fi
"${COMPOSE[@]}" up -d --build
STOPPED=0
for i in $(seq 1 30); do curl -sf "$DASHBOARD_URL/healthz" && break || sleep 3; done
curl -sf "$DASHBOARD_URL/healthz" >/dev/null || die "healthz failed after 90 s -- see: ${COMPOSE[*]} logs --tail 100 $SERVICE"
echo
read -r SEC_INTO_HOUR SEC_TO_BOUNDARY SEC_TO_NEAREST BOUNDARY_VERDICT <<<"$(boundary_state)"
log "launched at +${SEC_INTO_HOUR}s into the hour (nearest :00 is ${SEC_TO_NEAREST}s away: $BOUNDARY_VERDICT)"
if [[ "$BOUNDARY_VERDICT" == LATE ]]; then
  log "WARNING: that is past the ${TOP_OF_HOUR_TOLERANCE_S}s tolerance on both sides of a :00."
  log "         Expect GENOME_MISSED_HOUR for today's city-days (§BOUNDARY). This is not recoverable."
fi

CID="$("${COMPOSE[@]}" ps -q "$SERVICE")"
[[ -n "$CID" ]] || die "no running container for service $SERVICE"
# Assert the container's ACTUAL values, not the presence of any GENOME_ line and not
# the losing env-file layer (§MODE). `printenv KEY` exits 1 when the key is unset.
ACTUAL_MODE="$(docker exec "$CID" printenv GENOME_STRATEGY_MODE || true)"
ACTUAL_ID="$(docker exec "$CID" printenv GENOME_STRATEGY_ID || true)"
ACTUAL_CACHE="$(docker exec "$CID" printenv MP_FORECAST_CACHE_DIR || true)"
echo "  container GENOME_STRATEGY_MODE = ${ACTUAL_MODE:-<unset>}"
echo "  container GENOME_STRATEGY_ID   = ${ACTUAL_ID:-<unset>}"
echo "  container MP_FORECAST_CACHE_DIR= ${ACTUAL_CACHE:-<unset>}"
[[ "$ACTUAL_MODE" == "shadow" ]] || die "container GENOME_STRATEGY_MODE is '${ACTUAL_MODE:-<unset>}', expected 'shadow' -- F3 deploys shadow only"
[[ "$ACTUAL_ID" == "$GENOME_ID" ]] || die "container GENOME_STRATEGY_ID is '${ACTUAL_ID:-<unset>}', expected '$GENOME_ID'"
[[ -n "$ACTUAL_CACHE" ]] || die "MP_FORECAST_CACHE_DIR is unset in the container -- the genome state file has nowhere to live"

GENOME_LOG="$(poll_genome_log "$CID")" || die \
  "no GenomeStrategy line in the container's newest /app/logs/money_printer_*.log within
     ${LOADED_DEADLINE_S}s (polled every ${LOADED_POLL_S}s) --
     the log file may not exist yet (retry: docker exec $CID sh -c 'ls -t /app/logs/money_printer_*.log')
     or WeatherBot never reached the genome block (GENOME_STRATEGY_ID unset at import time).
     A slow Pi 4 is not a failure: raise MP_LOADED_DEADLINE_S and re-run."
echo "$GENOME_LOG"
if grep -q 'GenomeStrategy REFUSED' <<<"$GENOME_LOG"; then
  die "GenomeStrategy was REFUSED (line above) -- the bot is running V2 only"
elif grep -qE 'GenomeStrategy .* loaded' <<<"$GENOME_LOG"; then
  grep -qE 'mode=shadow|shadow=True' <<<"$GENOME_LOG" \
    || die "the loaded line does not say shadow -- refusing to call this a shadow deploy"
  log "genome strategy loaded in shadow mode (verified from the file log and the container env)"
else
  die "the GenomeStrategy lines above say neither 'loaded' nor 'REFUSED' -- inspect them by hand"
fi

log "done. After the next :00 UTC boundary, from any LAN host:"
echo "   python scripts/check_maia_emit_cadence.py --url http://maia.local:8050"
echo "   (--url is the BASE url; the client appends /api/logs/tail itself)"
echo "   expected: PASS with verified_ok >= 1 and outcome_codes == {GENOME_SHADOW: n_emit}"
echo "   rollback: remove GENOME_STRATEGY_ID from $ENV_FILE and run: ${COMPOSE[*]} up -d"
