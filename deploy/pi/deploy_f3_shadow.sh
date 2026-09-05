#!/usr/bin/env bash
# F3 shadow deploy for maia -- one command, idempotent (docs/factory/F3_RUNBOOK.md §3, §1.1).
#
#   ON maia, from the checkout (~/money_printer):
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id>              # e.g. 0c4b20502f2daf65 (fr31a_taker)
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --no-repair  # skip the NO-side state repair
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --any-time   # deploy off-boundary, forfeiting today
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --plan       # print the pre-flight and exit (no side effects)
#
# What it does, in order:
#   1. git pull --ff-only (refuses on a non-fast-forward, like deploy/README.md says)
#   2. creates /srv/money_printer/data/forecast_cache owned by uid 1000 (compose bind)
#      and copies data/calibration/*.json into the /srv data bind
#   3. upserts GENOME_STRATEGY_ID in /srv/money_printer/.env and NEUTRALISES any
#      GENOME_STRATEGY_MODE line there -- that line is dead config (see §MODE below)
#   4. builds the image, then waits for the :00-aligned launch window (see §BOUNDARY)
#   5. stops the sandbox, repairs the NO-side settlement sign in exchange_state.json and
#      trade_journal.jsonl (scripts/repair_no_settlement_pnl.py, dry run printed first,
#      then --apply with .bak-n backups) -- the engine fix 724d93c only corrects FUTURE
#      settlements; historical NO closes since 2026-09-01 are sign-flipped until repaired
#   6. docker compose up -d --build, /healthz, then asserts the container's ACTUAL
#      GENOME_STRATEGY_MODE/ID VALUES and reads the FILE log for the loaded/REFUSED line
#   7. prints how to verify the F3 maia criterion after the next :00 UTC boundary
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
set -euo pipefail

GENOME_ID=""
DO_REPAIR=1
WAIT_FOR_BOUNDARY=1
PLAN_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-repair)          DO_REPAIR=0 ;;
    --any-time)           WAIT_FOR_BOUNDARY=0 ;;
    --at-boundary)        WAIT_FOR_BOUNDARY=1 ;;
    --plan|--dry-plan)    PLAN_ONLY=1 ;;
    -h|--help)            sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)                   echo "unknown option: $arg" >&2; exit 2 ;;
    *)                    [[ -n "$GENOME_ID" ]] && { echo "unexpected argument: $arg" >&2; exit 2; }
                          GENOME_ID="$arg" ;;
  esac
done
[[ -n "$GENOME_ID" ]] || {
  echo "usage: $0 <genome_id> [--no-repair] [--any-time|--at-boundary] [--plan]" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
STATE_ROOT=/srv/money_printer
ENV_FILE="$STATE_ROOT/.env"
SPEC="$ROOT/configs/factory/promoted/$GENOME_ID.json"
DASHBOARD_URL="${MP_DASHBOARD_URL:-http://localhost:8050}"
# Mirrors src/strategies/genome_strategy.py DEFAULT_TOP_OF_HOUR_TOLERANCE_S. The
# launch window is shortened by LAUNCH_LEAD_S so the container has time to boot and
# take its first tick inside the tolerance.
TOP_OF_HOUR_TOLERANCE_S="${MP_TOP_OF_HOUR_TOLERANCE_S:-120}"
LAUNCH_LEAD_S="${MP_LAUNCH_LEAD_S:-75}"

log() { printf '[deploy_f3_shadow %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
STOPPED=0   # 1 while the sandbox is stopped for the repair
restore() { if [[ "$STOPPED" == 1 ]]; then STOPPED=0; log "restoring the sandbox after a failure"; "${COMPOSE[@]}" up -d || true; fi; }
die() { log "ERROR: $*"; restore; exit 1; }
trap restore ERR

# MP_DEPLOY_NOW_EPOCH is a TEST SEAM (tests/test_ops_deploy_preflight.py): it pins the
# clock the boundary math reads so the pre-flight can be exercised without waiting an
# hour. Never set it on maia.
now_epoch() { echo "${MP_DEPLOY_NOW_EPOCH:-$(date -u +%s)}"; }

# Prints "<seconds into the hour> <seconds until the next :00> <IN_WINDOW|LATE>".
boundary_state() {
  local now into until_next usable
  now="$(now_epoch)"
  into=$(( now % 3600 ))
  until_next=$(( 3600 - into ))
  usable=$(( TOP_OF_HOUR_TOLERANCE_S - LAUNCH_LEAD_S ))
  (( usable < 0 )) && usable=0
  if (( into <= usable )); then echo "$into $until_next IN_WINDOW"; else echo "$into $until_next LATE"; fi
}

# ---- pre-flight (no side effects; runs before git/sudo/docker touch anything) ----
ENV_MODE="${GENOME_STRATEGY_MODE:-}"
read -r SEC_INTO_HOUR SEC_TO_BOUNDARY BOUNDARY_VERDICT <<<"$(boundary_state)"
log "0/7 pre-flight"
echo "  genome_id           = $GENOME_ID"
echo "  spec                = $SPEC"
echo "  shell GENOME_STRATEGY_MODE = ${ENV_MODE:-<unset, compose default: shadow>}"
echo "  seconds into hour   = $SEC_INTO_HOUR (aligned launch window: 0..$(( TOP_OF_HOUR_TOLERANCE_S - LAUNCH_LEAD_S > 0 ? TOP_OF_HOUR_TOLERANCE_S - LAUNCH_LEAD_S : 0 ))s)"
echo "  next :00 UTC in     = ${SEC_TO_BOUNDARY}s"
echo "  boundary verdict    = $BOUNDARY_VERDICT"
if [[ "$BOUNDARY_VERDICT" == LATE ]]; then
  echo "  COST of deploying now: the container's first tick lands more than"
  echo "    ${TOP_OF_HOUR_TOLERANCE_S}s past :00, so the missed-hour rule closes EVERY city-day now"
  echo "    visible (all four cities) with GENOME_MISSED_HOUR for the rest of the market-day."
  echo "    That is unrecoverable -- the genome state file remembers it for days, and in F4"
  echo "    each lost day is one fewer of the >=50 settled target_dates the gate needs."
  if [[ "$WAIT_FOR_BOUNDARY" == 1 ]]; then
    echo "    ACTION: this run will WAIT ~${SEC_TO_BOUNDARY}s for the next :00 before starting the container."
    echo "            Ctrl-C and re-run with --any-time to accept the cost instead."
  else
    echo "    ACTION: --any-time given -- proceeding and ACCEPTING the forfeited market-day."
  fi
else
  echo "  deploying now lands inside the aligned window; no city-day is forfeited."
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
# Built BEFORE the boundary wait and the repair: `compose run` uses the image as built
# and the repair script lives INSIDE it (the Dockerfile copies the checkout), so the
# freshly pulled tree must be in the image first. Building here also means the wait
# below is the LAST slow step, so `up -d` really does land on the boundary.
"${COMPOSE[@]}" build "$SERVICE"

log "5/7 boundary gate"
read -r SEC_INTO_HOUR SEC_TO_BOUNDARY BOUNDARY_VERDICT <<<"$(boundary_state)"
if [[ "$BOUNDARY_VERDICT" == LATE && "$WAIT_FOR_BOUNDARY" == 1 ]]; then
  # Wait until LAUNCH_LEAD_S before the next :00 so the container is up and has taken
  # its first tick inside the top-of-hour tolerance.
  SLEEP_S=$(( SEC_TO_BOUNDARY - LAUNCH_LEAD_S ))
  (( SLEEP_S < 0 )) && SLEEP_S=0
  log "waiting ${SLEEP_S}s for the :00-aligned launch window (--any-time skips this)"
  sleep "$SLEEP_S"
  read -r SEC_INTO_HOUR SEC_TO_BOUNDARY BOUNDARY_VERDICT <<<"$(boundary_state)"
fi
log "launching at +${SEC_INTO_HOUR}s into the hour ($BOUNDARY_VERDICT)"

if [[ "$DO_REPAIR" == 1 ]]; then
  log "6/7 NO-side settlement repair (sandbox stopped while the state file is rewritten)"
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
else
  log "6/7 repair skipped (--no-repair)"
fi

log "7/7 docker compose up -d --build"
"${COMPOSE[@]}" up -d --build
STOPPED=0
for i in $(seq 1 30); do curl -sf "$DASHBOARD_URL/healthz" && break || sleep 3; done
curl -sf "$DASHBOARD_URL/healthz" >/dev/null || die "healthz failed after 90 s -- see: ${COMPOSE[*]} logs --tail 100 $SERVICE"
echo
read -r SEC_INTO_HOUR _ BOUNDARY_VERDICT <<<"$(boundary_state)"
if [[ "$BOUNDARY_VERDICT" == LATE ]]; then
  log "WARNING: the container came up +${SEC_INTO_HOUR}s into the hour, past the ${TOP_OF_HOUR_TOLERANCE_S}s tolerance."
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

sleep 10
# The loaded / REFUSED lines go through src/utils/logger.py, which attaches ONLY a
# FileHandler and forces any console StreamHandler to WARNING+ -- so NOTHING the bot
# logs reaches container stdout and `compose logs | grep 'GenomeStrategy'` is
# unconditionally empty, on a good deploy as readily as a broken one (F3 review,
# 2026-09-05). Read the file log inside the container instead.
GENOME_LOG="$(docker exec "$CID" sh -c \
  'set -e; f=$(ls -t /app/logs/money_printer_*.log 2>/dev/null | head -n 1); [ -n "$f" ] && grep -h "GenomeStrategy" "$f" | tail -n 5' || true)"
if [[ -z "$GENOME_LOG" ]]; then
  die "no GenomeStrategy line in the container's newest /app/logs/money_printer_*.log after 10 s --
     the log file may not exist yet (retry: docker exec $CID sh -c 'ls -t /app/logs/money_printer_*.log')
     or WeatherBot never reached the genome block (GENOME_STRATEGY_ID unset at import time)"
fi
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
