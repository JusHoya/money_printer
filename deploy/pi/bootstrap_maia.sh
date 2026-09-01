#!/usr/bin/env bash
# One-shot host prep for maia (Raspberry Pi 4) — run ON maia as a sudo-capable
# user:  bash deploy/pi/bootstrap_maia.sh
#
# Idempotent: safe to re-run. Prints what it skipped and why.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }

say "Host facts"
uname -a
grep -m1 MemTotal /proc/meminfo
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT

# --- UTC ------------------------------------------------------------------
# Kalshi symbols are ET and parse_expiry() converts ET->UTC. A non-UTC host
# once silently produced 0 training samples for months (HANDOFF.md §5).
if [ "$(timedatectl show -p Timezone --value)" != "Etc/UTC" ]; then
  say "Setting timezone to UTC"
  sudo timedatectl set-timezone Etc/UTC
else
  say "Timezone already UTC — skipped"
fi

# --- Docker ---------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (get.docker.com convenience script)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  say "NOTE: log out and back in for docker group membership to apply"
else
  say "Docker already installed — skipped ($(docker --version))"
fi

# --- State root, ideally on USB-SSD --------------------------------------
# Continuous CSV/log writes destroy SD cards. Prefer any mounted non-SD disk.
STATE_ROOT=/srv/money_printer
if [ ! -d "$STATE_ROOT" ]; then
  say "Creating $STATE_ROOT"
  sudo mkdir -p "$STATE_ROOT"/{data,logs}
  sudo chown -R 1000:1000 "$STATE_ROOT"
fi
ROOT_DEV=$(findmnt -n -o SOURCE /)
case "$ROOT_DEV" in
  /dev/mmcblk*)
    echo "WARNING: / is on the SD card ($ROOT_DEV)."
    echo "  Mount a USB SSD at $STATE_ROOT (or bind it there) before running"
    echo "  the harvester 24/7, or budget for SD replacement. Continuing."
    ;;
  *)
    echo "Root is on $ROOT_DEV (not SD) — good."
    ;;
esac

# --- Log hygiene ----------------------------------------------------------
if ! dpkg -l log2ram >/dev/null 2>&1; then
  say "log2ram not installed (optional, reduces SD wear from journald)."
  echo "  See https://github.com/azlux/log2ram — install manually if / is on SD."
else
  say "log2ram present — skipped"
fi

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

# --- Secrets --------------------------------------------------------------
if [ ! -f "$STATE_ROOT/.env" ]; then
  say "Seeding $STATE_ROOT/.env from template — FILL IT IN before compose up"
  cp "$REPO_DIR/.env.example" "$STATE_ROOT/.env"
  chmod 600 "$STATE_ROOT/.env"
fi
if [ ! -f "$STATE_ROOT/kalshi_priv.key" ]; then
  echo "REMINDER: place the Kalshi RSA private key at $STATE_ROOT/kalshi_priv.key (chmod 600)."
fi

# --- Reconcile timers -----------------------------------------------------
say "Installing systemd reconcile timers"
sudo cp "$REPO_DIR"/deploy/pi/systemd/mp-reconcile-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mp-reconcile-weather.timer mp-reconcile-settlement.timer
systemctl list-timers 'mp-reconcile-*' --no-pager || true

say "Done. Next:"
echo "  1) Fill $STATE_ROOT/.env (Kalshi read-only creds, NWS user-agent, fresh Discord webhook)"
echo "  2) cd $REPO_DIR && docker compose -f deploy/pi/docker-compose.yml up -d --build"
echo "  3) curl -s http://localhost:8050/api/status | head -c 300"
