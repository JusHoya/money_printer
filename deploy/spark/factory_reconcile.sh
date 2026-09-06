#!/usr/bin/env bash
# Weekly lab-vs-paper reconcile of the promoted genome -- PRD_STRATEGY_FACTORY FR-F4.2,
# docs/factory/F3_RUNBOOK.md section 5, docs/FACTORY.md. Runs ON alcyone from the
# checkout, normally via mp-factory-reconcile.timer (deploy/spark/systemd/). Safe to
# run by hand: bash deploy/spark/factory_reconcile.sh
#
# What it does
#   1. Finds the genome to reconcile: MP_GENOME_ID, else the id maia reports on
#      GET /api/genome (the deployed one). Refuses if no promoted spec exists for it.
#   2. Picks the reconcile window: the previous Monday..Sunday in UTC by default
#      (MP_RECONCILE_FROM / MP_RECONCILE_TO override, YYYY-MM-DD target_dates).
#   3. Resolves the FROZEN SEARCH FRAME the spec was scored on:
#      data/factory/frames/*_<frame_search_sha256[:12]>. That directory is gitignored
#      and lives only in the lab -- which is why this job is on alcyone, not maia.
#      If the exact frame is absent it falls back to the newest frame dir and SAYS SO
#      (the report then cannot claim the lab trade set of the promoted spec).
#   4. Runs scripts/factory_paper_reconcile.py inside the `lab` compose service
#      (pandas/pyarrow live in the lab image; `lab` keeps its network so it can read
#      maia over HTTP -- the `factory` service is network_mode: none by design):
#        --promoted configs/factory/promoted/<id>.json --url http://maia.local:8050
#        --from <mon> --to <sun> --frames <frame dir>
#      which writes reports/factory/paper_reconcile_<from>_<to>.{json,md}.
#   5. Commits exactly the two report files it produced (explicit paths, never
#      `git add -A`, never a push) unless MP_RECONCILE_NO_COMMIT=1.
#
# Exit code = the reconcile's: 0 OK/PARTIAL, 1 DISCREPANCY (blocking rows), 2 usage,
# 3 REFUSED (nothing comparable -- the normal answer for a SHADOW week outside the
# frozen frame: 0 fills against 0 lab trades is not a pass, and the report says so).
# The Hermes watch (hermes_plugin/scripts/mp_factory_reconcile.sh) posts only when
# the newest report is blocking/refused or the weekly run is overdue.
#
# Env overrides
#   MP_REPO_DIR            checkout to operate on (default: this script's repo)
#   MONEY_PRINTER_URL      sandbox base URL (default http://maia.local:8050)
#   MP_GENOME_ID           genome id (default: maia's /api/genome)
#   MP_RECONCILE_FROM/TO   window (default previous Mon..Sun UTC)
#   MP_RECONCILE_FRAMES    frame dir (default: resolved from the spec's frame_search_sha256)
#   MP_RECONCILE_DRY_RUN=1 print the plan and stop before docker
#   MP_RECONCILE_NO_COMMIT=1  leave the report files uncommitted
set -euo pipefail

REPO_DIR="${MP_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
URL="${MONEY_PRINTER_URL:-http://maia.local:8050}"
COMPOSE="$REPO_DIR/deploy/spark/docker-compose.lab.yml"
cd "$REPO_DIR"

die() { echo "factory_reconcile: $*" >&2; exit 2; }

# --- python for JSON (python3 on alcyone; `python` on a dev box) -------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c pass >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "no python interpreter on PATH"

# --- 1. which genome ---------------------------------------------------------
GID="${MP_GENOME_ID:-}"
if [ -z "$GID" ]; then
  GID=$(curl -sS --max-time 15 "$URL/api/genome" 2>/dev/null \
        | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print((d.get("genome") or {}).get("genome_id") or "")' 2>/dev/null || true)
fi
[ -n "$GID" ] || die "no genome id: set MP_GENOME_ID or make $URL/api/genome answer"
[[ "$GID" =~ ^[0-9a-f]{16}$ ]] || die "genome id '$GID' is not a 16-hex genome_id"
SPEC="configs/factory/promoted/$GID.json"
[ -f "$SPEC" ] || die "no promoted spec at $SPEC (git pull --ff-only first?)"

# --- 2. window -----------------------------------------------------------------
if [ -n "${MP_RECONCILE_FROM:-}" ] || [ -n "${MP_RECONCILE_TO:-}" ]; then
  FROM="${MP_RECONCILE_FROM:?MP_RECONCILE_FROM and MP_RECONCILE_TO go together}"
  TO="${MP_RECONCILE_TO:?MP_RECONCILE_FROM and MP_RECONCILE_TO go together}"
else
  # previous Monday..Sunday, UTC: back up to the most recent Sunday, then 6 more days
  dow=$(date -u +%u)                      # 1 = Monday .. 7 = Sunday
  TO=$(date -u -d "-$dow days" +%F)       # last Sunday
  FROM=$(date -u -d "$TO -6 days" +%F)
fi
[[ "$FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "bad window $FROM..$TO"

# --- 3. the frame the spec was scored on ---------------------------------------
SHA12=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["frame_search_sha256"][:12])' "$SPEC")
FRAMES="${MP_RECONCILE_FRAMES:-}"
FRAME_NOTE=""
if [ -z "$FRAMES" ]; then
  match=$(ls -d data/factory/frames/*_"$SHA12" 2>/dev/null | head -1 || true)
  if [ -n "$match" ]; then
    FRAMES="$match"
  else
    newest=$(ls -d data/factory/frames/*/ 2>/dev/null | sort | tail -1 | sed 's:/$::' || true)
    [ -n "$newest" ] || die "no frozen frame under data/factory/frames (the spec was scored on search sha $SHA12); this job needs the lab frame"
    FRAMES="$newest"
    FRAME_NOTE="WARN: no frame dir matches the spec's frame_search_sha256 ($SHA12); using $newest -- the lab trade set below is NOT the promoted spec's frame and the report's frame_search_sha256 will disagree with the spec"
  fi
fi
[ -d "$FRAMES/search" ] || die "$FRAMES has no search/ frame"

echo "factory_reconcile: genome $GID  window $FROM..$TO  frame $FRAMES  sandbox $URL"
[ -n "$FRAME_NOTE" ] && echo "factory_reconcile: $FRAME_NOTE" >&2
if [ "${MP_RECONCILE_DRY_RUN:-0}" = "1" ]; then
  echo "factory_reconcile: dry run -- stopping before docker"
  exit 0
fi

# --- 4. run inside the lab image (network ON: it must reach maia) ----------------
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
STEM="paper_reconcile_${FROM}_${TO}"
set +e
docker compose -f "$COMPOSE" run --rm -T lab \
  python scripts/factory_paper_reconcile.py \
    --promoted "$SPEC" --url "$URL" --from "$FROM" --to "$TO" --frames "$FRAMES"
rc=$?
set -e
echo "factory_reconcile: reconcile exit $rc -> reports/factory/$STEM.{json,md}"

# --- 5. commit exactly what was produced --------------------------------------
if [ "${MP_RECONCILE_NO_COMMIT:-0}" != "1" ] && [ -f "reports/factory/$STEM.json" ]; then
  git add -- "reports/factory/$STEM.json" "reports/factory/$STEM.md" 2>/dev/null || true
  if ! git diff --cached --quiet -- "reports/factory/$STEM.json" "reports/factory/$STEM.md"; then
    git commit -q -m "data(paper-reconcile): $GID week $FROM..$TO (exit $rc)" \
      -- "reports/factory/$STEM.json" "reports/factory/$STEM.md"
    echo "factory_reconcile: committed reports/factory/$STEM.{json,md} (not pushed)"
  fi
fi
exit "$rc"
