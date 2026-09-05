#!/usr/bin/env bash
# Host-side wrapper for one strategy-factory run ON alcyone -- Phase F2
# (PRD_STRATEGY_FACTORY.md FR-F2.6 / Phase F2 exit criteria; docs/factory/F2_RUNBOOK.md).
# Normally launched by the user unit deploy/spark/systemd/mp-factory@.service
# (`systemctl --user start mp-factory@<run_id>`); safe to run by hand:
#
#   bash deploy/spark/mp_factory_run.sh run_2026-09-04
#
# What it does, in order (every step through the network-less `factory`
# compose service, as the host uid -- FR-F0.6):
#   1. factory.py run --run-id <run_id>          (adds --resume automatically when
#                                                 data/factory/runs/<run_id>/run.json
#                                                 already exists: a restarted unit
#                                                 continues from the last SCORED gen)
#   2. factory.py controls <run_id>              (41 control replicates, resumable)
#   3. factory.py report <run_id>                (summary.json/.md, oos_by_date.csv,
#                                                 finalists.json, board.md, latest.json)
#   4. merges the factory throughput + host resources into
#      reports/factory/<run_id>/bench.json (scripts/factory_bench_coexist.py; the
#      mp-vllm idle/running samples are taken separately, see the runbook)
#   5. deploy/spark/mp_factory_notify.sh <run_id> DONE|FAILED  -- writes
#      reports/factory/<run_id>/completion.txt (the monitor cron's fallback
#      post) and tries `hermes send` to Discord.
# Throughout, a background sampler appends `free -g` every 60 s to
# reports/factory/<run_id>/resources.log (gitignored; before/after/peak used
# GiB are summarised at the end and copied into completion.txt / bench.json).
#
# Env overrides
#   MP_REPO_DIR              checkout to operate on (default: this script's repo)
#   MP_FACTORY_RUN_ARGS      extra args for `factory.py run` (e.g. "--config configs/factory/x.yaml")
#   MP_FACTORY_RESUME_ARGS   what to add when run.json exists (default "--resume")
#   MP_FACTORY_SKIP_CONTROLS=1  skip step 2 (smoke runs only; the report says so)
#   MP_FACTORY_SAMPLE_S      sampler period in seconds (default 60)
#   MP_FACTORY_DRY_RUN=1     print the plan and stop before touching docker
set -euo pipefail

RUN_ID="${1:-${MP_FACTORY_RUN_ID:-}}"
[[ -n "$RUN_ID" ]] || { echo "usage: $0 <run_id>" >&2; exit 2; }
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "bad run_id '$RUN_ID' (want [A-Za-z0-9._-])" >&2; exit 2; }

REPO_DIR="${MP_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
HERE="$REPO_DIR/deploy/spark"
COMPOSE_FILE="$HERE/docker-compose.lab.yml"
RUN_DIR_REL="data/factory/runs/$RUN_ID"
REPORT_DIR_REL="reports/factory/$RUN_ID"
SAMPLE_S="${MP_FACTORY_SAMPLE_S:-60}"

log() { printf '[mp-factory %s %s] %s\n' "$RUN_ID" "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

cd "$REPO_DIR"
[[ -f "$COMPOSE_FILE" ]] || die "compose file missing: $COMPOSE_FILE"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$REPO_DIR is not a git checkout"

# Bind sources + nested mountpoints must pre-exist on the host (compose header).
mkdir -p logs data/factory data/ladders_holdout data/ladders_2026-09 "$REPORT_DIR_REL"
export LAB_UID="${LAB_UID:-$(id -u)}"
export LAB_GID="${LAB_GID:-$(id -g)}"

RESUME=()
if [[ -f "$RUN_DIR_REL/run.json" ]]; then
  read -r -a RESUME <<< "${MP_FACTORY_RESUME_ARGS:---resume}"
  log "$RUN_DIR_REL/run.json exists -> resuming (${RESUME[*]})"
fi
RUN_ARGS=()
if [[ -n "${MP_FACTORY_RUN_ARGS:-}" ]]; then
  read -r -a RUN_ARGS <<< "$MP_FACTORY_RUN_ARGS"
fi

log "repo=$REPO_DIR rev=$(git rev-parse --short HEAD) branch=$(git rev-parse --abbrev-ref HEAD) uid=$LAB_UID:$LAB_GID"
log "plan: run --run-id $RUN_ID ${RESUME[*]:-} ${RUN_ARGS[*]:-} -> controls -> report -> bench merge -> notify"
if [[ "${MP_FACTORY_DRY_RUN:-0}" == "1" ]]; then
  log "dry run: stopping before docker"
  exit 0
fi
command -v docker >/dev/null 2>&1 || die "docker not on PATH"

# --- resource sampler --------------------------------------------------------
RES_LOG="$REPORT_DIR_REL/resources.log"
free_used_gib()  { free -g | awk '/^Mem:/{print $3}'; }
free_avail_gib() { free -g | awk '/^Mem:/{print $7}'; }
T0=$(date +%s)
{
  echo "# mp-factory $RUN_ID resources (free -g, GiB); t = seconds since start"
  echo "# start $(date -u +%FT%TZ)"
  echo "t=0 used_gib=$(free_used_gib) avail_gib=$(free_avail_gib) phase=before"
} >> "$RES_LOG"
USED_BEFORE=$(free_used_gib)

SAMPLER_PID=""
if [[ "$SAMPLE_S" =~ ^[1-9][0-9]*$ ]]; then
  (
    while sleep "$SAMPLE_S"; do
      echo "t=$(( $(date +%s) - T0 )) used_gib=$(free_used_gib) avail_gib=$(free_avail_gib) phase=$(cat "$REPORT_DIR_REL/.phase" 2>/dev/null || echo run)"
    done >> "$RES_LOG"
  ) &
  SAMPLER_PID=$!
fi

STATE=FAILED
finish() {
  local rc=$?
  set +e
  if [[ -n "$SAMPLER_PID" ]]; then kill "$SAMPLER_PID" 2>/dev/null; wait "$SAMPLER_PID" 2>/dev/null; fi
  local wall=$(( $(date +%s) - T0 ))
  local used_after; used_after=$(free_used_gib)
  echo "t=$wall used_gib=$used_after avail_gib=$(free_avail_gib) phase=after" >> "$RES_LOG"
  local peak; peak=$(awk -F'used_gib=' '/used_gib=/{split($2,a," "); if(a[1]+0>m)m=a[1]+0} END{print m+0}' "$RES_LOG")
  {
    echo "# end $(date -u +%FT%TZ) state=$STATE rc=$rc wall_s=$wall"
    echo "# used_gib before=$USED_BEFORE after=$used_after peak=$peak"
  } >> "$RES_LOG"
  rm -f "$REPORT_DIR_REL/.phase"
  log "state=$STATE rc=$rc wall=${wall}s used_gib before=$USED_BEFORE after=$used_after peak=$peak"
  # bench.json: factory throughput (from the run's status/run.json) + host numbers.
  # The mp-vllm idle/running samples are merged into the same file by the runbook steps.
  if command -v python3 >/dev/null 2>&1; then
    python3 scripts/factory_bench_coexist.py \
      --out "$REPORT_DIR_REL/bench.json" \
      --throughput-from "$RUN_DIR_REL/status.json" --throughput-from "$RUN_DIR_REL/run.json" \
      --wall-s "$wall" \
      --extra "used_gib_before=$USED_BEFORE" --extra "used_gib_after=$used_after" --extra "used_gib_peak=$peak" \
      --extra "state=$STATE" || log "WARN: bench merge failed (non-fatal)"
  fi
  MP_FACTORY_WALL_S="$wall" MP_FACTORY_USED_GIB="$USED_BEFORE/$used_after/$peak" \
    bash "$HERE/mp_factory_notify.sh" "$RUN_ID" "$STATE" || log "WARN: notify failed (non-fatal)"
  exit "$rc"
}
trap finish EXIT

compose_factory() {
  docker compose -f "$COMPOSE_FILE" run --rm factory python scripts/factory.py "$@"
}

# --- 1. run ------------------------------------------------------------------
echo run > "$REPORT_DIR_REL/.phase"
log "step 1/3: factory.py run --run-id $RUN_ID ${RESUME[*]:-} ${RUN_ARGS[*]:-}"
compose_factory run --run-id "$RUN_ID" ${RESUME[@]+"${RESUME[@]}"} ${RUN_ARGS[@]+"${RUN_ARGS[@]}"}
[[ -f "$RUN_DIR_REL/run.json" ]] || die "run wrote no $RUN_DIR_REL/run.json"

# --- 2. controls -------------------------------------------------------------
if [[ "${MP_FACTORY_SKIP_CONTROLS:-0}" == "1" ]]; then
  log "step 2/3: controls SKIPPED (MP_FACTORY_SKIP_CONTROLS=1)"
else
  echo controls > "$REPORT_DIR_REL/.phase"
  log "step 2/3: factory.py controls $RUN_ID"
  compose_factory controls "$RUN_ID"
fi

# --- 3. report ---------------------------------------------------------------
echo report > "$REPORT_DIR_REL/.phase"
log "step 3/3: factory.py report $RUN_ID"
compose_factory report "$RUN_ID"
[[ -f "$REPORT_DIR_REL/summary.json" ]] || die "report wrote no $REPORT_DIR_REL/summary.json"

STATE=DONE
log "done: $REPORT_DIR_REL/{summary.json,summary.md,oos_by_date.csv,finalists.json,board.md,status.json,run.json,bench.json}"
