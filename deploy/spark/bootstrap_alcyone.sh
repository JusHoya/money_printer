#!/usr/bin/env bash
# One-shot lab bring-up for alcyone (DGX Spark) — run ON alcyone:
#   bash deploy/spark/bootstrap_alcyone.sh
#
# Prepares the OFFLINE lab container only (backtests, calibration builds,
# model training against the harvested tape). The Hermes/vLLM serving layer
# is separate — see the pleiades runbooks and deploy/spark/hermes_model_swap.sh.
#
# Idempotent: safe to re-run. Prints what it skipped and why.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }

say "Host facts"
uname -a
grep -m1 MemTotal /proc/meminfo

# --- Docker ---------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. The Spark ships with Docker — check PATH or"
  echo "install per the pleiades cluster runbook before re-running."
  exit 1
fi
say "Docker present ($(docker --version))"

# --- Repo -----------------------------------------------------------------
REPO_DIR="$HOME/money_printer"
if [ ! -d "$REPO_DIR/.git" ]; then
  say "Cloning money_printer"
  git clone https://github.com/JusHoya/money_printer.git "$REPO_DIR"
  git -C "$REPO_DIR" checkout revival/pleiades-2026-09
else
  say "Repo present — fetching"
  git -C "$REPO_DIR" fetch --all --prune
  git -C "$REPO_DIR" checkout revival/pleiades-2026-09
  git -C "$REPO_DIR" pull --ff-only
fi

# --- Archive mount --------------------------------------------------------
# docker-compose.lab.yml mounts ~/mp_archive/extracted read-only at /archive.
# The committed manifest hashes the *tarballs* (archive/*.tgz), so integrity
# is checked against those when they are still on disk next to extracted/.
ARCHIVE_ROOT="$HOME/mp_archive"
MANIFEST="$REPO_DIR/vm_snapshot_2026_08_22/meta/archive_sha256.txt"
if [ -d "$ARCHIVE_ROOT/archive" ] && [ -f "$MANIFEST" ]; then
  say "Verifying archive tarballs against $(basename "$MANIFEST")"
  (cd "$ARCHIVE_ROOT" && sha256sum -c "$MANIFEST")
elif [ -d "$ARCHIVE_ROOT/extracted" ]; then
  say "Archive extracted at $ARCHIVE_ROOT/extracted — tarballs absent, hash check skipped"
else
  say "No archive at $ARCHIVE_ROOT — lab still works for code-only runs"
  echo "  Copy the VM snapshot payload (vm_snapshot_2026_08_22/MANIFEST.md) to"
  echo "  $ARCHIVE_ROOT, verify against $MANIFEST,"
  echo "  and extract into $ARCHIVE_ROOT/extracted before tape-driven work."
  mkdir -p "$ARCHIVE_ROOT/extracted"
fi

# --- Lab container --------------------------------------------------------
say "Building the lab image (torch arm64 wheels — first build is slow)"
docker compose -f "$REPO_DIR/deploy/spark/docker-compose.lab.yml" build

say "Smoke-running the lab container"
docker compose -f "$REPO_DIR/deploy/spark/docker-compose.lab.yml" run --rm lab

say "Done. Next:"
echo "  1) Tape-driven work runs via:"
echo "       docker compose -f deploy/spark/docker-compose.lab.yml run --rm lab \\"
echo "           python scripts/gas_backtest.py --help"
echo "  2) Hermes/vLLM serving is managed separately (pleiades Phase 3-4);"
echo "     a prepared model swap lives at deploy/spark/hermes_model_swap.sh"
echo "     — do NOT run it before side-by-side validation."
