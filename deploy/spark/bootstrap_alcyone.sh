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
# The alcyone checkout lives under ~/projects (the same layout the M0 capture
# unit hard-codes: /home/jushoya/projects/money_printer).
REPO_DIR="${MP_REPO_DIR:-$HOME/projects/money_printer}"
if [ ! -d "$REPO_DIR/.git" ]; then
  say "Cloning money_printer into $REPO_DIR"
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone https://github.com/JusHoya/money_printer.git "$REPO_DIR"
  git -C "$REPO_DIR" checkout revival/pleiades-2026-09
else
  say "Repo present at $REPO_DIR — fetching"
  git -C "$REPO_DIR" fetch --all --prune
  git -C "$REPO_DIR" checkout revival/pleiades-2026-09
  git -C "$REPO_DIR" pull --ff-only
fi

# --- Lab uid (FR-F0.6) ---------------------------------------------------
# docker-compose.lab.yml runs the lab as ${LAB_UID}:${LAB_GID} so the bind-
# mounted checkout never collects root-owned files. Compose reads this .env
# from the compose file's directory; it is gitignored (.env pattern).
LAB_ENV="$REPO_DIR/deploy/spark/.env"
say "Writing $LAB_ENV (LAB_UID=$(id -u) LAB_GID=$(id -g))"
printf 'LAB_UID=%s\nLAB_GID=%s\n' "$(id -u)" "$(id -g)" > "$LAB_ENV"
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  echo "WARN: $(id -un) is not in the docker group — 'docker compose' will need sudo,"
  echo "      and the M0 capture timer (runs as this user) will fail. Fix:"
  echo "      sudo usermod -aG docker $(id -un) && re-login"
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

say "Checking the lab runs as the host uid (FR-F0.6 exit criterion)"
LAB_UID_SEEN="$(docker compose -f "$REPO_DIR/deploy/spark/docker-compose.lab.yml" run --rm lab id -u | tr -d '[:space:]')"
if [ "$LAB_UID_SEEN" = "$(id -u)" ]; then
  echo "  lab uid = $LAB_UID_SEEN (host uid) — OK"
else
  echo "  WARN: lab uid = '$LAB_UID_SEEN', host uid = $(id -u). Check $LAB_ENV."
fi

say "Done. Next:"
echo "  1) Tape-driven work runs via:"
echo "       docker compose -f deploy/spark/docker-compose.lab.yml run --rm lab \\"
echo "           python scripts/gas_backtest.py --help"
echo "  2) Pin the lab manifest once the image is final:"
echo "       docker compose -f deploy/spark/docker-compose.lab.yml run --rm lab \\"
echo "           pip freeze > deploy/spark/requirements-lab.lock"
echo "  3) M0 ladder capture timer (kill date 2026-09-15):"
echo "       bash deploy/spark/install_ladder_capture.sh"
echo "  4) Hermes/vLLM serving is managed separately (pleiades Phase 3-4);"
echo "     a prepared model swap lives at deploy/spark/hermes_model_swap.sh"
echo "     — do NOT run it before side-by-side validation."
