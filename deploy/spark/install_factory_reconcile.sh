#!/usr/bin/env bash
# Install + enable the weekly lab-vs-paper reconcile timer ON alcyone (FR-F4.2).
#   bash deploy/spark/install_factory_reconcile.sh
# Idempotent: re-copies the units, reloads systemd, (re)enables the timer.
# Mirrors install_ladder_capture.sh; the Hermes watch is a separate, optional step (below).
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
if ! ls -d "$REPO_DIR"/data/factory/frames/*/ >/dev/null 2>&1; then
  echo "WARN: no frozen frame under data/factory/frames -- the reconcile needs the lab frame"
  echo "      the promoted spec was scored on (frame_search_sha256[:12] in its JSON)."
fi
if ! curl -fsS --max-time 10 "${MONEY_PRINTER_URL:-http://maia.local:8050}/healthz" >/dev/null 2>&1; then
  echo "WARN: ${MONEY_PRINTER_URL:-http://maia.local:8050}/healthz did not answer from here;"
  echo "      the job reads maia over HTTP and will exit 2 until it does."
fi

sudo install -m 0644 \
  "$HERE/systemd/mp-factory-reconcile.service" \
  "$HERE/systemd/mp-factory-reconcile.timer" \
  "$UNIT_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now mp-factory-reconcile.timer

echo
systemctl list-timers mp-factory-reconcile.timer --no-pager
echo
echo "Dry run now :  MP_RECONCILE_DRY_RUN=1 bash $HERE/factory_reconcile.sh"
echo "Real run now:  sudo systemctl start mp-factory-reconcile.service"
echo "Logs        :  journalctl -u mp-factory-reconcile.service -n 80 --no-pager"
echo
echo "Optional Hermes anomaly line (mp_watch.sh style, silent unless something is wrong):"
echo "  cp $REPO_DIR/hermes_plugin/scripts/mp_factory_reconcile.sh ~/.hermes/scripts/"
echo "  ~/.local/bin/hermes cron create 6h --name mp-factory-reconcile --no-agent \\"
echo "      --script mp_factory_reconcile.sh --deliver discord:1491982736989093961 \\"
echo "      --provider custom --model ykarout/Qwen3.5-9B-NVFP4"
