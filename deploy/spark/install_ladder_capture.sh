#!/usr/bin/env bash
# Install + enable the M0 ladder-capture timer ON alcyone.
#   bash deploy/spark/install_ladder_capture.sh
# Idempotent: re-copies the units, reloads systemd, (re)enables the timer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system
REPO_DIR="$(cd "$HERE/../.." && pwd)"

if [ "$REPO_DIR" != "$HOME/projects/money_printer" ]; then
  echo "WARN: this checkout is $REPO_DIR but the unit hard-codes"
  echo "      /home/jushoya/projects/money_printer -- edit the .service before enabling."
fi
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  echo "WARN: $(id -un) is not in the docker group; the service runs as that user"
  echo "      and needs it (sudo usermod -aG docker $(id -un); re-login)."
fi
if [ ! -f "$HERE/.env" ]; then
  echo "NOTE: $HERE/.env absent -- fine for the timer (the script exports LAB_UID/LAB_GID"
  echo "      itself) but interactive 'docker compose run' will default to uid 1000."
fi

sudo install -m 0644 \
  "$HERE/systemd/mp-ladder-capture.service" \
  "$HERE/systemd/mp-ladder-capture.timer" \
  "$UNIT_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now mp-ladder-capture.timer

echo
systemctl list-timers mp-ladder-capture.timer --no-pager
echo
echo "Dry run now :  MP_CAPTURE_DRY_RUN=1 bash $HERE/ladder_capture.sh"
echo "Real run now:  sudo systemctl start mp-ladder-capture.service"
echo "Logs        :  journalctl -u mp-ladder-capture.service -n 80 --no-pager"
