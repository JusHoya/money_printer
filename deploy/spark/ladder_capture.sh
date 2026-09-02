#!/usr/bin/env bash
# M0 daily ladder capture -- PRD_STRATEGY_FACTORY.md FR-F0.5, roadmap F0 #6.
# Runs ON alcyone from the checkout, normally via mp-ladder-capture.timer
# (deploy/spark/systemd/). Safe to run by hand: bash deploy/spark/ladder_capture.sh
#
# What it does
#   1. Works out "yesterday" as an ET calendar day (Kalshi KXHIGH target dates
#      are settlement days in America/New_York, not host/UTC days).
#   2. Refuses to run past the M0 kill date (2026-09-15) unless
#      MP_CAPTURE_KILL_DATE is overridden on purpose.
#   3. Pulls the KXHIGH{NY,CHI,LAX,MIA} ladders for D-1 -- and re-pulls D-2, so
#      `result` / `expiration_value` land in the tape once Kalshi settles --
#      through the lab container (network on) into the SEALED root
#      data/ladders_2026-09/. The lab runs as the host uid, so nothing in the
#      checkout ends up root-owned (FR-F0.6).
#   4. Commits exactly the files it produced: explicit paths, never `git add -A`,
#      never a push. Pull on maia stays fast-forward because alcyone only adds
#      dated artifacts under one root.
#
# Env overrides
#   MP_CAPTURE_KILL_DATE    last target date that may be captured (default 2026-09-15)
#   MP_CAPTURE_LOOKBACK     days to (re)capture ending at D-1 (default 2)
#   MP_CAPTURE_TARGET_DATE  force the end date (YYYY-MM-DD) instead of ET yesterday
#   MP_CAPTURE_DRY_RUN=1    print the plan and stop before touching the network
#   MP_REPO_DIR             checkout to operate on (default: this script's repo)
set -euo pipefail

REPO_DIR="${MP_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
COMPOSE_FILE="$REPO_DIR/deploy/spark/docker-compose.lab.yml"
OUT_REL="data/ladders_2026-09"
KILL_DATE="${MP_CAPTURE_KILL_DATE:-2026-09-15}"
LOOKBACK="${MP_CAPTURE_LOOKBACK:-2}"
SERIES=(KXHIGHNY KXHIGHCHI KXHIGHLAX KXHIGHMIA)

log() { printf '[ladder-capture %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ "$LOOKBACK" =~ ^[1-9][0-9]*$ ]] || die "MP_CAPTURE_LOOKBACK must be a positive integer (got '$LOOKBACK')"

# --- target dates (ET calendar days) --------------------------------------
et_today="$(TZ=America/New_York date +%F)"
end_date="${MP_CAPTURE_TARGET_DATE:-$(date -u -d "$et_today -1 day" +%F)}"
[[ "$end_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "bad target date '$end_date'"
start_date="$(date -u -d "$end_date -$((LOOKBACK - 1)) day" +%F)"

# --- kill-date guard --------------------------------------------------------
# ISO dates compare lexically. The guard is on the *target* date, so the last
# run captures the kill date itself (fires the morning after it) and the next
# one refuses.
if [[ "$end_date" > "$KILL_DATE" ]]; then
  die "target date $end_date is after the M0 kill date $KILL_DATE" \
      "(PRD_STRATEGY_FACTORY.md FR-F0.5). Disable the timer, or set" \
      "MP_CAPTURE_KILL_DATE deliberately to extend the capture."
fi

# --- preflight --------------------------------------------------------------
cd "$REPO_DIR"
[[ -f "$COMPOSE_FILE" ]] || die "compose file missing: $COMPOSE_FILE"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$REPO_DIR is not a git checkout"

# The lab service interpolates LAB_UID/LAB_GID; a systemd run has no shell
# profile, so compute them here rather than trusting deploy/spark/.env.
export LAB_UID="${LAB_UID:-$(id -u)}"
export LAB_GID="${LAB_GID:-$(id -g)}"

log "repo=$REPO_DIR branch=$(git rev-parse --abbrev-ref HEAD) et_today=$et_today"
log "capture $start_date..$end_date -> $OUT_REL (kill date $KILL_DATE, uid $LAB_UID:$LAB_GID)"

if [[ "${MP_CAPTURE_DRY_RUN:-0}" == "1" ]]; then
  log "dry run: stopping before the pull"
  exit 0
fi
command -v docker >/dev/null 2>&1 || die "docker not on PATH"

# --- pull -------------------------------------------------------------------
docker compose -f "$COMPOSE_FILE" run --rm lab \
  python scripts/backfill_ladders.py --start "$start_date" --end "$end_date" --out "$OUT_REL"

[[ -f "$OUT_REL/manifest.json" ]] || die "backfill wrote no $OUT_REL/manifest.json"

# backfill_ladders.py rewrites manifest.json with the run it just did. Keep a
# per-run copy so every day's provenance stays in the tree, not only in git
# history, and the SHA/coverage of each capture can be audited at M1.
mkdir -p "$OUT_REL/manifests"
cp "$OUT_REL/manifest.json" "$OUT_REL/manifests/$end_date.json"

# The marker makes src/backtest/sealed_roots.py refuse this directory even if
# it is copied or renamed (FR-F0.5: the search-frame loader refuses this root).
if [[ ! -f "$OUT_REL/SEALED" ]]; then
  {
    echo "SEALED -- PRD_STRATEGY_FACTORY.md FR-F0.5 / section 4 A3."
    echo "M0 daily ladder capture (R3 reserve). Never loaded by the search frame;"
    echo "opened once by the F4 holdout path under an unseal record."
  } > "$OUT_REL/SEALED"
fi

# --- commit (explicit paths only) ------------------------------------------
paths=("$OUT_REL/manifest.json" "$OUT_REL/manifests/$end_date.json" "$OUT_REL/SEALED")
captured=0
d="$start_date"
while [[ ! "$d" > "$end_date" ]]; do
  for s in "${SERIES[@]}"; do
    f="$OUT_REL/$s/$d.csv"
    if [[ -f "$f" ]]; then
      paths+=("$f")
      captured=$((captured + 1))
    else
      log "WARN: no ladder file for $s $d (empty day or HTTP failure -- see the manifest)"
    fi
  done
  d="$(date -u -d "$d +1 day" +%F)"
done
log "ladder files present for this window: $captured"

git add -- "${paths[@]}"
if git diff --cached --quiet -- "$OUT_REL"; then
  log "nothing new under $OUT_REL; no commit"
  exit 0
fi

git_id=()
if ! git config user.email >/dev/null 2>&1; then
  git_id=(-c user.name=mp-ladder-capture -c user.email=mp-ladder-capture@alcyone.local)
fi
git ${git_id[@]+"${git_id[@]}"} commit --quiet \
  -m "data(ladders_2026-09): capture $end_date" -- "${paths[@]}"
log "committed $(git rev-parse --short HEAD): data(ladders_2026-09): capture $end_date (no push)"
