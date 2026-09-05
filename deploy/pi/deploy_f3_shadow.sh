#!/usr/bin/env bash
# F3 shadow deploy for maia -- one command, idempotent (docs/factory/F3_RUNBOOK.md §3, §1.1).
#
#   ON maia, from the checkout (~/money_printer):
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id>            # e.g. 0c4b20502f2daf65 (fr31a_taker)
#       bash deploy/pi/deploy_f3_shadow.sh <genome_id> --no-repair   # skip the NO-side state repair
#
# What it does, in order:
#   1. git pull --ff-only (refuses on a non-fast-forward, like deploy/README.md says)
#   2. creates /srv/money_printer/data/forecast_cache owned by uid 1000 (compose bind)
#   3. upserts GENOME_STRATEGY_ID / GENOME_STRATEGY_MODE=shadow in /srv/money_printer/.env
#      (no duplicate lines; shadow is also pinned by the compose file itself)
#   4. stops the sandbox, repairs the NO-side settlement sign in exchange_state.json and
#      trade_journal.jsonl (scripts/repair_no_settlement_pnl.py, dry run printed first,
#      then --apply with .bak-n backups) -- the engine fix 724d93c only corrects FUTURE
#      settlements; historical NO closes since 2026-09-01 are sign-flipped until repaired
#   5. docker compose up -d --build, /healthz, container env check
#   6. prints how to verify the F3 maia criterion after the next :00 UTC boundary
set -euo pipefail

GENOME_ID="${1:-}"
[[ -n "$GENOME_ID" ]] || { echo "usage: $0 <genome_id> [--no-repair]" >&2; exit 2; }
DO_REPAIR=1
[[ "${2:-}" == "--no-repair" ]] && DO_REPAIR=0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
STATE_ROOT=/srv/money_printer
ENV_FILE="$STATE_ROOT/.env"
SPEC="$ROOT/configs/factory/promoted/$GENOME_ID.json"

log() { printf '[deploy_f3_shadow %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; restore; exit 1; }
STOPPED=0   # set to 1 while the sandbox is stopped for the repair
restore() { if [[ "$STOPPED" == 1 ]]; then log "restoring the sandbox after a failure"; "${COMPOSE[@]}" up -d || true; fi; }
trap restore ERR

cd "$ROOT"
log "1/6 git pull --ff-only"
git pull --ff-only || die "git pull refused (non-fast-forward) -- reconcile by hand, never reset the sandbox checkout"
[[ -f "$SPEC" ]] || die "promoted spec missing after pull: $SPEC"
grep -q '"mode": "shadow"' "$SPEC" || die "$SPEC is not a shadow spec; F3 deploys shadow only"

log "2/6 forecast cache bind"
sudo mkdir -p "$STATE_ROOT/data/forecast_cache"
sudo chown 1000:1000 "$STATE_ROOT/data/forecast_cache"

log "3/6 runtime env ($ENV_FILE)"
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
upsert GENOME_STRATEGY_MODE shadow
sudo grep -E '^GENOME_' "$ENV_FILE"

SERVICE=sandbox   # the runtime service (container mp-sandbox); NOT config --services|head, which lists autoheal first
if [[ "$DO_REPAIR" == 1 ]]; then
  log "4/6 NO-side settlement repair (sandbox stopped while the state file is rewritten)"
  "${COMPOSE[@]}" stop
  STOPPED=1
  REPAIR=(python scripts/repair_no_settlement_pnl.py --state /app/data/exchange_state.json --journal /app/data/trade_journal.jsonl)
  RUN=("${COMPOSE[@]}" run --rm --no-deps --entrypoint python "$SERVICE")
  "${RUN[@]}" -c 'import src.core.matching_engine; print("repair image OK")' || die "the $SERVICE image cannot import the engine"
  set +e
  "${RUN[@]}" "${REPAIR[@]:1}"                      # dry run: exit 1 = repairs pending, 0 = nothing to do
  rc=$?
  set -e
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
  log "4/6 repair skipped (--no-repair)"
fi

log "5/6 docker compose up -d --build"
"${COMPOSE[@]}" up -d --build
STOPPED=0   # set to 1 while the sandbox is stopped for the repair
sleep 8
curl -sf http://localhost:8050/healthz || die "healthz failed"
echo
CID="$("${COMPOSE[@]}" ps -q "$SERVICE")"
docker exec "$CID" env | grep -E 'GENOME_|MP_FORECAST' || die "GENOME_* not visible in the container"

log "6/6 done. After the next :00 UTC boundary, from any LAN host:"
echo "   python scripts/check_maia_emit_cadence.py --url http://maia.local:8050/api/logs/tail"
echo "   expected: PASS with verified_ok >= 1 and outcome_codes == {GENOME_SHADOW: n_emit}"
echo "   rollback: remove GENOME_STRATEGY_ID from $ENV_FILE and run: ${COMPOSE[*]} up -d"
